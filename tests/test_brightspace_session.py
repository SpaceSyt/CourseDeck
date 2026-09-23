from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from coursedeck.connectors import brightspace
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Outcome

HOME = "https://school.example/d2l/home"
SSO = "https://login.microsoftonline.com/common/oauth2/authorize"


@pytest.mark.parametrize("step", ["next", "password", "loading"])
async def test_brightspace_sso_continuation(tmp_path, monkeypatch, step):
    db = Database(tmp_path / "tasks.sqlite3")
    db.update_state("brightspace", config={"base_url": "https://school.example"})
    connector = brightspace.BrightspaceConnector(db, None, None)
    original = brightspace.resume_session

    async def bounded(page, ready, **kwargs):
        return await original(page, ready, timeout_ms=1000)

    monkeypatch.setattr(brightspace, "resume_session", bounded)
    connector.validate_session = AsyncMock(return_value=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        goto = page.goto

        async def navigate(url, **kwargs):
            return await goto(SSO if url == HOME else url, **kwargs)

        monkeypatch.setattr(page, "goto", navigate)
        monkeypatch.setattr(context, "new_page", AsyncMock(return_value=page))

        async def route(request):
            if request.request.url == SSO:
                body = (
                    '<input name="loginfmt" type="email" value="student@example.edu">'
                    f'<input id="idSIButton9" type="submit" value="Next" '
                    f"onclick=\"location.href='{HOME}'\">"
                    if step == "next"
                    else '<input type="password">'
                    if step == "password"
                    else "Loading"
                )
            elif request.request.url == HOME:
                body = "<d2l-navigation>Courses</d2l-navigation>"
            else:
                return await request.abort()
            await request.fulfill(content_type="text/html", body=body)

        await context.route("**/*", route)
        try:
            if step == "next":
                await connector.restore_browser_session(context)
                assert page.url == HOME
                connector.validate_session.assert_awaited_once_with(context)
            elif step == "password":
                with pytest.raises(TransportError) as caught:
                    await connector.restore_browser_session(context)
                assert caught.value.outcome == Outcome.AUTH_REQUIRED
                connector.validate_session.assert_not_awaited()
            else:
                with pytest.raises(BrowserTimeout):
                    await connector.restore_browser_session(context)
                connector.validate_session.assert_not_awaited()
        finally:
            await browser.close()
