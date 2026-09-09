import asyncio
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from ..credentials import CredentialStore
from ..domain import Course, Outcome, SyncResult, Task
from .browser_base import BrowserConnector, session_html
from .dates import source_date
from .http import TransportError, json_get


def map_folder(course_id: str, raw: dict, submissions: list | None, base_url: str) -> Task:
    availability = raw.get("Availability") or {}
    entities = submissions or []
    feedback = next((s["Feedback"] for s in entities if s.get("Feedback")), {})
    submitted = any(s.get("Submissions") or s.get("CompletionDate") for s in entities)
    graded = bool(feedback.get("IsGraded"))
    instructions = raw.get("CustomInstructions") or {}
    description = instructions.get("Text") or BeautifulSoup(
        instructions.get("Html", ""), "html.parser"
    ).get_text(" ", strip=True)
    return Task(
        provider="brightspace",
        external_id=f"dropbox-{raw['Id']}",
        course_external_id=course_id,
        title=raw["Name"],
        description=description,
        due_at=source_date(raw.get("DueDate")),
        available_at=source_date(availability.get("StartDate")),
        closes_at=source_date(availability.get("EndDate")),
        points_possible=(raw.get("Assessment") or {}).get("ScoreDenominator"),
        score=feedback.get("Score"),
        graded=graded,
        submission_status="graded"
        if graded
        else "submitted"
        if submitted
        else "open"
        if submissions is not None
        else "unknown",
        url=f"{base_url}/d2l/lms/dropbox/user/folder_submit_files.d2l?db={raw['Id']}&ou={course_id}",
        raw_data={
            "folder": raw,
            "submission_summary": {
                "known": submissions is not None,
                "submitted": submitted,
                "graded": graded,
                "score": feedback.get("Score"),
            },
        },
    )


def map_scheduled_item(course: Course, raw: dict) -> Task:
    if str(raw["OrgUnitId"]) != course.external_id:
        raise ValueError("Scheduled item belongs to another course")
    item_id = str(raw["ItemId"])
    item_type = "ModuleCO" if raw.get("ItemType") == 0 else "TopicCO"
    dates, unavailable = {}, []
    for key, source_key in (
        ("due_at", "DueDate"),
        ("available_at", "StartDate"),
        ("closes_at", "EndDate"),
    ):
        try:
            dates[key] = source_date(raw.get(source_key))
        except (ValueError, TypeError):
            dates[key] = None
            unavailable.append(key)
    if raw.get("IsExempt"):
        dates["due_at"] = None
    # Reading a content page can complete it in Brightspace; that does not prove an assignment
    # was submitted. Keep this distinction instead of hiding a deadline as completed.
    return Task(
        provider="brightspace",
        external_id=f"content-{item_id}",
        course_external_id=course.external_id,
        title=raw["ItemName"],
        **dates,
        url=f"{urlparse(course.source_url).scheme}://{urlparse(course.source_url).netloc}"
        f"/d2l/le/content/{course.external_id}/Home?itemIdentifier="
        f"D2L.LE.Content.ContentObject.{item_type}-{item_id}",
        submission_status="unknown",
        raw_data={
            "parser": "scheduled_content",
            "activity_type": raw.get("ActivityType"),
            "content_completed_at": raw.get("DateCompleted"),
            "exempt": raw.get("IsExempt", False),
            "source_link_available": bool(raw.get("ItemUrl")),
            "unavailable_fields": unavailable,
        },
    )


