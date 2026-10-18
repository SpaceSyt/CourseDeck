import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from coursedeck import tray


def batch():
    return {
        "count": 2,
        "timezone": "America/New_York",
        "items": [
            {
                "title": "Fixture assignment",
                "due_at": "2026-11-01T06:30:00+00:00",
                "kinds": ["2h"],
                "warning": None,
            },
            {
                "title": "Second assignment",
                "due_at": "2026-11-01T07:30:00+00:00",
                "kinds": ["deadline_earlier"],
                "warning": "Submission status unknown",
            },
        ],
    }


def test_balloon_contains_due_zone_summary_and_caution():
    message = tray.reminder_message(batch())
    assert "Deadline moved earlier" in message
    assert "Fixture assignment" in message
    assert "Nov 01, 01:30 EST" in message
    assert "+1 more" in message
    assert "unverified" in message
    assert len(message) < 256
    assert tray.reminder_message({"items": []}) is None


@pytest.mark.parametrize("operation", ["claim", "snooze"])
def test_tray_uses_local_protected_reminder_endpoint(monkeypatch, operation):
    calls = []

    def respond(request):
        calls.append((request.method, str(request.url), request.headers["X-CourseDeck"]))
        return httpx.Response(200, json={"count": 0, "items": []})

    client = httpx.Client(base_url="http://127.0.0.1:12345", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tray.httpx, "Client", lambda **kwargs: client)
    service = SimpleNamespace(ready=True, url="http://127.0.0.1:12345")
    assert tray.TrayService.reminder_request(service, operation)["count"] == 0
    assert calls == [("POST", f"http://127.0.0.1:12345/api/reminders/{operation}", "1")]


def test_tray_polls_after_transient_failure_and_emits_one_summary(monkeypatch):
    class StopAfterThree:
        def __init__(self):
            self.ticks = 0

        def wait(self, timeout):
            self.ticks += 1
            return self.ticks > 3

        def is_set(self):
            return False

        def set(self):
            pass

    notifications = []

    class Icon:
        def __init__(self, *args):
            self.title = "CourseDeck"

        def run(self, setup):
            setup(self)

        def notify(self, *args):
            notifications.append(args)

    class Menu:
        SEPARATOR = object()

        def __init__(self, *args):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pystray",
        SimpleNamespace(Icon=Icon, Menu=Menu, MenuItem=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(tray, "tray_image", lambda: None)
    monkeypatch.setattr(tray.threading, "Event", StopAfterThree)
    monkeypatch.setattr(tray.time, "monotonic", Mock(side_effect=[0, 0, 30, 30, 60, 60]))
    opened = Mock()
    monkeypatch.setattr(tray.webbrowser, "open", opened)
    service = SimpleNamespace(
        ready=True,
        reminder_request=Mock(
            side_effect=[httpx.ConnectError("fixture offline"), batch(), {"items": []}]
        ),
    )
    tray.run_tray(service, open_browser=False)
    assert service.reminder_request.call_count == 3
    assert notifications == [(tray.reminder_message(batch()), "CourseDeck deadlines")]
    opened.assert_not_called()
