"""Synthetic route fixtures: no browser, DNS, credential store or remote traffic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coursedeck.chat_browser import BrowserTools
from coursedeck.chat_browser_policy import CourseNavigation

COURSE = {
    "id": "brightspace:123",
    "external_id": "123",
    "provider": "brightspace",
    "name": "Fixture",
    "source_url": "https://school.example/d2l/home/123",
}


@pytest.mark.parametrize("resource_type", ["fetch", "xhr"])
@pytest.mark.parametrize(
    "url",
    [
        "https://school.example/d2l/api/le/1.82/999/content/toc",
        "https://unrelated.example/collect?data=synthetic",
        "https://school.example/d2l/api/lp/1.0/enrollments/myenrollments/",
        "https://school.example/d2l/home/999",
        "https://school.example/d2l/home/123?ou=999",
        "https://school.example/d2l/api/le/1.82/123/content/toc?ou=999",
        "https://school.example/d2l/api/le/1.82/123/content/toc?url=https://unrelated.example",
        "https://school.example/d2l/api/le/1.82/123/content/toc?page=1&page=2",
        "https://school.example/d2l/api/le/1.82/123/content/%2e%2e/999/content/toc",
        "https://school.example/d2l/api/le/1.82/123/content/%252e%252e/999/content/toc",
        "https://school.example/d2l/api/le/1.82/123/content%2ftoc",
        "https://school.example/d2l/api/le/1.82/123/dropbox/folders/1/delete",
        "https://s.brightspace.com/lib/data.json",
    ],
)
async def test_non_navigation_requests_cannot_escape_course(monkeypatch, resource_type, url):
    dns = AsyncMock(return_value="93.184.216.34")
    monkeypatch.setattr("coursedeck.chat_browser.public_address", dns)
    browser = BrowserTools(None, None, None)
    browser.course, browser.policy = COURSE, CourseNavigation(COURSE)
    route = SimpleNamespace(
        request=SimpleNamespace(
            url=url, method="GET", resource_type=resource_type, is_navigation_request=lambda: False
        ),
        abort=AsyncMock(),
        fallback=AsyncMock(),
    )
    await browser.route(route)
    route.abort.assert_awaited_once()
    route.fallback.assert_not_awaited()
    dns.assert_not_awaited()
    assert browser.blocked


@pytest.mark.parametrize(
    "path",
    [
        "/d2l/api/le/1.82/123/content/toc",
        "/d2l/api/le/1.82/123/content/modules/456",
        "/d2l/api/le/1.82/123/content/topics/456",
        "/d2l/api/le/1.82/123/content/myItems/?bookmark=fixture-page-2",
        "/d2l/api/le/1.82/123/news/",
        "/d2l/api/le/1.82/123/quizzes/456",
        "/d2l/api/le/1.82/123/discussions/forums/456/topics/789/posts/",
        "/d2l/api/le/1.82/123/dropbox/folders/456/submissions/mysubmissions/",
        "/d2l/lms/dropbox/user/folders_list.d2l?ou=123",
    ],
)
async def test_selected_course_get_apis_still_read(monkeypatch, path):
    monkeypatch.setattr(
        "coursedeck.chat_browser.public_address", AsyncMock(return_value="93.184.216.34")
    )
    browser = BrowserTools(None, None, None)
    browser.course, browser.policy = COURSE, CourseNavigation(COURSE)
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://school.example" + path,
            method="GET",
            resource_type="fetch",
            is_navigation_request=lambda: False,
        ),
        abort=AsyncMock(),
        fallback=AsyncMock(),
    )
    await browser.route(route)
    route.fallback.assert_awaited_once()
    route.abort.assert_not_awaited()


@pytest.mark.parametrize(
    "url,kind,allowed",
    [
        ("https://school.example/d2l/common/scripts/app.js?v=2026.1", "script", True),
        ("https://school.example/d2l/lp/auth/login.js", "script", True),
        ("https://school.example/d2l/common/styles/app.css", "stylesheet", True),
        ("https://s.brightspace.com/lib/bsi/app.js", "script", True),
        ("https://s.brightspace.com/lib/bsi/app.css", "stylesheet", True),
        ("https://unrelated.example/app.js", "script", False),
        ("https://s.brightspace.com.evil.example/lib/app.js", "script", False),
        ("https://school.example/d2l/api/le/1.82/999/content/toc.js", "script", False),
        ("https://school.example/d2l/home/999", "script", False),
        ("https://school.example/d2l/common/scripts/app.js", "fetch", False),
        ("https://school.example/d2l/common/scripts/app.js?data=private", "script", False),
        ("https://s.brightspace.com/lib/app.js?token=private", "script", False),
        ("https://s.brightspace.com/lib/app.js?v=1&v=2", "script", False),
        ("https://s.brightspace.com/lib/app.js?url=https://evil.example", "script", False),
        ("https://s.brightspace.com/lib/%2e%2e/collect.js", "script", False),
        ("https://s.brightspace.com/lib/app.js", "stylesheet", False),
        ("https://www.gstatic.com/classroom/app.js", "script", False),
    ],
)
def test_assets_use_separate_typed_host_path_allowlist(url, kind, allowed):
    assert bool(CourseNavigation(COURSE).allows_resource(url, kind)) is allowed


@pytest.mark.parametrize(
    "provider,root,allowed,blocked",
    [
        (
            "gradescope",
            "https://www.gradescope.com/courses/123",
            "https://www.gradescope.com/courses/123/assignments/45/submissions/67",
            "https://www.gradescope.com/courses/999/assignments/45/submissions/67",
        ),
        (
            "google_classroom",
            "https://classroom.google.com/c/abc",
            "https://classroom.google.com/u/0/c/abc/a/45/details",
            "https://classroom.google.com/u/0/c/other/a/45/details",
        ),
        (
            "webassign",
            "https://www.webassign.net/student?class=123",
            "https://www.webassign.net/student?class=123&action=pastassignments",
            "https://www.webassign.net/student?class=999&action=pastassignments",
        ),
    ],
)
def test_provider_course_scope_applies_to_dynamic_subrequests(provider, root, allowed, blocked):
    policy = CourseNavigation(COURSE | {"provider": provider, "source_url": root})
    assert policy.allows_resource(allowed, "xhr")
    assert not policy.allows_resource(blocked, "xhr")
    assert not policy.allows_resource("https://unrelated.example/collect", "xhr")


def test_google_bootstrap_asset_does_not_open_account_or_global_rpc_endpoints():
    policy = CourseNavigation(
        COURSE
        | {
            "provider": "google_classroom",
            "source_url": "https://classroom.google.com/c/abc",
        }
    )
    assert policy.allows_resource("https://www.gstatic.com/classroom/app.js", "script")
    assert policy.allows_resource(
        "https://www.gstatic.com/_/mss/boq-classroom/_/js/k=fixture/m=main", "script"
    )
    assert not policy.allows_resource("https://accounts.google.com/ListAccounts", "xhr")
    assert not policy.allows_resource(
        "https://classroom.google.com/_/ClassroomUi/data/batchexecute", "fetch"
    )
    assert not policy.allows_resource("https://www.gstatic.com/another-product/app.js", "script")


@pytest.mark.parametrize(
    "suffix",
    [
        "?ou=999",
        "?courseId=999",
        "/%2e%2e/999",
        "/%252e%252e/999",
    ],
)
def test_selected_navigation_path_cannot_override_course_or_traverse(suffix):
    assert not CourseNavigation(COURSE).allows(COURSE["source_url"] + suffix)
