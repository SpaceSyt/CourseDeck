import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from coursedeck.db import Database
from coursedeck.gmail_actions import trash_threads
from coursedeck.gmail_browser import GmailBrowser

ACCOUNT = "student@example.test"
FIRST = "1111111111111111"
SECOND = "2222222222222222"


def item(key=FIRST, subject="Course update"):
    return {
        "id": "gmail:" + key,
        "subject": subject,
        "content_key": "text-links-v1:" + key,
        "url": "https://untrusted.example/delete-everything",
    }


HTML = """<html><body><header><a id="account"></a></header><main role="main"></main>
<script>
async function render() {
  const info = await fixtureView(location.hash);
  document.querySelector('#account').setAttribute(
    'aria-label', 'Google Account: (' + info.account + ')');
  const main = document.querySelector('main');
  if (info.kind === 'thread') {
    const title = document.createElement('h2');
    title.className = 'hP'; title.textContent = info.subject;
    title.setAttribute('data-legacy-thread-id', info.identity); main.append(title);
    const body = document.createElement('div'); body.className = 'adn';
    body.setAttribute('data-legacy-message-id', info.last_message); main.append(body);
    if (info.control) {
      const button = document.createElement('div'); button.className = 'nX';
      button.setAttribute('role', 'button'); button.setAttribute('tabindex', '0');
      button.title = info.control; button.textContent = info.control;
      button.onclick = async () => {
        await fixtureTrash(info.id);
        main.innerHTML = '<p>Conversations</p>'; button.remove();
      };
      if (info.in_body) { body.classList.add('a3s'); body.append(button); }
      else document.body.append(button);
      if (info.duplicate) document.body.append(button.cloneNode(true));
    }
  } else {
    if (info.redirect) location.hash = 'inbox';
    if (!info.rows.length) main.innerHTML = '<p>No matches</p><p>Try a different search</p>';
    else {
      const table = document.createElement('table'); main.append(table);
      for (const row of info.rows) {
        const tr = document.createElement('tr'); tr.className = 'zA'; table.append(tr);
        const td = document.createElement('td'); tr.append(td);
        const subject = document.createElement('span'); subject.className = 'bog';
        subject.textContent = row.subject; subject.setAttribute('data-legacy-thread-id', row.id);
        td.append(subject);
      }
    }
  }
}
render();
</script></body></html>"""


