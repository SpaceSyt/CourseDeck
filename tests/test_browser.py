from pathlib import Path

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
