import asyncio
import ipaddress
import ssl
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request

from coursedeck import __main__, lan_server, tray
from coursedeck.runtime import listening_socket


class Access:
    def __init__(self, *, enabled=True, host="192.168.1.8"):
        self.config = SimpleNamespace(
            enabled=enabled,
            host=host,
            port=48322,
            certfile="test-cert.pem",
            keyfile="test-key.pem",
        )
        self.active = False
        self.active_url = None
        self.error = None

    def starting(self):
        return self.config if self.config.enabled else None

    def mark_started(self, host, port):
        self.active = True
        self.active_url = f"https://{host}:{port}"

    def mark_stopped(self, error=None):
        self.active = False
        self.active_url = None
        self.error = error


@pytest.fixture
def harness(monkeypatch):
    app = FastAPI()
    app.state.network = Access()
    captured = {"closed": False}

    def config(application, **kwargs):
        captured["app"] = application
        captured["config"] = kwargs
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        captured["tls"] = context
        return SimpleNamespace(load=Mock(), ssl=context)

    @contextmanager
    def socket(port, *, host):
        captured["bind"] = (host, port)
        try:
            yield "mock-socket"
        finally:
            captured["closed"] = True

    class Server:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
            captured["server"] = self

        async def serve(self, *, sockets):
            captured["sockets"] = sockets
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(lan_server.uvicorn, "Config", config)
    monkeypatch.setattr(lan_server, "listening_socket", socket)
    monkeypatch.setattr(lan_server, "_SecondaryServer", Server)
    return app, captured


async def test_disabled_setting_never_creates_tls_config_or_socket(harness):
    app, captured = harness
    app.state.network.config.enabled = False
    async with lan_server.lan_listener(app):
        assert not app.state.network.active
        assert "config" not in captured
        assert "bind" not in captured
    assert app.state.network.error is None


async def test_exact_private_binding_tls_and_single_lifespan_with_shutdown(harness):
    app, captured = harness
    async with lan_server.lan_listener(app):
        assert captured["app"] is app
        assert captured["bind"] == ("192.168.1.8", 48322)
        assert captured["sockets"] == ["mock-socket"]
        assert captured["config"]["lifespan"] == "off"
        assert captured["config"]["proxy_headers"] is False
        assert captured["config"]["access_log"] is False
        assert captured["config"]["ssl_version"] == ssl.PROTOCOL_TLS_SERVER
        assert captured["tls"].minimum_version == ssl.TLSVersion.TLSv1_2
        assert app.state.network.active
        assert app.state.network.active_url == "https://192.168.1.8:48322"
    assert captured["closed"]
    assert captured["server"].should_exit
    assert not app.state.network.active
    assert app.state.network.active_url is None


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "::", "localhost"])
async def test_unsafe_binding_falls_back_without_opening_socket(harness, host):
    app, captured = harness
    app.state.network.config.host = host
    async with lan_server.lan_listener(app):
        assert "bind" not in captured
        assert not app.state.network.active
        assert "could not start" in app.state.network.error


@pytest.mark.parametrize("error", [OSError("secret-path"), SystemExit(1)])
async def test_tls_failure_preserves_local_lifespan_and_redacts_details(
    harness, monkeypatch, error
):
    app, captured = harness

    def config(*args, **kwargs):
        raise error

    monkeypatch.setattr(lan_server.uvicorn, "Config", config)
    async with lan_server.lan_listener(app):
        assert "bind" not in captured
        assert not app.state.network.active
        assert "could not start" in app.state.network.error
        assert "secret-path" not in app.state.network.error


async def test_missing_tls_context_cannot_open_plaintext_socket(harness, monkeypatch):
    app, captured = harness
    monkeypatch.setattr(
        lan_server.uvicorn, "Config", lambda *a, **kw: SimpleNamespace(load=Mock(), ssl=None)
    )
    async with lan_server.lan_listener(app):
        assert "bind" not in captured
        assert not app.state.network.active
        assert "could not start" in app.state.network.error


async def test_bind_failure_preserves_local_lifespan(harness, monkeypatch):
    app, _ = harness

    @contextmanager
    def socket(*args, **kwargs):
        raise OSError("Address unavailable")
        yield

    monkeypatch.setattr(lan_server, "listening_socket", socket)
    async with lan_server.lan_listener(app):
        assert not app.state.network.active
        assert "could not start" in app.state.network.error


