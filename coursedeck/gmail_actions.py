"""Move explicitly confirmed Gmail conversations to Trash through the visible UI."""

import asyncio
import re
import unicodedata
from urllib.parse import quote, unquote_plus, urlsplit

from .gmail_browser import ROWS, VISIBLE_ROWS, GmailBrowser, gmail_url

MAX_THREADS = 20
BATCH_TIMEOUT = 240
THREAD_TIMEOUT = 40
ELEMENT_TIMEOUT = 10000
SEARCH_PAGES = 3
THREAD_HEADING = '[role="main"]:visible h2.hP[data-legacy-thread-id]:visible'
DELETE_BUTTON = 'div.nX[role="button"][title="Delete"]:visible'


class _Refused(ValueError):
    pass


class _AccountChanged(_Refused):
    pass


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _authorized(gmail, account):
    state = gmail.db.state("gmail")
    if not state.get("authorized") or (state.get("account") or "").casefold() != account:
        raise _AccountChanged("The authorized Gmail account changed. Review the selection again.")


async def _account(gmail, page, account):
    _authorized(gmail, account)
    if not gmail_url(page.url) or await gmail.account(page) != account:
        raise _AccountChanged("The open Gmail account does not match the confirmed account.")


async def _open(gmail, page, account, fragment):
    # A new document prevents stale inbox/search rows from proving Trash membership.
    await page.goto("about:blank")
    await page.goto(
        "https://mail.google.com/mail/u/0/#" + fragment,
        wait_until="domcontentloaded",
        timeout=20000,
    )
    await gmail._resume_session(page)
    await page.locator('[role="main"]:visible').first.wait_for(timeout=ELEMENT_TIMEOUT)
    await _account(gmail, page, account)


async def _thread(gmail, page, account, item):
    _latest_message(item)
    await _open(gmail, page, account, "all/" + item["remote_id"])
    await page.locator(THREAD_HEADING).wait_for(timeout=ELEMENT_TIMEOUT)
    await _identity(page, item)


def _latest_message(item):
    match = re.fullmatch(r"text-links-v1:([0-9a-f]{10,32})", item.get("content_key") or "")
    if not match:
        raise _Refused(
            "The latest Gmail message cannot be verified. "
            "Sync Gmail and preview the selection again."
        )
    return match[1]


async def _identity(page, item):
    heading = page.locator(THREAD_HEADING)
    if (
        await heading.count() != 1
        or await heading.get_attribute("data-legacy-thread-id") != item["remote_id"]
        or _text(await heading.inner_text()) != _text(item["subject"])
    ):
        raise _Refused("Gmail conversation identity or subject changed. Review it again.")
    latest = _latest_message(item)
    ids = await page.locator('[role="main"]:visible .adn[data-legacy-message-id]').evaluate_all(
        "els => els.map(e => e.getAttribute('data-legacy-message-id'))"
    )
    if not ids or ids[-1] != latest:
        raise _Refused("The Gmail conversation has changed since confirmation. Review it again.")


async def _in_trash(gmail, page, account, item):
    # Only words from the confirmed subject enter this fixed search. Gmail search
    # operators cannot be injected, and matching rows must still have the exact ID.
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", item["subject"])))[:5]
    query = "in:trash" + "".join(' subject:"' + word[:60] + '"' for word in words)
    fragment = "search/" + quote(query, safe="")
    await _open(gmail, page, account, fragment)
    for index in range(SEARCH_PAGES):
        await page.wait_for_function(
            """() => [...document.querySelectorAll('tr.zA')].some(e=>e.getClientRects().length) ||
            [...document.querySelectorAll('[role="main"]')].some(e=>e.getClientRects().length &&
              /(?:No matches|No conversations found[.]|No conversations in Trash[.])/
                .test(e.innerText))
            """,
            timeout=ELEMENT_TIMEOUT,
        )
        await _account(gmail, page, account)
        if not re.fullmatch(
            re.escape(unquote_plus(fragment)) + r"(?:/p\d+)?",
            unquote_plus(urlsplit(page.url).fragment),
        ):
            raise _Refused("Gmail did not remain in the requested Trash search.")
        rows = await page.locator(VISIBLE_ROWS).evaluate_all(ROWS)
        found = [row for row in rows if row["id"] == item["remote_id"]]
        if found:
            return len(found) == 1 and _text(found[0]["subject"]) == _text(item["subject"])
        older = page.get_by_role("button", name="Older", exact=True)
        if (
            not rows
            or await older.count() != 1
            or not await older.is_enabled()
            or await older.get_attribute("aria-disabled") == "true"
        ):
            return False
        if index + 1 == SEARCH_PAGES:
            return False
        previous = [row["id"] for row in rows]
        await older.click(timeout=ELEMENT_TIMEOUT)
        await page.wait_for_function(
            """previous => {
              const ids = [...document.querySelectorAll('tr.zA')]
                .filter(e=>e.getClientRects().length)
                .map(e=>e.querySelector('[data-legacy-thread-id]')
                  ?.getAttribute('data-legacy-thread-id'));
              return ids.length && JSON.stringify(ids) !== JSON.stringify(previous);
            }""",
            arg=previous,
            timeout=ELEMENT_TIMEOUT,
        )
    return False


