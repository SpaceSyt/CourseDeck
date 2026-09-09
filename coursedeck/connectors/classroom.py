import asyncio
import json
import secrets
import time
import webbrowser
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from ..credentials import CredentialStore
from ..db import Database
from ..domain import Course, Outcome, SyncResult, Task
from .base import Connector
from .http import TransportError, json_get

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
]


def map_course(raw: dict) -> Course:
    return Course(
        provider="google_classroom",
        external_id=str(raw["id"]),
        name=raw["name"],
        section=raw.get("section"),
        source_url=raw.get("alternateLink"),
        raw_metadata=raw,
    )


def map_task(course_id: str, raw: dict, submission: dict | None) -> Task:
    due = None
    if raw.get("dueDate"):
        d, t = raw["dueDate"], raw.get("dueTime", {})
        due = datetime(
            d["year"],
            d["month"],
            d["day"],
            t.get("hours", 0),
            t.get("minutes", 0),
            t.get("seconds", 0),
            tzinfo=UTC,
        )
    submission = submission or {}
    status = {
        "NEW": "open",
        "CREATED": "open",
        "TURNED_IN": "submitted",
        "RETURNED": "returned",
        "RECLAIMED_BY_STUDENT": "open",
    }.get(submission.get("state"), "unknown")
    return Task(
        provider="google_classroom",
        external_id=str(raw["id"]),
        course_external_id=course_id,
        title=raw["title"],
        description=raw.get("description", ""),
        due_at=due,
        url=raw.get("alternateLink"),
        points_possible=raw.get("maxPoints"),
        score=submission.get("assignedGrade"),
        graded="assignedGrade" in submission,
        submission_status=status,
        source_updated_at=raw.get("updateTime"),
        raw_data={"coursework": raw, "submission": submission},
    )


class ClassroomTransport:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def pages(self, path: str, field: str, **params):
        seen = set()
        for _ in range(1000):
            body = await json_get(self.client, path, params)
            if not isinstance(body, dict) or not isinstance(body.get(field, []), list):
                raise TransportError(Outcome.PARSE_ERROR, "Classroom response schema changed.")
            if "error" in body or (field not in body and body.keys() - {"nextPageToken"}):
                raise TransportError(
                    Outcome.PARSE_ERROR, "Classroom list response was not recognized."
                )
            for row in body.get(field, []):
                yield row
            token = body.get("nextPageToken")
            if not token:
                return
            if token in seen:
                raise TransportError(Outcome.PARSE_ERROR, "Classroom pagination did not advance.")
            seen.add(token)
            params["pageToken"] = token
        raise TransportError(Outcome.PARTIAL, "Pagination safety limit reached.")

    async def sync(self):
        result = SyncResult(
            outcome=Outcome.SUCCESS,
            complete=True,
            metadata={"transport": "official_api", "scope": "student_readonly"},
        )
        try:
            async for raw_course in self.pages("courses", "courses", studentId="me"):
                c = map_course(raw_course)
                result.courses.append(c)
                cid = quote(c.external_id, safe="")
                try:
                    # Deadlines are the priority. Keep coursework even if submissions fail later.
                    course_tasks = {}
                    async for raw in self.pages(f"courses/{cid}/courseWork", "courseWork"):
                        mapped = map_task(c.external_id, raw, None)
                        course_tasks[mapped.external_id] = mapped
                        result.tasks.append(mapped)
                    # '-' requests submissions across coursework, avoiding N+1 requests.
                    try:
                        async for sub in self.pages(
                            f"courses/{cid}/courseWork/-/studentSubmissions",
                            "studentSubmissions",
                            userId="me",
                        ):
                            task = course_tasks.get(str(sub["courseWorkId"]))
                            if task is not None:
                                updated = map_task(c.external_id, task.raw_data["coursework"], sub)
                                task.submission_status = updated.submission_status
                                task.score, task.graded = updated.score, updated.graded
                                task.raw_data = updated.raw_data
                    except TransportError as exc:
                        if exc.outcome != Outcome.PARTIAL:
                            raise
                        result.warnings.append("Submission state unavailable for a course.")
                        result.complete = False
                except TransportError as exc:
                    result.warnings.append(exc.safe_message)
                    result.complete = False
                    result.metadata["last_error"] = exc.outcome
                    if exc.outcome in {Outcome.AUTH_REQUIRED, Outcome.RATE_LIMITED}:
                        raise
        except TransportError as exc:
            result.warnings.append(exc.safe_message)
            result.complete = False
            result.outcome = Outcome.PARTIAL if result.courses else exc.outcome
            result.metadata["last_error"] = exc.outcome
        except (KeyError, ValueError, TypeError):
            result.warnings.append("Classroom parser rejected an unexpected record.")
            result.complete = False
            result.outcome = Outcome.PARTIAL if result.tasks else Outcome.PARSE_ERROR
        if result.outcome == Outcome.SUCCESS and not result.complete:
            result.outcome = Outcome.PARTIAL
        return result


