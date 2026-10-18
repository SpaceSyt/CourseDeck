import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request

from coursedeck import tray
from coursedeck.runtime import StartupError, instance_lock, listening_socket


@contextmanager
def running_service(directory, app_factory):
    with instance_lock(directory), listening_socket(0) as sock:
        service = tray.TrayService(directory, sock, app_factory=app_factory)
        try:
            service.start(timeout=5)
            yield service
        finally:
            service.stop()


def test_service_identity_and_graceful_shutdown_release_resources(tmp_path):
    entered = threading.Event()
    exited = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        try:
            yield
        finally:
            exited.set()

    with running_service(tmp_path, lambda: FastAPI(lifespan=lifespan)) as service:
        assert entered.is_set()
        assert service.ready
        assert tray.existing_tray_url(tmp_path) == service.url
        port = service.port
        with pytest.raises(StartupError):
            with instance_lock(tmp_path):
                pytest.fail("Service data directory must remain locked")
    assert exited.is_set()
    assert not service.thread.is_alive()
    assert not service.ready
    assert not (tmp_path / "tray.json").exists()
    with instance_lock(tmp_path), listening_socket(port):
        pass


def test_identity_route_takes_precedence_over_existing_spa_catchall(tmp_path):
    app = FastAPI()

    @app.get("/{path:path}")
    async def spa(path: str):
        return {"page": path}

    with running_service(tmp_path, lambda: app) as service:
        assert tray.existing_tray_url(tmp_path) == service.url
        with httpx.Client(base_url=service.url, trust_env=False) as client:
            assert client.get("/courses").json() == {"page": "courses"}


def test_failed_startup_leaves_no_thread_or_descriptor(tmp_path):
    @asynccontextmanager
    async def lifespan(app):
        raise RuntimeError("Synthetic startup failure")
        yield

    with pytest.raises(StartupError, match="could not start"):
        with running_service(tmp_path, lambda: FastAPI(lifespan=lifespan)):
            pytest.fail("Failed lifespan was reported as ready")
    assert not (tmp_path / "tray.json").exists()
    assert not any(t.name == "CourseDeck service" for t in threading.enumerate())
    with instance_lock(tmp_path):
        pass


def test_stalled_async_startup_can_be_cancelled_and_cleans_up(tmp_path):
    entered = threading.Event()
    cleaned_up = threading.Event()
    release = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        try:
            # The release event keeps a broken implementation from hanging the test run.
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield
        finally:
            cleaned_up.set()

    with instance_lock(tmp_path), listening_socket(0) as sock:
        port = sock.getsockname()[1]
        service = tray.TrayService(tmp_path, sock, app_factory=lambda: FastAPI(lifespan=lifespan))
        stopper = threading.Thread(target=service.stop, daemon=True)
        try:
            with pytest.raises(StartupError, match="took too long"):
                service.start(timeout=0.25)
            assert entered.wait(timeout=1)
            stopper.start()
            stopper.join(timeout=3)
            assert not stopper.is_alive(), "Shutdown remained blocked on unfinished startup"
            assert cleaned_up.is_set()
            assert not service.thread.is_alive()
            assert not (tmp_path / "tray.json").exists()
        finally:
            release.set()
            if stopper.ident is not None:
                stopper.join(timeout=5)
            service.stop()
    with instance_lock(tmp_path), listening_socket(port):
        pass


def test_mismatching_service_descriptor_is_rejected(tmp_path):
    with running_service(tmp_path, FastAPI) as service:
        descriptor = tmp_path / "tray.json"
        descriptor.write_text(
            json.dumps({"port": service.port, "instance": "0" * 32}), encoding="utf-8"
        )
        assert tray.existing_tray_url(tmp_path) is None


