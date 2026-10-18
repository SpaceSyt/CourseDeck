import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from coursedeck import __main__, runtime


def test_default_directory_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("COURSEDECK_DATA_DIR", raising=False)
    expected = Path(runtime.__file__).resolve().parent.parent / "data"
    for directory in (tmp_path, tmp_path / "other"):
        directory.mkdir(exist_ok=True)
        monkeypatch.chdir(directory)
        assert runtime.data_directory() == expected


def test_explicit_directory_overrides_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COURSEDECK_DATA_DIR", str(tmp_path / "environment"))
    assert runtime.data_directory() == tmp_path / "environment"
    assert runtime.data_directory("chosen") == tmp_path / "chosen"


@contextmanager
def holding_process(directory, ready):
    script = """
import sys
from pathlib import Path
from coursedeck.runtime import instance_lock
with instance_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text('ready')
    sys.stdin.readline()
"""
    # The base interpreter avoids Windows venv launchers retaining a separate child PID.
    process = subprocess.Popen(
        [sys._base_executable, "-c", script, str(directory), str(ready)],
        cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(f"Lock holder failed: {process.communicate()[1]}")
            if time.monotonic() >= deadline:
                pytest.fail("Lock holder did not start")
            time.sleep(0.02)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("kill", [False, True])
def test_process_lock_blocks_second_owner_and_releases_after_exit(tmp_path, kill):
    directory = tmp_path / "data"
    with holding_process(directory, tmp_path / "ready") as process:
        with pytest.raises(runtime.StartupError, match="already using"):
            with runtime.instance_lock(directory):
                pytest.fail("Concurrent ownership was allowed")
        if kill:
            process.kill()
            process.communicate(timeout=10)
        else:
            process.communicate("exit\n", timeout=10)
            assert process.returncode == 0
    assert (directory / "coursedeck.lock").exists()
    with runtime.instance_lock(directory):
        pass


def test_occupied_port_rejects_startup_before_constructing_server(tmp_path, monkeypatch, capsys):
    server = Mock(side_effect=AssertionError("App startup must not be reached"))
    monkeypatch.setattr(__main__.uvicorn, "Server", server)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.bind(("127.0.0.1", 0))
        existing.listen()
        port = existing.getsockname()[1]
        assert __main__.main(["--data-dir", str(tmp_path), "--port", str(port)]) == 1
    server.assert_not_called()
    assert "Port" in capsys.readouterr().err
    with runtime.instance_lock(tmp_path), runtime.listening_socket(port):
        pass


def test_same_data_directory_rejects_another_port(tmp_path, monkeypatch, capsys):
    server = Mock(side_effect=AssertionError("App startup must not be reached"))
    monkeypatch.setattr(__main__.uvicorn, "Server", server)
    directory = tmp_path / "data"
    with holding_process(directory, tmp_path / "ready"):
        assert __main__.main(["--data-dir", str(directory), "--port", "0"]) == 1
    server.assert_not_called()
    assert "Cannot start CourseDeck" in capsys.readouterr().err


def test_startup_passes_reserved_socket_and_releases_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("COURSEDECK_DATA_DIR", "unused")
    captured = {}

    def run(*, sockets):
        sock = sockets[0]
        captured["port"] = sock.getsockname()[1]
        captured["socket"] = sock
        assert os.environ["COURSEDECK_DATA_DIR"] == str(tmp_path)
        with pytest.raises(runtime.StartupError):
            with runtime.listening_socket(captured["port"]):
                pytest.fail("Reserved port was reused")

    server = Mock()
    server.return_value.run.side_effect = run
    monkeypatch.setattr(__main__.uvicorn, "Server", server)
    assert __main__.main(["--data-dir", str(tmp_path), "--port", "0"]) == 0
    assert captured["socket"].fileno() == -1
    with runtime.instance_lock(tmp_path), runtime.listening_socket(captured["port"]):
        pass


def test_server_failure_releases_directory_and_port(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURSEDECK_DATA_DIR", "unused")
    captured = {}

    def fail(*, sockets):
        captured["port"] = sockets[0].getsockname()[1]
        raise OSError("Server failed")

    server = Mock()
    server.return_value.run.side_effect = fail
    monkeypatch.setattr(__main__.uvicorn, "Server", server)
    assert __main__.main(["--data-dir", str(tmp_path), "--port", "0"]) == 1
    assert "Server failed" in capsys.readouterr().err
    with runtime.instance_lock(tmp_path), runtime.listening_socket(captured["port"]):
        pass


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_invalid_port_does_not_create_data_directory(tmp_path, port):
    directory = tmp_path / "unused"
    with pytest.raises(SystemExit) as error:
        __main__.main(["--data-dir", str(directory), "--port", port])
    assert error.value.code == 2
    assert not directory.exists()