@pytest.fixture
async def mailbox(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    db.update_state("gmail", account=ACCOUNT, authorized=True)
    gmail = GmailBrowser(db, tmp_path)
    monkeypatch.setattr("coursedeck.gmail_actions.ELEMENT_TIMEOUT", 1200)
    state = SimpleNamespace(
        account=ACCOUNT,
        threads={FIRST: "Course update", SECOND: "Second update"},
        trashed=set(),
        clicks=[],
        routes=[],
        searches=[],
        contexts=[],
        identities={},
        latest={},
        controls={},
        duplicate=False,
        in_body=False,
        discard_click=False,
        hide_results=False,
        account_after_click=None,
        wait_on_click=None,
        search_redirect=False,
    )
    original = gmail.browser.session

    @asynccontextmanager
    async def session(timezone=None):
        async with original(timezone) as context:
            state.contexts.append(context)

            async def view(fragment):
                if fragment.startswith("#all/"):
                    key = fragment.removeprefix("#all/")
                    return {
                        "kind": "thread",
                        "id": key,
                        "identity": state.identities.get(key, key),
                        "subject": state.threads[key],
                        "account": state.account,
                        "last_message": state.latest.get(key, key),
                        "control": state.controls.get(
                            key, "Delete forever" if key in state.trashed else "Delete"
                        ),
                        "duplicate": state.duplicate,
                        "in_body": state.in_body,
                    }
                state.searches.append(fragment)
                return {
                    "kind": "search",
                    "account": state.account,
                    "redirect": state.search_redirect,
                    "rows": []
                    if state.hide_results
                    else [{"id": key, "subject": state.threads[key]} for key in state.trashed],
                }

            async def trash(key):
                state.clicks.append(key)
                if state.wait_on_click is not None:
                    await state.wait_on_click.wait()
                if not state.discard_click:
                    state.trashed.add(key)
                if state.account_after_click:
                    state.account = state.account_after_click

            await context.expose_function("fixtureView", view)
            await context.expose_function("fixtureTrash", trash)

            async def serve(route):
                state.routes.append(route.request.url)
                await route.fulfill(body=HTML, content_type="text/html; charset=utf-8")

            await context.route("https://mail.google.com/**", serve)
            yield context

    gmail.browser.session = session
    try:
        yield gmail, state
    finally:
        if state.wait_on_click is not None:
            state.wait_on_click.set()
        await gmail.browser.close()


async def test_confirmed_threads_move_to_trash_and_verify_ids(mailbox):
    gmail, state = mailbox
    result = await trash_threads(gmail, ACCOUNT, [item(), item(SECOND, "Second update")])
    assert result == {
        "account": ACCOUNT,
        "results": [
            {"id": "gmail:" + FIRST, "status": "trashed"},
            {"id": "gmail:" + SECOND, "status": "trashed"},
        ],
    }
    assert state.clicks == [FIRST, SECOND]
    assert all(url.startswith("https://mail.google.com/mail/u/0/") for url in state.routes)
    assert all(fragment.startswith("#search/in%3Atrash") for fragment in state.searches)
    assert not state.contexts[-1].pages and not gmail.lock.locked()


async def test_already_trashed_never_clicks_delete_forever(mailbox):
    gmail, state = mailbox
    state.trashed.add(FIRST)
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"] == [{"id": "gmail:" + FIRST, "status": "already_trashed"}]
    assert state.clicks == []


@pytest.mark.parametrize(
    "mismatch", ["authorized", "visible", "identity", "subject", "new_message"]
)
async def test_identity_changes_never_click_trash(mailbox, mismatch):
    gmail, state = mailbox
    if mismatch == "authorized":
        gmail.db.update_state("gmail", account="other@example.test")
    elif mismatch == "visible":
        state.account = "other@example.test"
    elif mismatch == "identity":
        state.identities[FIRST] = SECOND
    elif mismatch == "subject":
        state.threads[FIRST] = "Changed subject"
    else:
        state.latest[FIRST] = SECOND
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "failed"
    assert state.clicks == [] and not gmail.lock.locked()
    if mismatch == "authorized":
        assert not state.contexts


async def test_partial_batch_continues_after_one_refused_thread(mailbox):
    gmail, state = mailbox
    state.identities[FIRST] = SECOND
    result = await trash_threads(gmail, ACCOUNT, [item(), item(SECOND, "Second update")])
    assert [row["status"] for row in result["results"]] == ["failed", "trashed"]
    assert state.clicks == [SECOND]


@pytest.mark.parametrize(
    "content_key", [None, "", "text-links-v1:", FIRST, "text-links-v1:invalid"]
)
async def test_missing_or_invalid_latest_message_requires_new_preview_without_clicking(
    mailbox, content_key
):
    gmail, state = mailbox
    selected = item() | {"content_key": content_key}
    result = await trash_threads(gmail, ACCOUNT, [selected])
    assert result["results"][0]["status"] == "failed"
    assert "Sync Gmail and preview" in result["results"][0]["error"]
    assert state.clicks == [] and state.routes == []


async def test_click_without_verified_membership_is_unverified_not_retried(mailbox):
    gmail, state = mailbox
    state.discard_click = True
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "unverified"
    assert state.clicks == [FIRST]


async def test_redirected_search_rows_cannot_prove_trash_membership(mailbox):
    gmail, state = mailbox
    state.search_redirect = True
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "unverified"
    assert "Trash search" in result["results"][0]["error"]
    assert state.clicks == [FIRST]


async def test_account_change_after_click_stops_remaining_batch(mailbox):
    gmail, state = mailbox
    state.account_after_click = "other@example.test"
    result = await trash_threads(gmail, ACCOUNT, [item(), item(SECOND, "Second update")])
    assert [row["status"] for row in result["results"]] == ["unverified", "failed"]
    assert state.clicks == [FIRST]


@pytest.mark.parametrize("mode", ["duplicate", "in_body"])
async def test_ambiguous_or_email_body_buttons_are_never_clicked(mailbox, mode):
    gmail, state = mailbox
    setattr(state, mode, True)
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "failed"
    assert state.clicks == []


async def test_cancel_after_click_closes_session_and_releases_shared_lock(mailbox):
    gmail, state = mailbox
    state.wait_on_click = asyncio.Event()
    operation = asyncio.create_task(trash_threads(gmail, ACCOUNT, [item()]))
    async with asyncio.timeout(10):
        while not state.clicks:
            await asyncio.sleep(0.01)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not gmail.lock.locked() and not state.contexts[-1].pages
    assert state.clicks == [FIRST]


async def test_invalid_batch_has_no_browser_or_partial_actions(mailbox):
    gmail, state = mailbox
    for values in ([], [item()] * 2, [item()] * 21, [item("not-a-thread")]):
        with pytest.raises(ValueError):
            await trash_threads(gmail, ACCOUNT, values)
    assert not state.contexts and state.clicks == []


async def test_browser_errors_are_safe_and_no_other_lock_is_released(mailbox, monkeypatch):
    gmail, state = mailbox
    monkeypatch.setattr("coursedeck.gmail_actions.BATCH_TIMEOUT", 0.05)
    await gmail.lock.acquire()
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "failed"
    assert gmail.lock.locked() and not state.contexts
    gmail.lock.release()

    @asynccontextmanager
    async def broken(timezone=None):
        raise ValueError("https://accounts.google.com/login?token=fixture-secret")
        yield

    gmail.browser.session = broken
    result = await trash_threads(gmail, ACCOUNT, [item()])
    assert result["results"][0]["status"] == "failed"
    assert "fixture-secret" not in str(result)