class ClassroomConnector(Connector):
    key = "google_classroom"
    display_name = "Google Classroom"
    description = "Official read-only API for your courses, coursework, and submission status."

    @property
    def configuration_fields(self):
        return [
            {
                "key": "client_id",
                "label": "Desktop OAuth client ID",
                "type": "text",
                "placeholder": "…apps.googleusercontent.com",
                "help": "Enable Classroom API and create a Desktop app client in Google Cloud.",
            },
            {
                "key": "client_secret",
                "label": "Desktop OAuth client secret",
                "type": "password",
                "help": "Stored in your OS credential vault. This is not your Google password.",
            },
        ]

    def __init__(self, db: Database, vault: CredentialStore):
        self.db, self.vault = db, vault
        self.pending: tuple[Flow, str, float] | None = None

    def connection_status(self):
        if self.pending and self.pending[2] > time.monotonic():
            return "login_pending"
        state = self.db.state(self.key)
        if state.get("authorized"):
            return "connected"
        return "not_connected" if state.get("configured") else "not_configured"

    async def configure(self, config: dict):
        installed = config.get("installed", config)
        client_id, secret = installed.get("client_id"), installed.get("client_secret")
        if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError("Provide a Google Desktop OAuth client_id")
        if not isinstance(secret, str) or not secret:
            raise ValueError("Provide the Desktop OAuth client_secret")
        await asyncio.to_thread(
            self.vault.set,
            f"{self.key}-client",
            {
                "client_id": client_id,
                "client_secret": secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
        )
        await asyncio.to_thread(self.vault.delete, self.key)
        self.pending = None
        self.db.update_state(self.key, configured=True, authorized=False)

    async def connect(self, callback_url: str | None = None):
        config = await asyncio.to_thread(self.vault.get, f"{self.key}-client")
        if not config or not callback_url:
            raise ValueError("Configure a Desktop OAuth client in source settings first")
        flow = Flow.from_client_config(
            {"installed": config},
            scopes=SCOPES,
            redirect_uri=callback_url,
            autogenerate_code_verifier=True,
        )
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        self.pending = (flow, state, time.monotonic() + 600)
        await asyncio.to_thread(webbrowser.open, url)
        return {"message": "Complete Google consent in your browser. This page will update."}

    async def authorization_callback(self, params: dict):
        pending, self.pending = self.pending, None
        if not pending or pending[2] < time.monotonic():
            raise ValueError("Login expired. Reconnect and try again.")
        flow, state, _ = pending
        if not secrets.compare_digest(state, params.get("state", "")) or not params.get("code"):
            raise ValueError("OAuth state mismatch or consent denied")
        await asyncio.to_thread(flow.fetch_token, code=params["code"])
        credentials = json.loads(flow.credentials.to_json())
        await asyncio.to_thread(self.vault.set, self.key, credentials)
        self.db.update_state(self.key, authorized=True, last_outcome=None, warnings=[])

    async def disconnect(self):
        self.pending = None
        await asyncio.to_thread(self.vault.delete, self.key)
        self.db.update_state(self.key, authorized=False, last_outcome=None, warnings=[])

    async def sync(self):
        raw = await asyncio.to_thread(self.vault.get, self.key)
        if not raw:
            self.db.update_state(self.key, authorized=False)
            return SyncResult(outcome=Outcome.AUTH_REQUIRED, warnings=["Connect Classroom first."])
        credentials = Credentials.from_authorized_user_info(raw, SCOPES)
        try:
            if not credentials.valid:
                await asyncio.to_thread(credentials.refresh, GoogleRequest())
                await asyncio.to_thread(self.vault.set, self.key, json.loads(credentials.to_json()))
        except RefreshError:
            self.db.update_state(self.key, authorized=False)
            return SyncResult(
                outcome=Outcome.AUTH_REQUIRED,
                warnings=["Google authorization expired or was revoked. Reconnect."],
            )
        async with httpx.AsyncClient(
            base_url="https://classroom.googleapis.com/v1/",
            timeout=30,
            headers={"Authorization": f"Bearer {credentials.token}"},
        ) as client:
            result = await ClassroomTransport(client).sync()
            if (
                result.outcome == Outcome.AUTH_REQUIRED
                or result.metadata.get("last_error") == Outcome.AUTH_REQUIRED
            ):
                self.db.update_state(self.key, authorized=False)
            return result
