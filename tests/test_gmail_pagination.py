from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from coursedeck.db import Database
from coursedeck.gmail_browser import GmailBrowser
from coursedeck.mail import MailMessage

HTML = """<meta charset="utf-8">
<header><a aria-label="Google Account: Student (student@example.test)">Account</a></header>
<div class="Dj"></div><button aria-label="Older" id="older">Older</button>
<main role="main"></main>
<script>
async function render() {
  if (location.hash.includes('/thread')) return;
  const cfg = await fixtureConfiguration();
  const page = Number(location.hash.match(/\\/p(\\d+)$/)?.[1] || 1);
  await recordPage(page);
  const main = document.querySelector('main');
  const older = document.querySelector('#older');
  const range = document.querySelector('.Dj');
  if (page === 1 && cfg.delayFirstPage && /^51/.test(range.textContent)) {
    await new Promise(resolve=>setTimeout(resolve,cfg.delayFirstPage));
  }
  main.innerHTML = '';
  if (!cfg.total) {
    main.textContent = cfg.emptyText;
    range.textContent = '';
    older.disabled = true;
    older.setAttribute('aria-disabled','true');
    return;
  }
  const pages = Math.ceil(cfg.total / cfg.size);
  for (let number=1; number<=pages; number++) {
    const table = document.createElement('table');
    table.style.display = number === page ? '' : 'none';
    for (let i=(number-1)*cfg.size; i<Math.min(number*cfg.size,cfg.total); i++) {
      const row = document.createElement('tr'); row.className='zA';
      const key = String(i).padStart(3,'0');
      row.innerHTML = `<td class="yW"><span name="Instructor"
      email="teacher@example.test">Instructor</span></td>
      <td><span class="bog">Message ${key}</span><span class="y2"> - Snippet ${key}</span></td>
      <td class="xW"><span title="${cfg.dates[i]}">Date</span></td>`;
      const subject=row.querySelector('.bog');
      if (!cfg.missingIds.includes(i)) subject.setAttribute('data-legacy-thread-id',
        cfg.duplicateIds.includes(i) ? String(i-1).padStart(3,'0') : key);
      if (!cfg.missingKeys) subject.setAttribute('data-legacy-last-message-id','last-'+key);
      subject.onclick=async()=>{
        await recordRead(key);
        main.innerHTML='<div class="a3s">Body '+key+'</div>' +
          (cfg.incomplete.includes(i) ? '<div class="kv">Unread collapsed message</div>' : '');
        location.hash='inbox/thread'+key;
      };
      table.append(row);
    }
    main.append(table);
  }
  range.textContent = cfg.unknownPager.includes(page) ? 'Range unavailable' :
    `${(page-1)*cfg.size+1}–${Math.min(page*cfg.size,cfg.total)} of ${cfg.total}`;
  older.disabled = page === pages;
  if (cfg.nativeDisabled) older.removeAttribute('aria-disabled');
  else older.setAttribute('aria-disabled',String(page===pages));
  older.onclick=()=>location.hash='inbox/p'+(page+1);
}
addEventListener('hashchange',render); render();
</script>"""


@asynccontextmanager
async def mailbox(tmp_path, monkeypatch, **options):
    import coursedeck.gmail_browser as module

    monkeypatch.setattr(module, "BODY_READS_PER_PAGE", 2)
    configuration = {
        "total": 126,
        "size": 50,
        "emptyText": "Your inbox is empty.",
        "missingIds": [],
        "missingKeys": False,
        "incomplete": [],
        "unknownPager": [],
        "nativeDisabled": False,
        "delayFirstPage": 0,
        "duplicateIds": [],
    } | options
    configuration["dates"] = [
        (datetime(2026, 9, 12, 12, tzinfo=UTC) - timedelta(minutes=i)).isoformat()
        for i in range(configuration["total"])
    ]
    db = Database(tmp_path / "mail.db")
    db.update_state("gmail", account="student@example.test", authorized=True)
    reads, pages = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        await context.expose_function("fixtureConfiguration", lambda: configuration)
        await context.expose_function("recordRead", lambda value: reads.append(value))
        await context.expose_function("recordPage", lambda value: pages.append(value))
        await context.route(
            "https://mail.google.com/**",
            lambda route: route.fulfill(body=HTML, content_type="text/html"),
        )

        class Session:
            @asynccontextmanager
            async def session(self, timezone=None):
                yield context

        def adapter():
            result = GmailBrowser(db, tmp_path)
            result.browser = Session()
            return result

        try:
            yield db, adapter, context, configuration, reads, pages
        finally:
            await browser.close()


def cached_ids(db):
    with db.connection() as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT id FROM mail_messages ORDER BY received_at DESC,id DESC"
            )
        ]


async def test_more_than_30_threads_hidden_rows_resume_history_and_preserve_local_state(
    tmp_path, monkeypatch
):
    async with mailbox(tmp_path, monkeypatch) as (db, adapter, context, cfg, reads, pages):
        first = adapter()
        await first._sync()
        assert cached_ids(db) == [f"gmail:{i:03}" for i in range(100)]
        assert db.state("gmail")["inbox_progress"]["next_page"] == 3
        assert db.state("gmail")["coverage"]["source_total"] == 126
        assert reads == ["000", "001", "050", "051"]
        page = context.pages[0]
        assert await page.locator("tr.zA").count() == 126
        assert await page.locator("tr.zA:visible").count() == 50
        first.store.patch("gmail:000", {"deleted": True, "starred": True})
        # A recreated adapter resumes its persisted page, rather than page two forever.
        second = adapter()
        await second._sync()
        assert cached_ids(db) == [f"gmail:{i:03}" for i in range(126)]
        assert db.state("gmail")["inbox_progress"]["next_page"] == 2
        assert reads[-4:] == ["002", "003", "100", "101"]
        assert second.store.get("gmail:000")["deleted"]
        assert second.store.get("gmail:000")["starred"]
        assert "gmail:000" not in [item["id"] for item in second.store.list()["messages"]]
        # Cached ordering follows actual dates: newly backfilled old mail stays below page one.
        await second._sync()
        assert cached_ids(db)[0] == "gmail:000"
        assert reads[-4:] == ["004", "005", "052", "053"]


