from unittest.mock import Mock

import httpx
import pytest

from coursedeck import desktop
from coursedeck.app import create_app


@pytest.fixture
async def desktop_client(tmp_path, monkeypatch):
    startup = Mock()
    startup.status.return_value = {"supported": True, "enabled": False}
    startup.set_enabled.return_value = {"supported": True, "enabled": True}
    constructor = Mock(return_value=startup)
    monkeypatch.setattr(desktop, "Autostart", constructor)
    app = create_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:48329"
    ) as client:
        yield client, startup, constructor, tmp_path


async def test_startup_status_never_enables_and_uses_current_data_and_port(desktop_client):
    client, startup, constructor, directory = desktop_client
    response = await client.get("/api/desktop/autostart")
    assert response.json() == {"supported": True, "enabled": False}
    constructor.assert_called_once_with(directory, 48329)
    startup.set_enabled.assert_not_called()


async def test_startup_change_requires_local_mutation_header(desktop_client):
    client, startup, _, _ = desktop_client
    response = await client.put("/api/desktop/autostart", json={"enabled": True})
    assert response.status_code == 403
    startup.set_enabled.assert_not_called()
    response = await client.put(
        "/api/desktop/autostart", json={"enabled": True}, headers={"X-CourseDeck": "1"}
    )
    assert response.status_code == 200
    assert response.json()["enabled"]
    startup.set_enabled.assert_called_once_with(True)


@pytest.mark.parametrize(
    "body", [{"enabled": "false"}, {"enabled": 1}, {"enabled": True, "path": "x"}]
)
async def test_startup_only_accepts_explicit_boolean(desktop_client, body):
    client, startup, _, _ = desktop_client
    response = await client.put("/api/desktop/autostart", json=body, headers={"X-CourseDeck": "1"})
    assert response.status_code == 422
    startup.set_enabled.assert_not_called()


async def test_startup_write_failure_is_visible_and_sanitized(desktop_client):
    client, startup, _, _ = desktop_client
    startup.set_enabled.side_effect = PermissionError("private-user-path")
    response = await client.put(
        "/api/desktop/autostart", json={"enabled": True}, headers={"X-CourseDeck": "1"}
    )
    assert response.status_code == 500
    assert "private-user-path" not in response.text
    assert "Could not change" in response.json()["detail"]


async def test_unsupported_desktop_cannot_enable_startup(desktop_client):
    client, startup, _, _ = desktop_client
    startup.status.return_value = {"supported": False, "enabled": False}
    response = await client.put(
        "/api/desktop/autostart", json={"enabled": True}, headers={"X-CourseDeck": "1"}
    )
    assert response.status_code == 409
    startup.set_enabled.assert_not_called()
