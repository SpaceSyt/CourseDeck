import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from coursedeck.browser import BrowserManager, SessionPersistenceError, SessionSnapshotInvalid
from coursedeck.connectors.dates import source_date
from coursedeck.connectors.gradescope import parse_assignments, parse_courses
from coursedeck.connectors.http import TransportError
from coursedeck.credentials import IncompleteCredentialError
from coursedeck.domain import Outcome


@pytest.fixture(autouse=True)
def session_vault(monkeypatch):
    records = {}
    vault = Mock()
    vault.get.side_effect = records.get
    vault.set.side_effect = lambda key, value: records.__setitem__(key, value)
    vault.delete.side_effect = lambda key: records.pop(key, None)
    monkeypatch.setattr("coursedeck.browser.CredentialStore", lambda _: vault)
    return vault


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
    context.cookies.return_value = []
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


COOKIE = {"name": "session", "value": "synthetic", "domain": "example.com", "path": "/"}


async def test_session_cookie_survives_browser_and_manager_restart(tmp_path, monkeypatch):
    browser = BrowserManager(tmp_path, "gradescope")
    launch = browser.launch

    async def headless_login(headless, timezone=None, *, read_only=False):
        return await launch(True, timezone, read_only=read_only)

    monkeypatch.setattr(browser, "launch", headless_login)
    try:
        await browser.login("data:text/html,<title>Login</title>")
        await browser.interactive.add_cookies([COOKIE])
        await browser.close_login()
    finally:
        await browser.close()
    restarted = BrowserManager(tmp_path, "gradescope")
    try:
        async with restarted.session() as context:
            cookies = await context.cookies()
            assert [(c["name"], c["value"]) for c in cookies] == [("session", "synthetic")]
            assert restarted.has_session_snapshot()
        await restarted.reset()
        async with restarted.session() as context:
            assert await context.cookies() == []
    finally:
        await restarted.close()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_rotated_session_saved_after_failed_or_cancelled_read(tmp_path, failure):
    browser = BrowserManager(tmp_path, "gradescope")
    try:
        with pytest.raises(failure):
            async with browser.session() as context:
                await context.add_cookies([COOKIE | {"value": "rotated"}])
                raise failure()
        async with browser.session() as context:
            assert (await context.cookies())[0]["value"] == "rotated"
            await context.clear_cookies()
        async with browser.session() as context:
            assert await context.cookies() == []
            assert browser.has_session_snapshot()
    finally:
        await browser.close()


