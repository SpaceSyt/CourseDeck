from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coursedeck.browser import BrowserManager
from coursedeck.connectors.dates import source_date
from coursedeck.connectors.gradescope import parse_assignments, parse_courses
from coursedeck.connectors.http import TransportError
from coursedeck.domain import Outcome


def test_gradescope_parser():
    folder = Path(__file__).parent / "fixtures/gradescope"
    courses = parse_courses((folder / "account.html").read_text(), "https://www.gradescope.com")
    tasks, warnings = parse_assignments((folder / "assignments.html").read_text(), courses[0])
    assert len(tasks) == 2 and len(warnings) == 1
    assert tasks[0].id == "gradescope:101:456"
    assert tasks[0].due_at < tasks[0].closes_at
    assert tasks[0].submission_status == "open"
    assert tasks[1].graded and tasks[1].score == 0
    assert tasks[1].url.endswith("/submissions/9")


def test_login_and_unknown_markup_fail_closed():
    with pytest.raises(TransportError) as exc:
        parse_courses('<input type="password">', "https://www.gradescope.com")
    assert exc.value.outcome == Outcome.AUTH_REQUIRED
    with pytest.raises(TransportError) as exc:
        parse_courses("<h1>Server error</h1>", "https://www.gradescope.com")
    assert exc.value.outcome == Outcome.PARSE_ERROR


@pytest.mark.parametrize("value", ["2026-11-01T01:30:00", "2026-03-08T02:30:00"])
def test_ambiguous_and_nonexistent_dates_rejected(value):
    with pytest.raises(ValueError):
        source_date(value, "America/New_York")


@pytest.mark.parametrize("provider", ["gradescope", "rephactor"])
async def test_profile_reset_confined_to_provider(tmp_path, provider):
    browser = BrowserManager(tmp_path, provider)
    browser.path.mkdir(parents=True)
    (browser.path / "cookie-fixture").write_text("synthetic")
    other = tmp_path / "personal"
    other.mkdir()
    await browser.reset()
    assert other.exists() and not browser.exists()
    browser.path = other
    with pytest.raises(ValueError):
        await browser.reset()


async def test_closed_login_window_allows_background_sync(tmp_path, monkeypatch):
    browser = BrowserManager(tmp_path, "gradescope")
    launch = browser.launch

    async def headless_login(headless, timezone=None, *, read_only=False):
        return await launch(True, timezone, read_only=read_only)

    monkeypatch.setattr(browser, "launch", headless_login)
    try:
        await browser.login("data:text/html,<title>Fixture login</title>")
        context = browser.interactive
        assert context is not None
        with pytest.raises(ValueError, match="Finish interactive login"):
            async with browser.session():
                pytest.fail("An open login window must retain exclusive profile access")

        # Closing Chromium externally must work without calling close_login().
        async with context.expect_event("close"):
            await context.browser.close()
        assert browser.interactive is None
        async with browser.session(read_only=True) as background:
            page = background.pages[0]
            await page.goto("data:text/html,<title>Background sync</title>")
            assert await page.title() == "Background sync"

        await browser.login("data:text/html,<title>Reconnect</title>")
        assert await browser.interactive.pages[0].title() == "Reconnect"
    finally:
        await browser.close()
    assert browser.interactive is None and browser.playwright is None


async def test_shutdown_releases_driver_when_login_close_fails(tmp_path):
    browser = BrowserManager(tmp_path, "gradescope")
    context = AsyncMock()
    context.close.side_effect = RuntimeError("synthetic close failure")
    driver = AsyncMock()
    browser.interactive = context
    browser.playwright = driver
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await browser.close()
    driver.stop.assert_awaited_once()
    assert browser.interactive is None and browser.playwright is None


async def test_failed_driver_shutdown_does_not_reuse_stopped_driver(tmp_path):
    browser = BrowserManager(tmp_path, "gradescope")
    driver = AsyncMock()
    driver.stop.side_effect = RuntimeError("synthetic driver failure")
    browser.playwright = driver
    with pytest.raises(RuntimeError, match="synthetic driver failure"):
        await browser.close()
    assert browser.playwright is None
    await browser.close()
    driver.stop.assert_awaited_once()