class BrightspaceAPITransport:
    """Same API mapper for OAuth HTTP and authenticated browser-context requests."""

    def __init__(self, get_json, base_url: str, lp_version: str, le_version: str):
        self.get = get_json
        self.base_url, self.lp, self.le = base_url, lp_version, le_version

    async def scheduled_content(self, course, result):
        endpoint = f"/d2l/api/le/{self.le}/{course.external_id}/content/myItems/"
        path, seen = endpoint, set()
        for _ in range(1000):
            body = await self.get(path, None)
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("Objects"), list)
                or "Next" not in body
            ):
                raise TransportError(
                    Outcome.PARSE_ERROR, "Scheduled content schema was not recognized."
                )
            for raw in body["Objects"]:
                try:
                    if not isinstance(raw, dict) or "ItemId" not in raw or "DueDate" not in raw:
                        raise ValueError("Incomplete content record")
                    result.metadata["content_items_checked"] += 1
                    if raw.get("DueDate"):
                        task = map_scheduled_item(course, raw)
                        result.tasks.append(task)
                        if task.raw_data["unavailable_fields"]:
                            result.warnings.append(
                                f"{course.name}: {task.title} — a date could not be read."
                            )
                except (KeyError, ValueError, TypeError):
                    result.warnings.append(f"{course.name}: a content item could not be read.")
            next_url = body["Next"]
            if next_url is None:
                return
            if not isinstance(next_url, str) or not next_url:
                raise TransportError(Outcome.PARSE_ERROR, "Content pagination link was invalid.")
            target = urlparse(urljoin(self.base_url + path, next_url))
            if (
                target.scheme != urlparse(self.base_url).scheme
                or target.netloc != urlparse(self.base_url).netloc
                or target.path != endpoint
                or target.username
                or target.password
                or target.fragment
            ):
                raise TransportError(
                    Outcome.PARSE_ERROR, "Content pagination left the source endpoint."
                )
            path = target.path + ("?" + target.query if target.query else "")
            if path in seen:
                raise TransportError(Outcome.PARSE_ERROR, "Content pagination did not advance.")
            seen.add(path)
        raise TransportError(Outcome.PARSE_ERROR, "Content pagination safety limit reached.")

    async def sync(self):
        result = SyncResult(
            outcome=Outcome.PARTIAL,
            complete=False,
            warnings=[
                "Synced assignment folders and dated course content. "
                "Standalone quizzes, discussions and announcements are not yet covered."
            ],
            metadata={
                "transport": "official_api",
                "coverage": "dropbox_and_dated_content",
                "content_items_checked": 0,
            },
        )
        try:
            bookmark, seen = None, set()
            for _ in range(1000):
                params = {"orgUnitTypeId": "3"}
                if bookmark:
                    params["bookmark"] = bookmark
                body = await self.get(f"/d2l/api/lp/{self.lp}/enrollments/myenrollments/", params)
                if not isinstance(body, dict) or not isinstance(body.get("Items"), list):
                    raise TransportError(
                        Outcome.PARSE_ERROR, "Enrollment schema was not recognized."
                    )
                for item in body["Items"]:
                    raw_course = item["OrgUnit"]
                    cid = str(raw_course["Id"])
                    course = Course(
                        provider="brightspace",
                        external_id=cid,
                        name=raw_course["Name"],
                        section=raw_course.get("Code"),
                        source_url=f"{self.base_url}/d2l/home/{cid}",
                        raw_metadata={"Access": item.get("Access", {})},
                    )
                    result.courses.append(course)
                    try:
                        folders = await self.get(
                            f"/d2l/api/le/{self.le}/{cid}/dropbox/folders/", None
                        )
                        if not isinstance(folders, list):
                            raise TransportError(
                                Outcome.PARSE_ERROR, "Dropbox schema was not recognized."
                            )
                        for folder in folders:
                            submissions = None
                            try:
                                submissions = await self.get(
                                    f"/d2l/api/le/{self.le}/{cid}/dropbox/folders/{folder['Id']}/submissions/mysubmissions/",
                                    None,
                                )
                                if not isinstance(submissions, list):
                                    raise TransportError(
                                        Outcome.PARSE_ERROR, "Submission schema was not recognized."
                                    )
                            except TransportError as exc:
                                result.warnings.append(
                                    f"{course.name}: {folder.get('Name', folder.get('Id'))} — "
                                    f"submission status: {exc.safe_message}"
                                )
                            try:
                                result.tasks.append(
                                    map_folder(cid, folder, submissions, self.base_url)
                                )
                            except (KeyError, ValueError, TypeError):
                                result.warnings.append(
                                    f"{course.name}: an assignment could not be read."
                                )
                    except TransportError as exc:
                        result.warnings.append(f"{course.name}: assignments — {exc.safe_message}")
                    try:
                        await self.scheduled_content(course, result)
                    except TransportError as exc:
                        result.warnings.append(f"{course.name}: dated content — {exc.safe_message}")
                paging = body.get("PagingInfo")
                if not isinstance(paging, dict) or not isinstance(paging.get("HasMoreItems"), bool):
                    raise TransportError(
                        Outcome.PARSE_ERROR, "Enrollment pagination metadata missing."
                    )
                if not paging["HasMoreItems"]:
                    return result
                bookmark = paging.get("Bookmark")
                if not bookmark or bookmark in seen:
                    raise TransportError(
                        Outcome.PARSE_ERROR, "Enrollment pagination did not advance."
                    )
                seen.add(bookmark)
            result.warnings.append("Enrollment pagination safety limit reached.")
        except TransportError as exc:
            result.outcome = Outcome.PARTIAL if result.courses else exc.outcome
            result.warnings.append(exc.safe_message)
        except (KeyError, ValueError, TypeError):
            result.outcome = Outcome.PARTIAL if result.tasks else Outcome.PARSE_ERROR
            result.warnings.append("Brightspace returned an unexpected record.")
        return result


