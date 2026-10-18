from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.session_cookies import restore_session_cookies


def cookie(name="session", value="saved", **fields):
    return {
        "name": name,
        "value": value,
        "domain": "school.example.test",
        "path": "/",
        "expires": -1,
    } | fields


async def test_restores_session_cookie_lost_on_restart_without_replacing_persistent_cookie():
    existing = cookie("remember", expires=4102444800)
    missing = cookie()
    context = SimpleNamespace(cookies=AsyncMock(return_value=[existing]), add_cookies=AsyncMock())
    await restore_session_cookies(context, [existing, missing])
    context.add_cookies.assert_awaited_once_with([missing])


async def test_rotated_profile_cookie_prevents_mixing_missing_old_session_cookies():
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[cookie("remember", "rotated")]), add_cookies=AsyncMock()
    )
    await restore_session_cookies(context, [cookie("remember"), cookie()])
    context.add_cookies.assert_not_awaited()


async def test_expired_snapshot_cookies_are_not_resurrected():
    context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
    await restore_session_cookies(context, [cookie(expires=1), cookie("valid", expires=4102444800)])
    context.add_cookies.assert_awaited_once_with([cookie("valid", expires=4102444800)])


@pytest.mark.parametrize(
    "fields",
    [{"domain": "other.example.test"}, {"path": "/other"}, {"partitionKey": "https://other.test"}],
)
async def test_cookie_identity_includes_domain_path_and_partition(fields):
    existing = cookie(value="other", **fields)
    context = SimpleNamespace(cookies=AsyncMock(return_value=[existing]), add_cookies=AsyncMock())
    await restore_session_cookies(context, [cookie()])
    context.add_cookies.assert_awaited_once_with([cookie()])
