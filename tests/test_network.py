"""LAN policy tests use synthetic ASGI peers; they never open a LAN socket."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coursedeck import network
from coursedeck.app import create_app
from coursedeck.network import COOKIE, NetworkAccess, NetworkConfig, NetworkPatch

PASSWORD = "synthetic-fixture-password"
LAN = "https://192.168.10.20:48322"
HEADERS = {"X-CourseDeck": "1", "Origin": LAN}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "validate_tls", lambda config: None)
    app = create_app(tmp_path)
    try:
        yield app
    finally:
        app.state.close_readers()


def configured(app):
    access = app.state.network
    access.save(
        NetworkPatch(
            enabled=True,
            host="192.168.10.20",
            port=48322,
            certfile="synthetic-cert",
            keyfile="synthetic-key",
            password=PASSWORD,
        )
    )
    access.mark_started(access.config.host, access.config.port)
    return access


def remote(app, url=LAN, peer="192.168.10.30"):
    return TestClient(app, base_url=url, client=(peer, 50000))


def login(client):
    response = client.post("/api/network/login", headers=HEADERS, json={"password": PASSWORD})
    assert response.status_code == 200
    return response


def test_default_off_without_creating_config_or_secret(workspace, tmp_path):
    client = TestClient(workspace, base_url="http://127.0.0.1")
    status = client.get("/api/network").json()
    assert status == {
        "enabled": False,
        "host": "",
        "port": 48322,
        "certfile": "",
        "keyfile": "",
        "password_set": False,
        "active": False,
        "active_url": None,
        "restart_required": False,
        "error": None,
    }
    assert not (tmp_path / "network.json").exists()
    assert client.get("/api/network/session").json() == {"local": True, "authenticated": True}
    assert remote(workspace).get("/api/snapshot").status_code == 400


def test_save_reload_status_disable_and_session_revocation(workspace, tmp_path):
    access = configured(workspace)
    client = remote(workspace)
    login(client)
    assert client.get("/api/snapshot").status_code == 200
    reloaded = NetworkAccess(tmp_path)
    assert reloaded.config.enabled and reloaded.status()["restart_required"]
    assert not reloaded.active and reloaded.status()["password_set"]
    assert PASSWORD not in (tmp_path / "network.json").read_text()
    assert "password_hash" not in access.status() and "password_salt" not in access.status()
    local = TestClient(workspace, base_url="http://127.0.0.1")
    workspace.state.lan_server = type(
        "Listener", (), {"stop": AsyncMock(side_effect=access.mark_stopped)}
    )()
    response = local.put("/api/network", headers={"X-CourseDeck": "1"}, json={"enabled": False})
    assert response.status_code == 200 and not response.json()["active"]
    workspace.state.lan_server.stop.assert_awaited_once()
    assert not NetworkAccess(tmp_path).config.enabled
    assert client.get("/api/snapshot").status_code == 400
    assert local.get("/api/snapshot").status_code == 200
    access.save(NetworkPatch(enabled=True))
    assert not access.active
    access.mark_started(access.config.host, access.config.port)
    assert client.get("/api/snapshot").status_code == 401


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "true"},
        {"enabled": True},
        {"host": "0.0.0.0"},
        {"host": "8.8.8.8"},
        {"host": "127.0.0.1"},
        {"host": "169.254.1.2"},
        {"host": "localhost"},
        {"host": "::"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"password": "short"},
        {"password_hash": "a" * 64},
        {"enabled": None},
        {"extra": True},
    ],
)
def test_invalid_configuration_is_safe_and_does_not_persist(workspace, tmp_path, patch):
    client = TestClient(workspace, base_url="http://127.0.0.1")
    response = client.put("/api/network", headers={"X-CourseDeck": "1"}, json=patch)
    assert response.status_code == 400
    assert not (tmp_path / "network.json").exists()
    assert not workspace.state.network.config.enabled
    assert "short" not in response.text


@pytest.mark.parametrize("content", ["{", "[]", '{"enabled":true}', '{"port":0}', "x" * 17000])
def test_corrupt_config_falls_back_without_overwriting(tmp_path, content):
    path = tmp_path / "network.json"
    path.write_text(content)
    access = NetworkAccess(tmp_path)
    assert access.error and not access.active and access.starting() is None
    assert path.read_text() == content
    access.save(NetworkPatch(enabled=False))
    assert NetworkAccess(tmp_path).error is None


def test_missing_tls_files_never_enable(tmp_path):
    access = NetworkAccess(tmp_path)
    with pytest.raises(ValueError):
        access.save(
            NetworkPatch(
                enabled=True,
                host="192.168.1.2",
                password=PASSWORD,
                certfile=str(tmp_path / "missing"),
                keyfile=str(tmp_path / "key"),
            )
        )
    assert not access.config.enabled and not access.path.exists()


def test_failed_atomic_save_preserves_previous_config_and_sessions(workspace, monkeypatch):
    access = configured(workspace)
    client = remote(workspace)
    login(client)
    before = access.path.read_bytes()
    monkeypatch.setattr(network.Path, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        access.save(NetworkPatch(enabled=False))
    assert access.active and access.path.read_bytes() == before
    assert client.get("/api/snapshot").status_code == 200


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/snapshot", "GET"),
        ("/api/mail", "GET"),
        ("/api/mail/messages/secret", "GET"),
        ("/api/chat/attachments/private/download", "GET"),
        ("/api/chat/library", "GET"),
        ("/api/chat/conversations", "GET"),
        ("/api/cache/tasks", "GET"),
        ("/api/heartbeat", "GET"),
        ("/api/network", "GET"),
        ("/api/chat/config", "GET"),
        ("/api/sync", "POST"),
        ("/api/tasks/secret/local", "PATCH"),
        ("/api/collection/jobs", "POST"),
        ("/api/sources/gmail/configure", "PUT"),
        ("/api/backup", "POST"),
        ("/docs", "GET"),
        ("/openapi.json", "GET"),
    ],
)
def test_all_private_routes_require_authentication(workspace, path, method):
    configured(workspace)
    response = remote(workspace).request(method, path, headers=HEADERS)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", network.LOCAL_ADMIN)
def test_authenticated_remote_cannot_administer(workspace, path):
    configured(workspace)
    client = remote(workspace)
    login(client)
    for method in ("GET", "PUT", "POST", "DELETE"):
        assert client.request(method, path, headers=HEADERS).status_code == 403


def test_login_cookie_logout_expiry_and_rotated_password(workspace, monkeypatch):
    access = configured(workspace)
    client = remote(workspace)
    assert client.get("/api/network/session").json() == {"local": False, "authenticated": False}
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie and COOKIE in cookie
    assert response.json() == {"ok": True}
    assert client.get("/api/network/session").json()["authenticated"]
    assert client.get("/api/snapshot").status_code == 200
    assert client.post("/api/network/logout", headers=HEADERS).status_code == 200
    assert client.get("/api/snapshot").status_code == 401
    login(client)
    now = network.time.monotonic()
    monkeypatch.setattr(network.time, "monotonic", lambda: now + network.SESSION_SECONDS + 1)
    assert client.get("/api/snapshot").status_code == 401
    login(client)
    access.save(NetworkPatch(password="replacement-fixture-password"))
    assert not access.active
    access.mark_started(access.config.host, access.config.port)
    assert client.get("/api/snapshot").status_code == 401
    assert (
        client.post("/api/network/login", headers=HEADERS, json={"password": PASSWORD}).status_code
        == 401
    )


def test_invalid_login_rate_limit_and_request_size(workspace):
    configured(workspace)
    client = remote(workspace)
    for _ in range(5):
        response = client.post(
            "/api/network/login", headers=HEADERS, json={"password": "wrong-fixture-password"}
        )
        assert response.status_code == 401 and "wrong-fixture-password" not in response.text
    assert (
        client.post("/api/network/login", headers=HEADERS, json={"password": PASSWORD}).status_code
        == 429
    )
    local = TestClient(workspace, base_url="http://127.0.0.1")
    assert (
        local.put("/api/network", headers={"X-CourseDeck": "1"}, content=b"x" * 9000).status_code
        == 413
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "https://192.168.10.20:1234"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_reads_and_writes_rejected_even_after_login(workspace, headers):
    configured(workspace)
    client = remote(workspace)
    login(client)
    for method in ("GET", "POST", "OPTIONS"):
        assert client.request(method, "/api/snapshot", headers=HEADERS | headers).status_code == 403


def test_remote_writes_require_origin_and_custom_header(workspace):
    configured(workspace)
    client = remote(workspace)
    login(client)
    for headers in ({}, {"X-CourseDeck": "1"}, {"Origin": LAN}):
        assert client.post("/api/sync", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "authority",
    ["localhost", "127.0.0.1", "[::1]", "evil.example", "192.168.10.20:1234", "192.168.10.20:bad"],
)
def test_remote_cannot_forge_host_or_forwarded_loopback(workspace, authority):
    configured(workspace)
    client = remote(workspace)
    response = client.get(
        "/api/snapshot",
        headers={
            "Host": authority,
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "http",
            "Forwarded": "for=127.0.0.1;host=localhost;proto=http",
        },
    )
    assert response.status_code in {400, 403}


def test_http_lan_and_unknown_peer_fail_closed(workspace):
    configured(workspace)
    assert remote(workspace, url=LAN.replace("https:", "http:")).get("/").status_code == 400
    assert remote(workspace, url="http://127.0.0.1", peer="unknown").get("/").status_code == 403
    assert remote(workspace, peer="203.0.113.9").get("/").status_code == 400
    local = TestClient(workspace, base_url="http://127.0.0.1")
    assert local.get("/api/snapshot", headers={"Origin": "http://evil.example"}).status_code == 403
    assert (
        local.get(
            "/api/oauth/missing/callback", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 404
    )


def test_duplicate_host_and_origin_headers_rejected(workspace):
    configured(workspace)
    client = remote(workspace)
    assert (
        client.get(
            "/", headers=[("host", "192.168.10.20:48322"), ("host", "localhost")]
        ).status_code
        == 400
    )
    assert client.get("/", headers=[("origin", LAN), ("origin", LAN)]).status_code == 403


def test_websockets_fail_closed(workspace):
    configured(workspace)
    with pytest.raises(WebSocketDisconnect) as failure:
        with remote(workspace).websocket_connect("/api/chat"):
            pytest.fail("No WebSocket endpoint should bypass authentication")
    assert failure.value.code == 1008


def test_remote_snapshot_omits_source_configuration(workspace):
    configured(workspace)
    client = remote(workspace)
    login(client)
    payload = client.get("/api/snapshot").json()
    assert payload["sources"]
    assert all(
        source["configuration_values"] == {} and source["configuration_fields"] == []
        for source in payload["sources"]
    )


@pytest.mark.parametrize("recovering", ["busy", "interrupted"])
def test_owner_can_disable_lan_during_database_recovery(workspace, recovering):
    access = configured(workspace)
    client = remote(workspace)
    login(client)
    if recovering == "busy":
        workspace.state.maintenance_gate.busy = True
    else:
        workspace.state.recovery.journal.write_text("{}")
    local = TestClient(workspace, base_url="http://127.0.0.1")
    assert local.get("/api/snapshot").status_code == 503
    assert local.get("/api/network").status_code == 200
    assert local.get("/api/network/session").status_code == 200
    assert client.get("/api/recovery/status").status_code == 200
    assert client.post("/api/recovery/restore", headers=HEADERS).status_code == 403
    response = local.put("/api/network", headers={"X-CourseDeck": "1"}, json={"enabled": False})
    assert response.status_code == 200 and not access.active


async def test_stream_revocation_drops_later_private_bytes(tmp_path):
    access = NetworkAccess(tmp_path)
    access.config = NetworkConfig(
        enabled=True,
        host="192.168.10.20",
        port=48322,
        certfile="fixture",
        keyfile="fixture",
        password_salt="0" * 32,
        password_hash="0" * 64,
    )
    access.mark_started(access.config.host, access.config.port)
    token = "a" * 43
    access._sessions[hashlib.sha256(token.encode()).hexdigest()] = network.time.monotonic() + 60
    messages = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        access.mark_stopped()
        await send(
            {"type": "http.response.body", "body": b"private later bytes", "more_body": True}
        )
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    scope = {
        "type": "http",
        "scheme": "https",
        "method": "GET",
        "path": "/api/stream",
        "client": ("192.168.10.30", 50000),
        "server": ("192.168.10.20", 48322),
        "headers": [(b"host", b"192.168.10.20:48322"), (b"cookie", f"{COOKIE}={token}".encode())],
    }
    await network.NetworkBoundary(downstream, access)(
        scope, AsyncMock(), AsyncMock(side_effect=messages.append)
    )
    assert b"".join(message.get("body", b"") for message in messages) == b"first"
    assert messages[-1].get("more_body", False) is False