async def trash_threads(gmail: GmailBrowser, expected_account: str, items: list[dict]) -> dict:
    """Apply a caller-confirmed list; never permanently delete or modify local mail flags.

    Opening a thread can mark it read in Gmail. A click without verified Trash
    membership returns unverified and must not be automatically repeated.
    """
    if (
        not isinstance(expected_account, str)
        or not expected_account.strip()
        or not isinstance(items, list)
        or not 1 <= len(items) <= MAX_THREADS
    ):
        raise ValueError("Confirm an account and between 1 and 20 Gmail conversations")
    account, selected, seen = expected_account.strip().casefold(), [], set()
    for item in items:
        key = item.get("id") if isinstance(item, dict) else None
        match = re.fullmatch(r"gmail:([0-9a-f]{10,32})", key) if isinstance(key, str) else None
        if (
            not match
            or key in seen
            or not isinstance(item.get("subject"), str)
            or len(item["subject"]) > 2000
            or (item.get("content_key") and not isinstance(item["content_key"], str))
        ):
            raise ValueError("The confirmed Gmail conversation selection is invalid")
        seen.add(key)
        selected.append(item | {"remote_id": match[1]})
    results = [
        {"id": item["id"], "status": "failed", "error": "Not attempted"} for item in selected
    ]
    current, clicked = None, False

    def failed(index, reason):
        results[index] = {
            "id": selected[index]["id"],
            "status": "unverified" if clicked and index == current else "failed",
            "error": reason,
        }

    try:
        async with asyncio.timeout(BATCH_TIMEOUT), gmail.lock:
            _authorized(gmail, account)
            if gmail.browser.interactive is not None:
                raise _Refused("Finish Gmail sign-in before applying the confirmed selection.")
            async with gmail.browser.session(gmail.db.settings().timezone) as context:
                page = context.pages[0] if context.pages else await context.new_page()
                page.on("dialog", lambda dialog: dialog.dismiss())
                for current, item in enumerate(selected):
                    clicked = False
                    try:
                        async with asyncio.timeout(THREAD_TIMEOUT):
                            await _thread(gmail, page, account, item)
                            button = page.locator(DELETE_BUTTON)
                            if await button.count() != 1:
                                if await _in_trash(gmail, page, account, item):
                                    results[current] = {
                                        "id": item["id"],
                                        "status": "already_trashed",
                                    }
                                    continue
                                raise _Refused("The Gmail Trash control could not be verified.")
                            if (
                                not await button.is_enabled()
                                or await button.get_attribute("aria-disabled") == "true"
                                or await button.evaluate("e => !!e.closest('.a3s')")
                            ):
                                raise _Refused("The Gmail Trash control is unavailable.")
                            await _account(gmail, page, account)
                            await _identity(page, item)
                            clicked = True
                            await button.click(timeout=ELEMENT_TIMEOUT)
                            await page.wait_for_function(
                                """id => ![...document.querySelectorAll('h2.hP')].some(e=>
                                  e.getClientRects().length &&
                                  e.getAttribute('data-legacy-thread-id') === id) ||
                                ![...document.querySelectorAll('div.nX[role="button"]')]
                                  .some(e=>e.getClientRects().length && e.title === 'Delete')""",
                                arg=item["remote_id"],
                                timeout=ELEMENT_TIMEOUT,
                            )
                            if await _in_trash(gmail, page, account, item):
                                results[current] = {"id": item["id"], "status": "trashed"}
                            else:
                                failed(current, "Gmail Trash membership could not be verified.")
                    except _AccountChanged:
                        raise
                    except _Refused as exc:
                        failed(current, str(exc))
                    except Exception:
                        failed(
                            current,
                            "Gmail did not finish the operation. Check Gmail before retrying.",
                        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, _Refused)
            else "Gmail is unavailable or timed out. Check Gmail before retrying."
        )
        for index, result in enumerate(results):
            if result.get("error") == "Not attempted":
                failed(index, reason)
    return {"account": expected_account, "results": results}
