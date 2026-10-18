from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.async_api import async_playwright

from coursedeck.connectors.brightspace import BrightspaceAPITransport, BrightspaceConnector
from coursedeck.connectors.http import TransportError
from coursedeck.db import Database
from coursedeck.domain import Course, Outcome, SyncResult


def scheduled(key=10, **changes):
    return {
        "OrgUnitId": "1",
        "ItemId": key,
        "ItemName": f"Reading {key}",
        "ItemType": 1,
        "DueDate": "2026-09-18T03:59:59Z",
        "StartDate": "2026-09-17T04:01:00Z",
        "EndDate": None,
        "DateCompleted": "2026-09-08T00:00:00Z",
        "ItemUrl": None,
        "ActivityType": 1,
        "IsExempt": False,
        **changes,
    }


async def test_browser_restores_sso_before_api_on_each_restart(tmp_path):
    db = Database(tmp_path / "db")
    db.update_state("brightspace", authorized=True, config={"base_url": "https://school.example"})
    calls = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def route_source(route):
            path = route.request.url.split("school.example", 1)[1]
            cookie = await route.request.header_value("cookie") or ""
            calls.append((path, "authenticated=yes" in cookie))
            if path == "/d2l/home" and "authenticated=yes" not in cookie:
                await route.fulfill(
                    content_type="text/html",
                    body="""<script>
                history.replaceState(null, '', '/sso/start');
                setTimeout(() => { document.cookie='authenticated=yes; path=/';
                  location.href='/d2l/home'; }, 50);
                </script>""",
                )
            elif "authenticated=yes" not in cookie:
                await route.fulfill(status=403)
            elif path == "/d2l/home":
                await route.fulfill(
                    content_type="text/html", body="<d2l-navigation>Main</d2l-navigation>"
                )
            elif "myenrollments" in path:
                await route.fulfill(
                    json={
                        "Items": [{"OrgUnit": {"Id": 1, "Name": "Computing"}}],
                        "PagingInfo": {"HasMoreItems": False},
                    }
                )
            elif "myItems" in path:
                await route.fulfill(json={"Objects": [scheduled()], "Next": None})
            else:
                await route.fulfill(json=[])

        class Manager:
            interactive = None

            def exists(self):
                return True

            @asynccontextmanager
            async def session(self, timezone=None):
                context = await browser.new_context()
                assert not await context.cookies()
                await context.route("https://school.example/**", route_source)

                class Response:
                    status = 200
                    url = "https://school.example/d2l/home"

                    async def text(self):
                        return "<d2l-navigation>Main</d2l-navigation>"

                    async def json(self):
                        return self.body

                async def request_get(url, params=None, timeout=None):
                    authenticated = any(
                        c["name"] == "authenticated" for c in await context.cookies()
                    )
                    calls.append((url, authenticated))
                    response = Response()
                    if not authenticated:
                        response.status = 403
                    if "myenrollments" in url:
                        response.body = {
                            "Items": [{"OrgUnit": {"Id": 1, "Name": "Computing"}}],
                            "PagingInfo": {"HasMoreItems": False},
                        }
                    elif "myItems" in url:
                        response.body = {"Objects": [scheduled()], "Next": None}
                    else:
                        response.body = []
                    return response

                try:
                    yield SimpleNamespace(
                        new_page=context.new_page, request=SimpleNamespace(get=request_get)
                    )
                finally:
                    await context.close()

        connector = BrightspaceConnector(db, Manager(), Mock())
        for _ in range(2):
            result = await connector.sync()
            assert result.outcome == Outcome.PARTIAL
            assert len(result.courses) == 1 and len(result.tasks) == 1
            assert result.tasks[0].title == "Reading 10"
        assert any("myItems" in path for path, _ in calls)
        assert all(authenticated for path, authenticated in calls if "/api/" in path)
        assert sum(path == "/d2l/home" and not auth for path, auth in calls) == 2
        await browser.close()


@pytest.mark.parametrize(
    "next_url",
    [
        "https://school.example/d2l/api/le/1.82/1/content/myItems/?bookmark=next",
        "https://unexpected.example/collect",
    ],
)
async def test_content_pagination_and_unknown_dates_retain_items(next_url):
    calls = []

    async def get(path, params):
        calls.append(path)
        assert "unexpected.example" not in path
        if "myenrollments" in path:
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Computing"}}],
                "PagingInfo": {"HasMoreItems": False},
            }
        if "dropbox" in path:
            return []
        if "bookmark=next" in path:
            return {"Objects": [scheduled(11, DueDate="unrecognized date")], "Next": None}
        return {"Objects": [scheduled()], "Next": next_url}

    transport = BrightspaceAPITransport(get, "https://school.example", "1.49", "1.82")
    result = SyncResult(outcome=Outcome.PARTIAL, metadata={"content_items_checked": 0})
    try:
        await transport.scheduled_content(
            Course(
                provider="brightspace",
                external_id="1",
                name="Computing",
                source_url="https://school.example/d2l/home/1",
            ),
            result,
        )
    except TransportError as exc:
        result.warnings.append(exc.safe_message)
    assert result.outcome == Outcome.PARTIAL and not result.complete
    assert result.tasks[0].submission_status == "unknown"  # Reading content is not submission.
    assert result.tasks[0].available_at is not None  # Future availability does not drop a deadline.
    assert result.tasks[0].due_at.isoformat() == "2026-09-18T03:59:59+00:00"
    if "unexpected" in next_url:
        assert len(result.tasks) == 1
        assert any("left the source endpoint" in warning for warning in result.warnings)
    else:
        assert len(result.tasks) == 2 and result.tasks[1].due_at is None
        assert result.tasks[1].raw_data["unavailable_fields"] == ["due_at", "description"]
        assert any("a date could not be read" in warning for warning in result.warnings)
