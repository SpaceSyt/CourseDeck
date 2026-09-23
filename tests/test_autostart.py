import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coursedeck import autostart


@pytest.fixture
def startup(tmp_path, monkeypatch):
    folder = tmp_path / "Startup"
    monkeypatch.setattr(autostart, "_startup_folder", lambda: folder)
    monkeypatch.setattr(autostart.Autostart, "supported", property(lambda self: True))
    monkeypatch.setattr(autostart, "_pythonw", lambda: tmp_path / "Python 环境" / "pythonw.exe")
    return folder


def test_status_does_not_create_folder(startup, tmp_path):
    setting = autostart.Autostart(tmp_path / "data", 48321)
    assert setting.status() == {"supported": True, "enabled": False}
    assert not startup.exists()


def test_enable_disable_idempotent_and_preserves_other_files(startup, tmp_path):
    setting = autostart.Autostart(tmp_path / "data", 48321)
    assert setting.set_enabled(True) == {"supported": True, "enabled": True}
    other = startup / "other.vbs"
    other.write_text("unrelated", encoding="utf-8")
    entry = next(startup.glob("CourseDeck-*.vbs"))
    original = entry.read_bytes()
    assert setting.set_enabled(True)["enabled"]
    assert entry.read_bytes() == original
    assert not setting.set_enabled(False)["enabled"]
    assert not setting.set_enabled(False)["enabled"]
    assert other.read_text(encoding="utf-8") == "unrelated"
    assert list(startup.iterdir()) == [other]


def test_custom_data_port_and_unicode_launcher(startup, tmp_path):
    data = tmp_path / "我的 data"
    setting = autostart.Autostart(data, 51234)
    setting.set_enabled(True)
    entry = next(startup.glob("CourseDeck-*.vbs"))
    contents = entry.read_text(encoding="utf-16")
    assert contents.startswith(setting.marker + "\n")
    assert '""' + str(data.resolve()) + '""' in contents
    assert "Python 环境" in contents
    assert "-m coursedeck.tray --background --port 51234 --data-dir" in contents
    assert ", 0, False" in contents
    updated = autostart.Autostart(data, 51235)
    assert updated.status()["enabled"]
    updated.set_enabled(True)
    assert "--port 51235" in entry.read_text(encoding="utf-16")
    separate = autostart.Autostart(tmp_path / "other data", 51234)
    assert not separate.status()["enabled"]
    separate.set_enabled(True)
    updated.set_enabled(False)
    assert separate.status()["enabled"]


@pytest.mark.parametrize("contents", [b"unrelated", "' foreign entry".encode("utf-16")])
def test_collision_never_overwritten_or_deleted(startup, tmp_path, contents):
    setting = autostart.Autostart(tmp_path / "data", 48321)
    startup.mkdir()
    entry = startup / f"CourseDeck-{setting.key}.vbs"
    entry.write_bytes(contents)
    for operation in (
        setting.status,
        lambda: setting.set_enabled(True),
        lambda: setting.set_enabled(False),
    ):
        with pytest.raises(OSError, match="not managed"):
            operation()
        assert entry.read_bytes() == contents


def test_unsupported_platform_never_touches_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.Autostart, "supported", property(lambda self: False))

    def unexpected():
        pytest.fail("Startup folder must not be inspected on this platform")

    monkeypatch.setattr(autostart, "_startup_folder", unexpected)
    setting = autostart.Autostart(tmp_path / "data", 48321)
    assert setting.status() == {"supported": False, "enabled": False}
    with pytest.raises(ValueError, match="only supported on Windows"):
        setting.set_enabled(True)


@pytest.mark.parametrize("port", [True, 0, -1, 65536, "48321"])
def test_invalid_port_rejected(tmp_path, port):
    with pytest.raises(ValueError, match="port"):
        autostart.Autostart(tmp_path, port)


def test_invalid_enabled_rejected(startup, tmp_path):
    setting = autostart.Autostart(tmp_path, 48321)
    with pytest.raises(ValueError, match="boolean"):
        setting.set_enabled("false")
    assert not startup.exists()


def test_missing_pythonw_does_not_create_entry(startup, tmp_path, monkeypatch):
    def missing():
        raise OSError("The Python windowless launcher is unavailable.")

    monkeypatch.setattr(autostart, "_pythonw", missing)
    with pytest.raises(OSError, match="launcher"):
        autostart.Autostart(tmp_path, 48321).set_enabled(True)
    assert not startup.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Script Host integration")
def test_windows_script_preserves_unicode_spaces_and_percent_paths(startup, tmp_path, monkeypatch):
    clone = tmp_path / "课程 clone"
    package = clone / "coursedeck"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    result = tmp_path / "arguments.json"
    (package / "tray.py").write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(result)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(autostart, "__file__", str(package / "autostart.py"))
    monkeypatch.setattr(autostart, "_pythonw", lambda: Path(sys.executable))
    monkeypatch.setenv("COURSEDECK_TEST_PATH", "must-not-be-expanded")
    data = tmp_path / "资料 %COURSEDECK_TEST_PATH% with spaces"
    setting = autostart.Autostart(data, 51234)
    setting.set_enabled(True)
    entry = next(startup.glob("CourseDeck-*.vbs"))
    completed = subprocess.run(
        ["cscript.exe", "//B", "//Nologo", str(entry)],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 10
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(result.read_text(encoding="utf-8")) == [
        "--background",
        "--port",
        "51234",
        "--data-dir",
        str(data.resolve()),
    ]