async def test_live_newer_cookie_wins_over_saved_snapshot(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    try:
        async with browser.session() as context:
            await context.add_cookies([COOKIE | {"value": "newer", "expires": 2100000000}])
        session_vault.set(
            browser.session_key,
            {"cookies": [COOKIE, COOKIE | {"name": "old-companion"}]},
        )
        async with browser.session() as context:
            assert [(c["name"], c["value"]) for c in await context.cookies()] == [
                ("session", "newer")
            ]
    finally:
        await browser.close()


async def test_vault_failure_closes_context_and_reports_safe_error(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    context = None
    try:
        session_vault.set.side_effect = RuntimeError("fixture-secret-must-not-leak")
        with pytest.raises(SessionPersistenceError, match="OS credential store") as exc:
            async with browser.session() as context:
                await context.add_cookies([COOKIE])
        assert "fixture-secret" not in str(exc.value)
        assert not context.browser.is_connected()
    finally:
        await browser.close()


async def test_unavailable_vault_does_not_launch_an_unrestored_profile(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.get.side_effect = RuntimeError("fixture-secret-must-not-leak")
    with pytest.raises(SessionPersistenceError, match="OS credential store") as exc:
        async with browser.session():
            pytest.fail("A failed secure-store read must not start a different session")
    assert "fixture-secret" not in str(exc.value)
    assert browser.playwright is None
    assert not browser.exists()


async def test_external_login_close_invalidates_previous_snapshot(tmp_path, monkeypatch):
    browser = BrowserManager(tmp_path, "gradescope")
    launch = browser.launch

    async def headless_login(headless, timezone=None, *, read_only=False):
        return await launch(True, timezone, read_only=read_only)

    monkeypatch.setattr(browser, "launch", headless_login)
    try:
        async with browser.session() as context:
            await context.add_cookies([COOKIE])
        await browser.login("data:text/html,<title>Signed out</title>")
        context = browser.interactive
        await context.clear_cookies()
        await context.browser.close()
        async with browser.session() as context:
            assert await context.cookies() == []
            assert browser.has_session_snapshot()
    finally:
        await browser.close()


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_cancelled_vault_worker_finishes_before_profile_release(
    tmp_path, session_vault, monkeypatch, operation
):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.set(browser.session_key, {"cookies": [COOKIE]})
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    method = getattr(session_vault, operation)
    original = method.side_effect

    def blocked(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Test did not release the synthetic vault worker"
        try:
            return original(*args)
        finally:
            finished.set()

    method.side_effect = blocked
    context = AsyncMock()
    context.cookies.return_value = [COOKIE | {"value": "rotated"}]

    async def run():
        if operation == "get":
            await browser.launch(True)
        elif operation == "delete":
            await browser.reset()
        else:
            monkeypatch.setattr(browser, "launch", AsyncMock(return_value=context))
            async with browser.session():
                pass

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            context.close.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        if operation == "set":
            context.close.assert_awaited_once()
            assert session_vault.get(browser.session_key)["cookies"][0]["value"] == "rotated"
            await browser.reset()
            assert session_vault.get(browser.session_key) is None
        elif operation == "get":
            assert browser.playwright is None
        else:
            assert session_vault.get(browser.session_key) is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_pending_logout_snapshot_finishes_before_next_launch(
    tmp_path, session_vault
):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.set(browser.session_key, {"cookies": [COOKIE]})
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = session_vault.set.side_effect

    def blocked(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        original(*args)

    session_vault.set.side_effect = blocked
    browser._pending_session_save = asyncio.create_task(browser._write_session([]))
    task = asyncio.create_task(browser.launch(True))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session_vault.get(browser.session_key) == {"cookies": []}
        assert browser.playwright is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancellation_during_cookie_capture_still_saves_session(
    tmp_path, session_vault, monkeypatch
):
    browser = BrowserManager(tmp_path, "gradescope")
    started, release = asyncio.Event(), asyncio.Event()
    context = AsyncMock()

    async def capture():
        started.set()
        await release.wait()
        return [COOKIE | {"value": "rotated"}]

    context.cookies.side_effect = capture
    monkeypatch.setattr(browser, "launch", AsyncMock(return_value=context))

    async def read():
        async with browser.session():
            raise asyncio.CancelledError()

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            context.close.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session_vault.get(browser.session_key)["cookies"][0]["value"] == "rotated"
        context.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path", ["login", "sync", "failed_restore"])
async def test_cancelled_context_teardown_finishes_before_releasing_profile(
    tmp_path, monkeypatch, path
):
    browser = BrowserManager(tmp_path, "gradescope")
    context = await browser.launch(True)
    started, release = asyncio.Event(), asyncio.Event()
    original = context._impl_obj.request.dispose

    async def dispose(*args, **kwargs):
        started.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(context._impl_obj.request, "dispose", dispose)
    if path == "failed_restore":
        await browser._write_session([])
        monkeypatch.setattr(
            browser.playwright.chromium,
            "launch_persistent_context",
            AsyncMock(return_value=context),
        )
        monkeypatch.setattr(
            "coursedeck.browser.restore_session_cookies",
            AsyncMock(side_effect=RuntimeError("Synthetic restore failure")),
        )
    elif path == "login":
        browser.interactive = context
    else:
        monkeypatch.setattr(browser, "launch", AsyncMock(return_value=context))

    async def teardown():
        if path == "login":
            await browser.close_login()
        elif path == "failed_restore":
            await browser.launch(True)
        else:
            async with browser.session():
                pass

    task = asyncio.create_task(teardown())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert context.browser.is_connected()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not context.browser.is_connected()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if context.browser.is_connected():
            await context.browser.close()
        await browser.close()


async def test_cancelled_driver_stop_completes_before_shutdown_returns(tmp_path):
    browser = BrowserManager(tmp_path, "gradescope")
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    driver = AsyncMock()

    async def stop():
        started.set()
        await release.wait()
        stopped.set()

    driver.stop.side_effect = stop
    browser.playwright = driver
    task = asyncio.create_task(browser.close())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert browser.playwright is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_explicit_login_recovers_incomplete_snapshot_without_resetting_profile(
    tmp_path, session_vault, monkeypatch
):
    browser = BrowserManager(tmp_path, "gradescope")
    original_get, original_delete = session_vault.get.side_effect, session_vault.delete.side_effect
    damaged = [True]

    def read(key):
        if key == browser.session_key and damaged[0]:
            raise IncompleteCredentialError("synthetic missing vault chunk")
        return original_get(key)

    def delete(key):
        assert key == browser.session_key
        damaged[0] = False
        return original_delete(key)

    try:
        async with browser.session() as context:
            await context.add_cookies([COOKIE | {"expires": 2100000000}])
        marker = browser.path / "retained-profile-marker"
        marker.write_text("synthetic")
        data_marker = tmp_path / "retained-cache-marker"
        data_marker.write_text("synthetic")
        session_vault.set("other-source", {"value": "synthetic"})
        session_vault.get.side_effect = read
        session_vault.delete.side_effect = delete
        launch = browser.launch

        async def headless_login(headless, timezone=None, *, read_only=False):
            return await launch(True, timezone, read_only=read_only)

        monkeypatch.setattr(browser, "launch", headless_login)
        await browser.login("data:text/html,<title>Reconnect</title>")
        assert marker.read_text() == data_marker.read_text() == "synthetic"
        assert (await browser.interactive.cookies())[0]["value"] == "synthetic"
        session_vault.delete.assert_called_once_with(browser.session_key)
        assert session_vault.get("other-source") == {"value": "synthetic"}
        await browser.close_login()
        assert session_vault.get(browser.session_key)["cookies"][0]["value"] == "synthetic"
    finally:
        await browser.close()


async def test_background_sync_does_not_discard_incomplete_snapshot(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.get.side_effect = IncompleteCredentialError("synthetic missing vault chunk")
    with pytest.raises(SessionSnapshotInvalid, match="Reconnect this source"):
        async with browser.session():
            pytest.fail("Background sync must not replace a damaged saved identity")
    session_vault.delete.assert_not_called()
    assert browser.playwright is None


async def test_locked_vault_is_not_treated_as_damaged_session(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.get.side_effect = RuntimeError("synthetic locked credential store")
    with pytest.raises(SessionPersistenceError, match="OS credential store") as exc:
        await browser.login("data:text/html,<title>Reconnect</title>")
    assert not isinstance(exc.value, SessionSnapshotInvalid)
    session_vault.delete.assert_not_called()
    assert browser.playwright is None


async def test_damaged_snapshot_recovery_only_retries_once(tmp_path, session_vault):
    browser = BrowserManager(tmp_path, "gradescope")
    session_vault.get.side_effect = IncompleteCredentialError("synthetic persistent damage")
    with pytest.raises(SessionSnapshotInvalid):
        await browser.login("data:text/html,<title>Reconnect</title>")
    session_vault.delete.assert_called_once_with(browser.session_key)
    assert session_vault.get.call_count == 2
    assert browser.playwright is None


@pytest.mark.parametrize("operation", ["read", "restore", "capture", "save", "delete"])
async def test_session_operation_failure_is_specific_safe_and_releases_context(
    tmp_path, session_vault, monkeypatch, operation
):
    browser = BrowserManager(tmp_path, "gradescope")
    context = AsyncMock()
    context.cookies.return_value = [COOKIE]
    failure = RuntimeError("token=SECRET")
    if operation == "read":
        session_vault.get.side_effect = failure
        call = browser.launch(True)
    elif operation == "restore":
        session_vault.set(browser.session_key, {"cookies": [COOKIE]})
        browser.playwright = Mock()
        browser.playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        monkeypatch.setattr(
            "coursedeck.browser.restore_session_cookies", AsyncMock(side_effect=failure)
        )
        call = browser.launch(True)
    elif operation == "delete":
        session_vault.delete.side_effect = failure
        call = browser._delete_session_snapshot()
    else:
        browser.interactive = context
        if operation == "capture":
            context.cookies.side_effect = failure
        else:
            session_vault.set.side_effect = failure
        call = browser.close_login()
    with pytest.raises(SessionPersistenceError) as caught:
        await call
    assert caught.value.category == f"session_{operation}_failed"
    assert caught.value.stage == f"session_{operation}"
    assert "SECRET" not in str(caught.value)
    if operation in {"restore", "capture", "save"}:
        context.close.assert_awaited_once()
    else:
        assert browser.playwright is None
    if operation == "capture":
        session_vault.set.assert_not_called()
    if operation != "delete":
        session_vault.delete.assert_not_called()


@pytest.mark.parametrize("operation", ["capture", "save", "close"])
async def test_cancelled_read_still_cancels_when_session_cleanup_fails(
    tmp_path, session_vault, monkeypatch, operation
):
    browser = BrowserManager(tmp_path, "gradescope")
    context = AsyncMock()
    context.cookies.return_value = [COOKIE]
    if operation == "save":
        session_vault.set.side_effect = RuntimeError("token=SECRET")
    else:
        getattr(
            context, "cookies" if operation == "capture" else "close"
        ).side_effect = RuntimeError("token=SECRET")
    monkeypatch.setattr(browser, "launch", AsyncMock(return_value=context))
    with pytest.raises(asyncio.CancelledError):
        async with browser.session():
            raise asyncio.CancelledError()
    context.close.assert_awaited_once()


async def test_cancelled_save_waits_for_failed_worker_and_preserves_cancellation(
    tmp_path, session_vault, monkeypatch
):
    browser = BrowserManager(tmp_path, "gradescope")
    context = AsyncMock()
    context.cookies.return_value = [COOKIE]
    monkeypatch.setattr(browser, "launch", AsyncMock(return_value=context))
    started, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def fail_save(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        finished.set()
        raise RuntimeError("token=SECRET")

    session_vault.set.side_effect = fail_save

    async def read():
        async with browser.session():
            pass

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        context.close.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        context.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
