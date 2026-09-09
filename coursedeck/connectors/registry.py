from pathlib import Path

from ..browser import BrowserManager, installed_chrome_channel
from ..credentials import CredentialStore
from ..db import Database
from .brightspace import BrightspaceConnector
from .classroom_connection import ClassroomConnector
from .gradescope import GradescopeConnector
from .webassign import WebAssignConnector


def build_connectors(db: Database, data_dir: Path):
    vault = CredentialStore(data_dir)
    return [
        ClassroomConnector(
            db,
            vault,
            BrowserManager(data_dir, "google_classroom", channel=installed_chrome_channel()),
        ),
        GradescopeConnector(
            db, BrowserManager(data_dir, "gradescope", channel=installed_chrome_channel())
        ),
        WebAssignConnector(
            db, BrowserManager(data_dir, "webassign", channel=installed_chrome_channel()), vault
        ),
        BrightspaceConnector(
            db, BrowserManager(data_dir, "brightspace", channel=installed_chrome_channel()), vault
        ),
    ]