def test_stale_service_descriptor_is_rejected(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (tmp_path / "tray.json").write_text(
        json.dumps({"port": port, "instance": "0" * 32}), encoding="utf-8"
    )
    assert tray.existing_tray_url(tmp_path) is None


@pytest.mark.parametrize("payload", [None, [], {}, {"instance": None}])
def test_malformed_identity_response_does_not_match(tmp_path, monkeypatch, payload):
    (tmp_path / "tray.json").write_text(
        json.dumps({"port": 12345, "instance": "0" * 32}), encoding="utf-8"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    monkeypatch.setattr(tray.httpx, "Client", lambda **kwargs: client)
    assert tray.existing_tray_url(tmp_path) is None


@pytest.mark.parametrize("identity", [None, "", [], "not-an-instance"])
def test_invalid_descriptor_never_contacts_service(tmp_path, monkeypatch, identity):
    (tmp_path / "tray.json").write_text(
        json.dumps({"port": 12345, "instance": identity}), encoding="utf-8"
    )
    client = Mock(side_effect=AssertionError("Invalid descriptor must not contact a server"))
    monkeypatch.setattr(tray.httpx, "Client", client)
    assert tray.existing_tray_url(tmp_path) is None
    client.assert_not_called()


@pytest.mark.parametrize("mail_status", ["connected", "disconnected", "login_pending"])
def test_sync_requests_sources_and_only_connected_mail(tmp_path, mail_status):
    app = FastAPI()
    calls = []

    @app.post("/api/sync")
    async def sources(request: Request):
        calls.append(("sources", request.headers.get("X-CourseDeck")))
        return {"ok": True}

    @app.get("/api/mail")
    async def mail(limit: int):
        assert limit == 1
        return {"connection": {"status": mail_status}}

    @app.post("/api/mail/connection/sync")
    async def sync_mail(request: Request):
        calls.append(("mail", request.headers.get("X-CourseDeck")))
        return {"ok": True}

    with running_service(tmp_path, lambda: app) as service:
        service.sync()
    expected = [("sources", "1")]
    if mail_status == "connected":
        expected.append(("mail", "1"))
    assert calls == expected
    with pytest.raises(StartupError, match="disconnected"):
        service.sync()


@pytest.mark.parametrize("background", [False, True])
def test_duplicate_main_opens_existing_service_without_starting_another(
    tmp_path, monkeypatch, background
):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    opened = Mock()
    monkeypatch.setattr(tray.webbrowser, "open", opened)
    with running_service(tmp_path, FastAPI) as service:
        constructor = Mock(side_effect=AssertionError("Must reuse existing service"))
        monkeypatch.setattr(tray, "TrayService", constructor)
        args = ["--data-dir", str(tmp_path)] + (["--background"] if background else [])
        assert tray.main(args) == 0
        if background:
            opened.assert_not_called()
        else:
            opened.assert_called_once_with(service.url)
        constructor.assert_not_called()


def test_occupied_port_does_not_open_unrelated_service(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    opened = Mock()
    error = Mock()
    constructor = Mock(side_effect=AssertionError("Must reject occupied port before startup"))
    monkeypatch.setattr(tray.webbrowser, "open", opened)
    monkeypatch.setattr(tray, "show_error", error)
    monkeypatch.setattr(tray, "TrayService", constructor)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert tray.main(["--data-dir", str(tmp_path), "--port", str(port)]) == 1
    opened.assert_not_called()
    constructor.assert_not_called()
    error.assert_called_once()
    with instance_lock(tmp_path), listening_socket(port):
        pass


def test_cli_service_lock_is_not_treated_as_a_reusable_tray(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    opened = Mock()
    error = Mock()
    constructor = Mock(side_effect=AssertionError("Must reject existing CLI instance"))
    monkeypatch.setattr(tray.webbrowser, "open", opened)
    monkeypatch.setattr(tray, "show_error", error)
    monkeypatch.setattr(tray, "TrayService", constructor)
    with instance_lock(tmp_path):
        assert tray.main(["--data-dir", str(tmp_path)]) == 1
    opened.assert_not_called()
    constructor.assert_not_called()
    error.assert_called_once()


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_invalid_port_does_not_create_data_directory(tmp_path, monkeypatch, port):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    directory = tmp_path / "unused"
    with pytest.raises(SystemExit) as error:
        tray.main(["--data-dir", str(directory), "--port", port])
    assert error.value.code == 2
    assert not directory.exists()