def parse_browser_folders(html: str, course: Course):
    soup = BeautifulSoup(html, "html.parser")
    tasks = {}
    for anchor in soup.select('a[href*="db="]'):
        url = urljoin(course.source_url, str(anchor["href"]))
        params = parse_qs(urlparse(url).query)
        if not params.get("db") or not anchor.get_text(" ", strip=True):
            continue
        row = anchor.find_parent("tr")
        due_node = row.select_one("[data-due-date], .due-date time[datetime]") if row else None
        due = None
        if due_node:
            try:
                due = source_date(due_node.get("datetime") or due_node.get("data-due-date"))
            except ValueError:
                pass
        aid = params["db"][0]
        tasks[aid] = Task(
            provider="brightspace",
            external_id=f"dropbox-{aid}",
            course_external_id=course.external_id,
            title=anchor.get_text(" ", strip=True),
            due_at=due,
            url=url,
            submission_status="unknown",
            raw_data={
                "parser": "browser_fallback",
                "unavailable_fields": ["due_at"] if due is None else [],
            },
        )
    return list(tasks.values())


class BrightspaceBrowserTransport:
    def __init__(self, context, base_url: str):
        self.context, self.base_url = context, base_url

    async def sync(self):
        result = SyncResult(
            outcome=Outcome.PARTIAL,
            complete=False,
            warnings=[
                "Experimental browser fallback. Course coverage, deadlines, "
                "and submission state require verification."
            ],
            metadata={"transport": "browser_dom_fallback"},
        )
        page = await self.context.new_page()
        await page.goto(self.base_url + "/d2l/home", wait_until="domcontentloaded")
        # Playwright locators pierce open shadow roots used by Brightspace course tiles.
        await page.locator('a[href*="/d2l/home/"]').first.wait_for(timeout=15000)
        links = await page.locator('a[href*="/d2l/home/"]').evaluate_all(
            "nodes => nodes.map(n => ({url: n.href, name: n.textContent.trim()}))"
        )
        courses = {}
        for link in links:
            match = re.fullmatch(r"/d2l/home/(\d+)", urlparse(link["url"]).path)
            if match and link["name"]:
                courses[match[1]] = Course(
                    provider="brightspace",
                    external_id=match[1],
                    name=link["name"],
                    source_url=f"{self.base_url}/d2l/home/{match[1]}",
                )
        result.courses = list(courses.values())
        for course in result.courses:
            html = await session_html(
                self.context,
                f"{self.base_url}/d2l/lms/dropbox/user/folders_list.d2l?ou={course.external_id}",
            )
            result.tasks.extend(parse_browser_folders(html, course))
        if not result.courses:
            result.outcome = Outcome.PARSE_ERROR
        return result


