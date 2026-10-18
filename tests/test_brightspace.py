import json
from pathlib import Path

from coursedeck.connectors.brightspace import BrightspaceAPITransport, map_folder
from coursedeck.connectors.http import TransportError
from coursedeck.domain import Outcome


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/brightspace/folder.json").read_text())


def test_due_is_not_closing_date():
    task = map_folder(
        "c",
        fixture(),
        [{"Submissions": [{"Id": 1}], "Feedback": {"IsGraded": True, "Score": 0}}],
        "https://school.example",
    )
    assert task.due_at < task.closes_at
    assert task.graded and task.score == 0
    assert task.id == "brightspace:c:dropbox-12"
    assert map_folder("c", fixture(), None, "https://school.example").submission_status == "unknown"


async def test_api_pagination_and_denied_submissions_preserve_deadlines():
    calls = []

    async def get(path, params):
        calls.append((path, params))
        if "myenrollments" in path:
            if params.get("bookmark"):
                return {"Items": [], "PagingInfo": {"HasMoreItems": False}}
            return {
                "Items": [{"OrgUnit": {"Id": 1, "Name": "Writing"}}],
                "PagingInfo": {"HasMoreItems": True, "Bookmark": "1"},
            }
        if "mysubmissions" in path:
            raise TransportError(Outcome.PARTIAL, "No permission for submission state")
        if "myItems" in path:
            return {"Objects": [], "Next": None}
        return [fixture()]

    result = await BrightspaceAPITransport(get, "https://school.example", "1.49", "1.82").sync()
    assert len(result.tasks) == 1
    assert result.tasks[0].due_at is not None
    assert result.tasks[0].submission_status == "unknown"
    assert result.outcome == Outcome.PARTIAL and not result.complete
    assert len([path for path, _ in calls if "myenrollments" in path]) == 2


async def test_browser_setup_does_not_require_a_credential_store(tmp_path):
    from unittest.mock import Mock

    from coursedeck.browser import BrowserManager
    from coursedeck.connectors.brightspace import BrightspaceConnector
    from coursedeck.db import Database

    vault = Mock()
    vault.delete.side_effect = RuntimeError("No keyring")
    connector = BrightspaceConnector(
        Database(tmp_path / "db"), BrowserManager(tmp_path, "brightspace"), vault
    )
    await connector.configure({"base_url": "https://school.example", "transport": "browser"})
    assert connector.connection_status() == "not_connected"
    vault.delete.assert_not_called()