async def test_native_disabled_last_page_is_respected(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, total=3, nativeDisabled=True) as (db, adapter, *rest):
        await adapter()._sync()
        assert len(cached_ids(db)) == 3
        assert db.state("gmail")["inbox_progress"]["next_page"] == 2


async def test_return_to_page_one_waits_for_visible_history_rows_to_change(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, delayFirstPage=180) as (db, adapter, context, *rest):
        page = await context.new_page()
        current = adapter()
        rows, _, position = await current._inbox_page(page, 2, 50)
        assert rows[0]["id"] == "050" and position == (51, 100, 126)
        rows, _, position = await current._inbox_page(page, 1, 50)
        assert rows[0]["id"] == "000" and position == (1, 50, 126)


async def test_duplicate_history_ids_do_not_advance_or_overwrite_first_header(
    tmp_path, monkeypatch
):
    async with mailbox(tmp_path, monkeypatch, duplicateIds=[51]) as (db, adapter, *rest):
        db.update_state("gmail", inbox_progress={"next_page": 2})
        current = adapter()
        await current._sync()
        assert len(cached_ids(db)) == 99
        assert db.state("gmail")["inbox_progress"]["next_page"] == 2
        assert current.store.get("gmail:050")["subject"] == "Message 050"
        assert "could not" in db.state("gmail")["warning"].lower()


async def test_body_failures_and_missing_message_keys_do_not_starve_other_rows(
    tmp_path, monkeypatch
):
    async with mailbox(
        tmp_path, monkeypatch, total=9, size=3, missingKeys=True, incomplete=[0, 3, 6]
    ) as (db, adapter, context, cfg, reads, pages):
        current = adapter()
        for _ in range(5):
            await current._sync()
        assert set(reads) == {f"{i:03}" for i in range(9)}
        assert current.store.get("gmail:000")["body"].startswith("Body 000")
        assert not current.store.get("gmail:000")["body_complete"]
        assert db.state("gmail")["coverage"]["cached_bodies_pending"] >= 3


async def test_latest_body_timeout_keeps_both_header_pages_and_reads_history(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch) as (db, adapter, context, cfg, reads, pages):
        current = adapter()
        original = current._bodies
        attempted_pages = []

        async def one_slow_page(page, rows, cached, number, page_size):
            assert len(cached_ids(db)) == 100
            assert db.state("gmail")["inbox_progress"]["next_page"] == 3
            attempted_pages.append(number)
            if number == 1:
                raise TimeoutError("Synthetic latest-page body budget exhausted")
            return await original(page, rows, cached, number, page_size)

        monkeypatch.setattr(current, "_bodies", one_slow_page)
        await current._sync()
        assert attempted_pages == [1, 2]
        assert reads == ["050", "051"]
        assert len(cached_ids(db)) == 100
        assert db.state("gmail")["last_sync"]
        assert "could not" in db.state("gmail")["warning"].lower()


async def test_unknown_pager_caches_headers_without_moving_checkpoint(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, unknownPager=[1]) as (db, adapter, *rest):
        db.update_state("gmail", inbox_progress={"next_page": 3})
        await adapter()._sync()
        assert len(cached_ids(db)) == 50
        assert db.state("gmail")["inbox_progress"]["next_page"] == 3
        assert not db.state("gmail")["coverage"]["pagination_known"]
        assert "pagination" in db.state("gmail")["warning"].lower()


async def test_missing_history_id_keeps_checkpoint_and_cache(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, missingIds=[57]) as (db, adapter, *rest):
        db.update_state("gmail", inbox_progress={"next_page": 2})
        await adapter()._sync()
        assert len(cached_ids(db)) == 99
        assert "gmail:050" in cached_ids(db)
        assert "gmail:057" not in cached_ids(db)
        assert db.state("gmail")["inbox_progress"]["next_page"] == 2
        assert "could not" in db.state("gmail")["warning"].lower()


async def test_confirmed_empty_inbox_retains_previously_cached_mail(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, total=0) as (db, adapter, *rest):
        current = adapter()
        current.store.upsert(MailMessage(id="gmail:old", sender="Fixture", subject="Cached"))
        await current._sync()
        assert cached_ids(db) == ["gmail:old"]
        assert db.state("gmail")["coverage"]["listed_this_run"] == 0
        assert db.state("gmail")["last_sync"]


async def test_unknown_empty_layout_does_not_report_success(tmp_path, monkeypatch):
    async with mailbox(tmp_path, monkeypatch, total=0, emptyText="Loading...") as (
        db,
        adapter,
        context,
        *rest,
    ):
        page = await context.new_page()
        original = page.wait_for_function

        async def short_timeout(*args, **kwargs):
            return await original(*args, **(kwargs | {"timeout": 200}))

        monkeypatch.setattr(page, "wait_for_function", short_timeout)
        db.update_state("gmail", last_sync="previous", inbox_progress={"next_page": 3})
        with pytest.raises((BrowserTimeout, ValueError)):
            await adapter()._sync()
        assert db.state("gmail")["last_sync"] == "previous"
        assert db.state("gmail")["inbox_progress"]["next_page"] == 3