@pytest.mark.parametrize("error", [OSError("secret"), SystemExit(3)])
async def test_secondary_server_failure_does_not_exit_application(harness, monkeypatch, error):
    app, captured = harness

    class FailingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self, *, sockets):
            raise error

    monkeypatch.setattr(lan_server, "_SecondaryServer", FailingServer)
    async with lan_server.lan_listener(app):
        assert not app.state.network.active
        assert "LAN listener stopped" in app.state.network.error
        assert "secret" not in app.state.network.error
        assert captured["closed"]


async def test_disabling_closes_secondary_listener_and_keeps_context_usable(harness):
    app, captured = harness
    async with lan_server.lan_listener(app):
        app.state.network.config.enabled = False
        await app.state.lan_server.stop()
        assert captured["closed"]
        assert not app.state.network.active
        assert app.state.lan_server.task.done()


async def test_stalled_startup_times_out_without_blocking_local_lifespan(harness, monkeypatch):
    app, captured = harness
    monkeypatch.setattr(lan_server, "_START_TIMEOUT", 0.01)
    monkeypatch.setattr(lan_server, "_STOP_TIMEOUT", 0.01)

    class StalledServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self, *, sockets):
            await asyncio.Event().wait()

    monkeypatch.setattr(lan_server, "_SecondaryServer", StalledServer)
    async with lan_server.lan_listener(app):
        assert not app.state.network.active
        assert "TimeoutError" in app.state.network.error
        assert captured["closed"]


def test_cli_keeps_loopback_and_disables_proxy_headers(tmp_path, monkeypatch):
    server = Mock()
    monkeypatch.setattr(__main__.uvicorn, "Server", server)
    monkeypatch.setenv("COURSEDECK_DATA_DIR", "unused")
    assert __main__.main(["--data-dir", str(tmp_path), "--port", "0"]) == 0
    config = server.call_args.args[0]
    assert config.host == "127.0.0.1"
    assert config.proxy_headers is False
    assert config.access_log is False


def test_tray_ignores_forged_forwarded_peer(tmp_path):
    app = FastAPI()

    @app.get("/peer")
    def peer(request: Request):
        return {"peer": request.client.host, "scheme": request.url.scheme}

    with listening_socket(0) as sock:
        service = tray.TrayService(tmp_path, sock, app_factory=lambda: app)
        try:
            service.start(timeout=5)
            with httpx.Client(base_url=service.url, trust_env=False) as client:
                response = client.get(
                    "/peer",
                    headers={"X-Forwarded-For": "192.168.1.9", "X-Forwarded-Proto": "https"},
                )
            assert response.json() == {"peer": "127.0.0.1", "scheme": "http"}
        finally:
            service.stop()


async def test_real_tls_listener_on_isolated_loopback_only(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CourseDeck isolated test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "isolated-cert.pem"
    keyfile = tmp_path / "isolated-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    entered = 0
    exited = 0
    port = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal entered, exited
        entered += 1
        try:
            yield
        finally:
            exited += 1

    app = FastAPI(lifespan=lifespan)
    app.state.network = Access()
    app.state.network.config.certfile = str(certfile)
    app.state.network.config.keyfile = str(keyfile)

    @app.get("/peer")
    async def peer(request: Request):
        return {"peer": request.client.host, "scheme": request.url.scheme}

    @contextmanager
    def loopback_socket(requested_port, *, host):
        nonlocal port
        assert (host, requested_port) == ("192.168.1.8", 48322)
        # The production bind call is intercepted before creating any LAN socket.
        with listening_socket(0) as sock:
            assert sock.getsockname()[0] == "127.0.0.1"
            port = sock.getsockname()[1]
            yield sock

    monkeypatch.setattr(lan_server, "listening_socket", loopback_socket)
    try:
        async with app.router.lifespan_context(app), lan_server.lan_listener(app):
            assert app.state.network.active
            assert entered == 1
            tls = ssl.create_default_context(cafile=str(certfile))
            async with httpx.AsyncClient(verify=tls, trust_env=False) as client:
                response = await client.get(
                    f"https://127.0.0.1:{port}/peer",
                    headers={"X-Forwarded-For": "192.168.1.9", "X-Forwarded-Proto": "http"},
                )
                assert response.json() == {"peer": "127.0.0.1", "scheme": "https"}
                negotiated = response.extensions["network_stream"].get_extra_info("ssl_object")
                assert negotiated.version() in {"TLSv1.2", "TLSv1.3"}
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                with pytest.raises(httpx.HTTPError):
                    await client.get(f"http://127.0.0.1:{port}/peer")
        assert exited == 1
        assert not app.state.network.active
        with listening_socket(port):
            pass
    finally:
        keyfile.unlink(missing_ok=True)
        certfile.unlink(missing_ok=True)