class BrightspaceConnector(BrowserConnector):
    key = "brightspace"
    display_name = "Brightspace"
    description = "Your school's instance, via API credentials or a dedicated browser session."
    login_path = "/d2l/home"

    @property
    def manual_login(self):
        return self.config.get("transport", "browser") == "browser"

    @property
    def configuration_fields(self):
        fields = super().configuration_fields + [
            {
                "key": "transport",
                "label": "Connection method",
                "type": "select",
                "options": ["browser", "api"],
            },
        ]
        if self.config.get("transport", "browser") != "api":
            return fields
        return fields + [
            {
                "key": "lp_version",
                "label": "Enrollment API version",
                "type": "text",
                "placeholder": "1.49",
            },
            {
                "key": "le_version",
                "label": "Learning API version",
                "type": "text",
                "placeholder": "1.82",
            },
            {
                "key": "access_token",
                "label": "API access token (API mode only)",
                "type": "password",
            },
            {"key": "refresh_token", "label": "API refresh token (optional)", "type": "password"},
            {"key": "client_id", "label": "API client ID (for refresh)", "type": "text"},
            {
                "key": "client_secret",
                "label": "API client secret (for refresh)",
                "type": "password",
            },
            {
                "key": "expires_in",
                "label": "Token lifetime in seconds",
                "type": "number",
                "placeholder": "3600",
            },
        ]

    def __init__(self, db, browser, vault: CredentialStore):
        super().__init__(db, browser)
        self.vault = vault

    def connection_status(self):
        if self.config.get("transport") == "api":
            return "connected" if self.db.state(self.key).get("authorized") else "not_connected"
        return super().connection_status()

    async def configure(self, config: dict):
        allowed = {
            "base_url",
            "timezone",
            "transport",
            "lp_version",
            "le_version",
            "access_token",
            "refresh_token",
            "client_id",
            "client_secret",
            "expires_in",
        }
        if config.keys() - allowed:
            raise ValueError("Unrecognized Brightspace configuration field")
        mode = config.get("transport", self.config.get("transport", "browser"))
        if mode not in {"api", "browser"}:
            raise ValueError("Transport must be api or browser")
        versions = {
            "lp_version": config.get("lp_version", self.config.get("lp_version", "1.49")),
            "le_version": config.get("le_version", self.config.get("le_version", "1.82")),
        }
        if any(not re.fullmatch(r"\d+\.\d+", v) for v in versions.values()):
            raise ValueError("API versions must be numeric, e.g. 1.82")
        old_url, old_mode = self.base_url, self.config.get("transport", "browser")
        await super().configure({k: config[k] for k in ("base_url", "timezone") if k in config})
        if old_url != self.base_url or old_mode != mode:
            if self.db.state(self.key).get("has_api_credentials"):
                await asyncio.to_thread(self.vault.delete, self.key)
            self.db.update_state(self.key, authorized=False, has_api_credentials=False)
        self.db.update_state(self.key, config=self.config | {"transport": mode} | versions)
        secrets = {
            k: config[k]
            for k in ("access_token", "refresh_token", "client_id", "client_secret")
            if k in config
        }
        if secrets:
            saved = (await asyncio.to_thread(self.vault.get, self.key) or {}) | secrets
            saved["expires_at"] = time.time() + float(config.get("expires_in", 3600))
            await asyncio.to_thread(self.vault.set, self.key, saved)
            self.db.update_state(self.key, has_api_credentials=True)

    async def connect(self, callback_url: str | None = None):
        if self.config.get("transport") != "api":
            return await super().connect(callback_url)
        if not self.base_url or not await asyncio.to_thread(self.vault.get, self.key):
            raise ValueError("Configure the instance and API credentials first")
        self.db.update_state(self.key, authorized=True)
        return {"message": "API mode enabled. Sync will validate access to your instance."}

    async def validate_session(self, context):
        try:
            html = await session_html(context, self.base_url + "/d2l/home")
            return "d2l-navigation" in html or "d2l-course" in html or "/d2l/lp/auth/logout" in html
        except TransportError:
            return False

    async def restore_browser_session(self, context):
        # A saved school SSO session may need browser redirects / JavaScript before API use.
        # context.request cannot perform that flow and can return 403 despite a valid login.
        page = await context.new_page()
        await page.goto(self.base_url + "/d2l/home", wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_url(
                lambda url: (
                    urlparse(url).netloc == urlparse(self.base_url).netloc
                    and urlparse(url).path.startswith("/d2l/home")
                ),
                timeout=30000,
            )
        except BrowserTimeout as exc:
            login = await page.locator('input[type="password"], input[type="email"]').count()
            if login or urlparse(page.url).netloc != urlparse(self.base_url).netloc:
                raise TransportError(
                    Outcome.AUTH_REQUIRED, "Brightspace needs school sign-in. Reconnect."
                ) from exc
            raise
        if not await self.validate_session(context):
            raise TransportError(
                Outcome.PARSE_ERROR, "Could not confirm the Brightspace home page. Retry sync."
            )

    async def disconnect(self):
        await super().disconnect()
        if self.db.state(self.key).get("has_api_credentials"):
            await asyncio.to_thread(self.vault.delete, self.key)
            self.db.update_state(self.key, has_api_credentials=False)

    async def api_token(self):
        token = await asyncio.to_thread(self.vault.get, self.key)
        if not token:
            raise TransportError(Outcome.AUTH_REQUIRED, "Configure Brightspace API credentials.")
        if token.get("expires_at", 0) <= time.time() + 30:
            if not all(token.get(k) for k in ("refresh_token", "client_id", "client_secret")):
                raise TransportError(
                    Outcome.AUTH_REQUIRED,
                    "Brightspace access token expired. Reconfigure or use browser mode.",
                )
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://auth.brightspace.com/core/connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": token["refresh_token"],
                        "client_id": token["client_id"],
                        "client_secret": token["client_secret"],
                    },
                )
                if response.status_code != 200:
                    raise TransportError(
                        Outcome.AUTH_REQUIRED,
                        "Brightspace refresh failed. Reauthorize your API credentials.",
                    )
                body = response.json()
                token.update(
                    {
                        "access_token": body["access_token"],
                        "refresh_token": body.get("refresh_token", token["refresh_token"]),
                        "expires_at": time.time() + body.get("expires_in", 3600),
                    }
                )
                await asyncio.to_thread(self.vault.set, self.key, token)
        return token["access_token"]

    async def sync(self):
        if self.connection_status() != "connected":
            return SyncResult(
                outcome=Outcome.AUTH_REQUIRED, warnings=["Connect Brightspace first."]
            )
        lp, le = self.config.get("lp_version", "1.49"), self.config.get("le_version", "1.82")
        try:
            if self.config.get("transport") == "api":
                token = await self.api_token()
                async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=30, headers={"Authorization": f"Bearer {token}"}
                ) as client:

                    async def get(path, params):
                        return await json_get(client, path, params)

                    return await BrightspaceAPITransport(get, self.base_url, lp, le).sync()
            async with self.browser.session(self.config.get("timezone")) as context:
                await self.restore_browser_session(context)

                async def get(path, params):
                    response = await context.request.get(
                        self.base_url + path, params=params, timeout=30000
                    )
                    if response.status != 200:
                        outcome = (
                            Outcome.RATE_LIMITED
                            if response.status == 429
                            else (
                                Outcome.AUTH_REQUIRED
                                if response.status == 401
                                else Outcome.NETWORK_ERROR
                                if response.status >= 500
                                else Outcome.PARTIAL
                            )
                        )
                        raise TransportError(
                            outcome, f"Brightspace resource unavailable (HTTP {response.status})."
                        )
                    try:
                        return await response.json()
                    except ValueError as exc:
                        raise TransportError(
                            Outcome.PARSE_ERROR, "Browser API returned non-JSON content."
                        ) from exc

                result = await BrightspaceAPITransport(get, self.base_url, lp, le).sync()
                result.metadata["transport"] = "browser_session_api"
                if not result.courses and result.outcome in {
                    Outcome.AUTH_REQUIRED,
                    Outcome.PARSE_ERROR,
                    Outcome.PARTIAL,
                }:
                    if not await self.validate_session(context):
                        return SyncResult(
                            outcome=Outcome.AUTH_REQUIRED,
                            warnings=["Brightspace session expired. Reconnect."],
                        )
                    return await BrightspaceBrowserTransport(context, self.base_url).sync()
                return result
        except TransportError as exc:
            return SyncResult(outcome=exc.outcome, warnings=[exc.safe_message])
        except (BrowserError, httpx.RequestError):
            return SyncResult(
                outcome=Outcome.NETWORK_ERROR,
                warnings=["Brightspace request failed; cached data retained."],
            )
