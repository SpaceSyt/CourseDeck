import json

import pytest
from playwright.async_api import Locator, async_playwright
from pydantic import ValidationError

from coursedeck.connectors.webassign import WebAssignConnector
from coursedeck.db import Database
from coursedeck.login_workflow import LoginWorkflow, run_login_workflow

ENTRY = "https://account.cengage.com/login"
READY = "https://www.webassign.net/student"


@pytest.fixture
async def login_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        pages = {READY: '<main id="assignments">Assignments</main>'}
        clicks = []
        await page.expose_function("recordClick", lambda: clicks.append("clicked"))
        await context.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html", body=pages.get(route.request.url, "Loading")
            ),
        )
        try:
            yield page, pages, clicks
        finally:
            await browser.close()


async def ready(page):
    return page.url == READY and bool(await page.locator("#assignments").count())


def button(*, redirect=True, name="Continue"):
    destination = f"location.href={json.dumps(READY)};" if redirect else ""
    return f"<button onclick='window.recordClick();{destination}'>{name}</button>"


async def run(page, *, interactive=True, **config):
    workflow = LoginWorkflow(timeout_seconds=1, **config)
    return await run_login_workflow(page, workflow, ready, interactive=interactive)


async def test_continuation_waits_for_readable_destination(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = button()
    await page.goto(ENTRY)
    progress = await run(page)
    assert progress.category == "ready"
    assert clicks == ["clicked"]


async def test_destination_url_without_protected_dom_is_not_login_success(login_page):
    page, pages, clicks = login_page
    pages[READY] = "Loading"
    await page.goto(READY)
    assert (await run(page)).category == "redirect_timeout"
    assert clicks == []


@pytest.mark.parametrize("interactive", [True, False])
async def test_static_password_value_is_not_password_manager_autofill(login_page, interactive):
    page, pages, clicks = login_page
    pages[ENTRY] = '<input type="password" value="fixture-secret">' + button()
    await page.goto(ENTRY)
    progress = await run(page, interactive=interactive)
    assert progress.category == "waiting_credentials"
    assert "fixture-secret" not in str(progress)
    assert clicks == []


@pytest.mark.parametrize("host", ["account.cengage.com", "school-idp.example.test"])
@pytest.mark.parametrize("field", ['autocomplete="username"', 'type="email"'])
async def test_username_only_login_requires_user_without_browser_autofill(login_page, host, field):
    page, pages, clicks = login_page
    url = f"https://{host}/login"
    pages[url] = f"<input {field}>" + button()
    await page.goto(url)
    assert (await run(page)).category == "waiting_credentials"
    assert not clicks


async def test_browser_autofilled_username_can_continue_without_reading_value(
    login_page, monkeypatch
):
    page, pages, clicks = login_page
    pages[ENTRY] = '<input autocomplete="username">' + button()
    await page.goto(ENTRY)
    original = Locator.evaluate

    async def evaluate(locator, expression, *args, **kwargs):
        if expression == "e => e.matches(':autofill') || e.matches(':-webkit-autofill')":
            return True
        return await original(locator, expression, *args, **kwargs)

    def no_value_read(*args, **kwargs):
        pytest.fail("Workflow must not read credential input values")

    monkeypatch.setattr(Locator, "evaluate", evaluate)
    monkeypatch.setattr(Locator, "input_value", no_value_read)
    assert (await run(page, interactive=False)).category == "waiting_credentials"
    assert not clicks
    assert (await run(page)).category == "ready"
    assert clicks == ["clicked"]


@pytest.mark.parametrize("host", ["account.cengage.com", "school-idp.example.test"])
async def test_mfa_is_left_for_user_even_on_school_idp(login_page, host):
    page, pages, clicks = login_page
    url = f"https://{host}/login"
    pages[url] = '<input autocomplete="one-time-code">' + button()
    await page.goto(url)
    assert (await run(page)).category == "waiting_mfa"
    assert clicks == []


async def test_permission_request_is_not_accepted(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = button(name="Allow") + button()
    await page.goto(ENTRY)
    assert (await run(page)).category == "waiting_permission"
    assert clicks == []


async def test_unknown_domain_and_cross_origin_forms_are_not_clicked(login_page):
    page, pages, clicks = login_page
    unknown = "https://account.cengage.com.example.test/login"
    pages[unknown] = button()
    await page.goto(unknown)
    assert (await run(page)).category in {"unknown_redirect", "redirect_timeout"}
    pages[ENTRY] = '<form action="https://other.example.test/collect">' + button() + "</form>"
    await page.goto(ENTRY)
    assert (await run(page)).category == "waiting_user"
    assert clicks == []


async def test_stuck_login_is_not_repeatedly_submitted(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = button(redirect=False)
    await page.goto(ENTRY)
    assert (await run(page)).category == "redirect_timeout"
    assert clicks == ["clicked"]


async def test_delayed_redirect_is_waited_without_resubmission(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = button(redirect=False).replace(
        "window.recordClick();",
        f"window.recordClick();setTimeout(()=>location.href={json.dumps(READY)},450);",
    )
    await page.goto(ENTRY)
    progress = await run_login_workflow(
        page, LoginWorkflow(timeout_seconds=3), ready, interactive=True
    )
    assert progress.category == "ready"
    assert clicks == ["clicked"]


async def test_navigation_during_control_inspection_rechecks_destination(login_page, monkeypatch):
    page, pages, clicks = login_page
    pages[ENTRY] = button(redirect=False)
    await page.goto(ENTRY)
    original = Locator.is_enabled

    async def enabled_then_redirect(locator, *args, **kwargs):
        enabled = await original(locator, *args, **kwargs)
        await page.goto(READY)
        return enabled

    monkeypatch.setattr(Locator, "is_enabled", enabled_then_redirect)
    progress = await run_login_workflow(
        page, LoginWorkflow(timeout_seconds=3), ready, interactive=True
    )
    assert progress.category == "ready"
    assert not clicks


@pytest.mark.parametrize(
    "path", ["/assignments/login", "/login/submission", "/%61ssignments/login"]
)
async def test_default_workflow_never_clicks_coursework_login_lookalikes(login_page, path):
    page, pages, clicks = login_page
    url = "https://www.webassign.net" + path
    pages[url] = button()
    await page.goto(url)
    assert (await run(page)).category != "ready"
    assert not clicks


@pytest.mark.parametrize(
    "field", ['type="tel"', 'type="number"', 'name="idvPin"', 'name="backupCodePin"']
)
async def test_verification_fields_never_submit_saved_codes(login_page, field):
    page, pages, clicks = login_page
    pages[ENTRY] = f'<input {field} value="123456">' + button()
    await page.goto(ENTRY)
    assert (await run(page)).category == "waiting_mfa"
    assert not clicks


async def test_autofill_submission_uses_boolean_state_not_password_value(login_page, monkeypatch):
    page, pages, clicks = login_page
    pages[ENTRY] = '<input type="password">' + button()
    await page.goto(ENTRY)
    original = Locator.evaluate

    async def evaluate(locator, expression, *args, **kwargs):
        if expression == "e => e.matches(':autofill') || e.matches(':-webkit-autofill')":
            return True  # Browser-managed state cannot be manufactured by fixture HTML.
        return await original(locator, expression, *args, **kwargs)

    def no_value_read(*args, **kwargs):
        pytest.fail("Workflow must not read credential input values")

    monkeypatch.setattr(Locator, "evaluate", evaluate)
    monkeypatch.setattr(Locator, "input_value", no_value_read)
    assert (await run(page)).category == "ready"
    assert clicks == ["clicked"]


async def test_custom_wait_then_continue_uses_declared_controls(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = '<main id="loaded">Ready</main>' + button()
    await page.goto(ENTRY)
    progress = await run(
        page,
        steps=[
            {
                "host": "account.cengage.com",
                "path": "/login",
                "action": "wait",
                "selector": "#loaded",
            },
            {
                "host": "account.cengage.com",
                "path": "/login",
                "action": "continue",
                "selector": "button",
            },
        ],
    )
    assert progress.category == "ready" and clicks == ["clicked"]


async def test_two_custom_steps_at_same_login_path_are_distinct(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = (
        '<button id="first" onclick="window.recordClick();this.hidden=true;'
        "document.querySelector('#second').hidden=false\">Continue</button>"
        + button().replace("<button", '<button id="second" hidden')
    )
    await page.goto(ENTRY)
    workflow = LoginWorkflow(
        timeout_seconds=3,
        steps=[
            {
                "host": "account.cengage.com",
                "path": "/login",
                "action": "continue",
                "selector": "#first",
            },
            {
                "host": "account.cengage.com",
                "path": "/login",
                "action": "continue",
                "selector": "#second",
            },
        ],
    )
    progress = await run_login_workflow(page, workflow, ready, interactive=True)
    assert progress.category == "ready" and clicks == ["clicked", "clicked"]


async def test_school_idp_can_redirect_without_automatic_clicks(login_page):
    page, pages, clicks = login_page
    url = "https://school-idp.example.test/sso"
    pages[url] = f"<script>setTimeout(()=>location.href={json.dumps(READY)},350)</script>"
    await page.goto(url)
    progress = await run_login_workflow(page, LoginWorkflow(timeout_seconds=3), ready)
    assert progress.category == "ready" and clicks == []


async def test_same_origin_course_submission_form_is_not_login(login_page):
    page, pages, clicks = login_page
    pages[ENTRY] = '<form action="/submission">' + button() + "</form>"
    await page.goto(ENTRY)
    assert (await run(page)).category == "waiting_user"
    assert clicks == []


@pytest.mark.parametrize(
    "overlay,category",
    [
        ('<input autocomplete="one-time-code">', "waiting_mfa"),
        ("<h2>Permissions requested</h2>", "waiting_permission"),
    ],
)
async def test_protected_page_with_challenge_overlay_is_not_ready(login_page, overlay, category):
    page, pages, clicks = login_page
    pages[READY] += overlay
    await page.goto(READY)
    assert (await run(page)).category == category
    assert not clicks


@pytest.mark.parametrize(
    "config",
    [
        {"entry_url": "https://account.cengage.com/login?token=fixture-secret"},
        {"entry_url": "https://user:fixture-secret@account.cengage.com/login"},
        {"entry_url": "https://other.example.test/login"},
        {"entry_url": "https://account.cengage.com/logout"},
        {"password": "fixture-secret"},
        {"steps": [{"host": "account.cengage.com", "path": "/login", "action": "fill"}]},
        {"steps": [{"host": "www.webassign.net", "path": "/assignment", "action": "continue"}]},
        {
            "steps": [
                {"host": "account.cengage.com", "path": "/login?token=secret", "action": "wait"}
            ]
        },
        {
            "steps": [
                {
                    "host": "account.cengage.com",
                    "path": "/login",
                    "action": "continue",
                    "selector": 'input[value="fixture-secret"]',
                }
            ]
        },
    ],
)
def test_workflow_rejects_credentials_and_arbitrary_actions(config):
    with pytest.raises(ValidationError):
        LoginWorkflow(**config)


async def test_webassign_mfa_retains_window_then_finish_saves_verified_session(
    login_page, tmp_path
):
    page, pages, clicks = login_page
    pages[ENTRY] = '<input autocomplete="one-time-code">' + button()
    protected = "https://www.webassign.net/student?class=1&UserPass=synthetic-session"
    pages[protected] = (
        '<main id="js-student-myAssignmentsPage"><h1>My Assignments</h1>'
        '<button role="tab">All assignments</button></main>'
    )

    class Manager:
        interactive = page.context

        async def login(self, url, timezone):
            await page.goto(url)

        async def close_login(self):
            self.interactive = None

    class Vault:
        def __init__(self):
            self.values = {}

        def set(self, name, value):
            self.values[name] = value

    db = Database(tmp_path / "db")
    manager, vault = Manager(), Vault()
    connector = WebAssignConnector(db, manager, vault)
    await connector.configure({"login_workflow": {"entry_url": ENTRY, "timeout_seconds": 1}})
    response = await connector.connect()
    assert response["category"] == "waiting_mfa"
    assert manager.interactive is not None and not vault.values and not clicks
    assert not db.state("webassign").get("authorized")
    await page.goto(protected)  # The user completes verification in the same source window.
    response = await connector.finish_login()
    assert response["category"] == "ready"
    assert db.state("webassign")["authorized"] is True
    assert manager.interactive is None
    assert vault.values["webassign_session"]["url"] == protected
    assert "synthetic-session" not in str(db.state("webassign"))


async def test_invalid_workflow_configuration_does_not_replace_existing_settings(tmp_path):
    db = Database(tmp_path / "db")
    db.update_state("webassign", config={"timezone": "America/New_York"})
    source = WebAssignConnector(db, None, object())
    with pytest.raises(ValueError):
        await source.configure(
            {"timezone": "UTC", "login_workflow": {"password": "fixture-secret"}}
        )
    assert db.state("webassign")["config"] == {"timezone": "America/New_York"}
