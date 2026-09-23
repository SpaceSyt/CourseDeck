"""Bounded course browsing during Chat. No arbitrary code or selectors."""

import asyncio
import hashlib
import re
from contextlib import AsyncExitStack
from time import monotonic
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from uuid import uuid4

from .chat_browser_policy import (
    BRIGHTSPACE_VIEWS,
    PROVIDERS,
    WRITE,
    CourseNavigation,
    clean_url,
    permitted_control,
)
from .connectors.brightspace_content import static_file_path
from .connectors.classroom_materials import reference_url
from .domain import now
from .task_links import public_address

SIGN_IN_REQUIRED = (
    "Source redirected to sign-in. Reconnect this source in Sources, then retry; "
    "cached evidence is retained."
)

# Walk open shadow roots too (Brightspace web components). Never read inputs, scripts,
# hidden tokens or raw HTML. Keep locator handles server-side, outside model arguments.
SNAPSHOT = r"""() => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++)
    for (const e of roots[i].querySelectorAll('*')) if (e.shadowRoot) roots.push(e.shadowRoot);
  const visible = e => {
    if (!e.getClientRects().length || getComputedStyle(e).visibility === 'hidden') return false;
    for (let parent = e; parent; parent = parent.parentElement || parent.getRootNode().host) {
      if (parent.matches('details:not([open])') &&
        !parent.querySelector(':scope > summary')?.contains(e)) return false;
      if (parent.hidden || getComputedStyle(parent).display === 'none') return false;
    }
    return true;
  };
  const texts = [], controls = [];
  let visualContent = false;
  let size = 0, truncated = false;
  for (const root of roots) {
    for (const e of root.querySelectorAll('img,canvas,video,audio')) {
      if (!visible(e)) continue;
      visualContent = true;
      if (e.matches('img') && e.alt) texts.push('[Image: ' + e.alt.slice(0, 500) + ']');
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const e = walker.currentNode.parentElement;
      if (!e || !visible(e) ||
        e.closest('script,style,noscript,textarea,input,select,[contenteditable]')) continue;
      const text = walker.currentNode.textContent.trim();
      if (text) { if (size + text.length > 60000) { truncated = true; break; }
        texts.push(text); size += text.length + 1; }
    }
    for (const e of root.querySelectorAll('a[href],summary,button,[role="button"],[role="tab"]')) {
      if (!visible(e) || e.disabled || e.getAttribute('aria-disabled') === 'true') continue;
      const label = (e.getAttribute('aria-label') || e.innerText || '').trim().slice(0, 180);
      if (!label) continue;
      const kind = e.matches('a[href]') ? 'link' : e.matches('summary') ||
        e.getAttribute('aria-expanded') === 'false' && e.hasAttribute('aria-controls')
        ? 'disclosure' : 'control';
      if (e.closest('form') && kind !== 'link' || e.matches('button[type="submit"]')) continue;
      if (controls.length >= 200) { truncated = true; break; }
      controls.push({label, kind, nativeDisclosure: e.matches('summary'),
        role: e.getAttribute('role') || (e.matches('button') ? 'button' : ''),
        href: kind === 'link' ? e.href : null, element: e});
    }
  }
  return {text: texts.join('\n'), controls, truncated, visualContent};
}"""


def redact(text):
    # Auth-bearing URLs occasionally appear in visible LMS text, especially WebAssign.
    return re.sub(r'https?://[^\s<>"\']+', lambda m: clean_url(m[0]) or "[private link]", text)


class BrowserTools:
    def __init__(self, service, task_tools, expose, *, documents=None):
        self.service, self.tasks, self.expose = service, task_tools, expose
        self.documents = documents if documents is not None else {}
        self.stack = AsyncExitStack()
        self.page = self.context = self.policy = self.course = None
        self.targets, self.snapshots = {}, {}
        self.snapshot_id = None
        self.operations = 0
        self.started = None
        self.blocked = set()
        self.auth_required = False
        self.addresses = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        try:
            await self.stack.aclose()
        finally:
            self.stack = AsyncExitStack()
            self.page = self.context = self.policy = self.course = None
            self.targets = {}
            self.snapshot_id = None

    def courses(self):
        active = set(self.service.scope(self.tasks.value.course_id)) & set(self.tasks.scope)
        return [
            c
            for c in self.service.db.source_courses()
            if c["id"] in active and c["provider"] in PROVIDERS
        ]

    def definitions(self, define):
        connectors = getattr(self.service.engine, "connectors", {})
        if not any(c["provider"] in connectors for c in self.courses()):
            return []
        string = {"type": "string", "maxLength": 2048}
        return [
            define("browser_courses", "List course IDs available for background browsing.", {}, []),
            define(
                "browser_open",
                "Open a task, cached material's source, or source course. "
                "Use IDs from get_task/search_knowledge/browser_courses. Supply exactly one ID. "
                "Returns visible evidence and allowed navigation targets; never submits work.",
                {"task_id": string, "source_course_id": string, "document_id": string},
                [],
            ),
            define(
                "browser_follow",
                "Follow a link or expand/page a reading control from the latest "
                "snapshot. Use only returned IDs. No arbitrary URLs, typing or JavaScript.",
                {"snapshot_id": string, "target_id": string},
                ["snapshot_id", "target_id"],
            ),
            define(
                "browser_read",
                "Read more of a previously returned browser snapshot without "
                "navigating. Page through text and targets; snapshots do not prove full coverage.",
                {
                    "snapshot_id": string,
                    "offset": {"type": "integer", "minimum": 0},
                    "target_offset": {"type": "integer", "minimum": 0},
                },
                ["snapshot_id"],
            ),
        ]

    async def run(self, name, args):
        schemas = {
            "browser_courses": set(),
            "browser_open": {"task_id", "source_course_id", "document_id"},
            "browser_follow": {"snapshot_id", "target_id"},
            "browser_read": {"snapshot_id", "offset", "target_offset"},
        }
        if name not in schemas or args.keys() - schemas[name]:
            raise ValueError("Unsupported browser parameters")
        if name == "browser_courses":
            return {
                "courses": [
                    {k: c.get(k) for k in ("id", "name", "provider")} for c in self.courses()
                ]
            }
        if name == "browser_read":
            return self.read(args)
        if self.operations >= 10 or self.started and monotonic() - self.started > 120:
            await self.close()
            raise ValueError("Browser limit reached; continue with a narrower question")
        self.operations += 1
        self.started = self.started or monotonic()
        self.auth_required = False
        try:
            async with asyncio.timeout(45):
                if name == "browser_open":
                    result = await self.open(args)
                    if result:
                        return result
                else:
                    result = await self.follow(args)
                    if result:
                        return result
                return await self.snapshot()
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            blocked = bool(self.blocked)
            await self.close()
            if self.auth_required:
                raise ValueError(SIGN_IN_REQUIRED) from None
            if type(exc) is ValueError:
                raise
            raise ValueError(
                "Source reading was blocked; it may require sign-in or an unsupported page action. "
                "Reconnect in Sources if needed; cached evidence is retained."
                if blocked
                else "Browser reading failed or timed out; cached evidence is retained. "
                "Check the source connection and retry."
            ) from None

    async def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.blocked.add("Some dynamic requests were blocked by the read-only browser")
            return await route.abort()
        reading_view = self.course["provider"] == "brightspace" and parsed.path in BRIGHTSPACE_VIEWS
        unsafe_query = any(
            (key != "start" and WRITE.search(key))
            or (key.casefold() in {"action", "command", "operation", "op"} and WRITE.search(value))
            for key, value in parse_qsl(parsed.query)
        )
        if (WRITE.search(unquote(parsed.path)) and not reading_view) or unsafe_query:
            self.blocked.add("A non-reading request was blocked")
            return await route.abort()
        if request.is_navigation_request() and not self.policy.allows(request.url):
            if (
                self.page
                and request.frame == self.page.main_frame
                and re.search(
                    r"/(?:login|signin|sign-in|d2l/login|d2l/lp/auth)(?:/|$)", parsed.path, re.I
                )
            ):
                self.auth_required = True
            self.blocked.add("A page outside the selected course or a sign-in redirect was blocked")
            return await route.abort()
        if parsed.scheme != "https" or parsed.port not in (None, 443) or not parsed.hostname:
            return await route.abort()
        if request.resource_type in {"image", "media", "font"}:
            return await route.abort()
        if parsed.hostname not in self.addresses:
            try:
                await public_address(parsed.hostname)
            except (ValueError, OSError):
                self.blocked.add("A non-public or unavailable network address was blocked")
                return await route.abort()
            self.addresses.add(parsed.hostname)
        await route.fallback()

    async def open(self, args):
        if len(args) != 1 or not all(isinstance(v, str) and v for v in args.values()):
            raise ValueError("Choose exactly one task_id, source_course_id or document_id")
        task = self.tasks.get(args["task_id"]) if "task_id" in args else None
        if "document_id" in args and args["document_id"] not in self.documents:
            raise ValueError("Choose a cached course material returned in this question")
        document = self.service.store.get(args["document_id"]) if "document_id" in args else None
        if "document_id" in args and (
            not document
            or document.get("kind") in {"mail", "email"}
            or document.get("provider") == "gmail"
        ):
            raise ValueError("Choose a cached course material returned by search_knowledge")
        cid = (
            task.get("source_course_id")
            if task
            else document.get("course_id")
            if document
            else args["source_course_id"]
        )
        course = next((c for c in self.courses() if c["id"] == cid), None)
        if not course:
            raise ValueError("Course is outside this conversation or has no browser reader")
        connector = getattr(self.service.engine, "connectors", {}).get(course["provider"])
        connector = getattr(connector, "active", connector)
        browser = getattr(connector, "browser", None)
        if not browser or connector.connection_status() != "connected":
            raise ValueError("Connect this source and finish login in Sources, then retry")
        await self.close()
        self.course = course
        self.connector = connector
        self.policy = CourseNavigation(
            course,
            [t.get("url") for t in self.service.db.tasks() if t.get("source_course_id") == cid],
        )
        url = (
            task.get("url")
            if task
            else document.get("url")
            if document
            else course.get("source_url")
        )
        if document and self.is_document(url):
            return await self.read_attachment(
                {
                    "url": clean_url(url),
                    "label": document["title"],
                }
            )
        if not self.policy.allows(url):
            raise ValueError("This source URL needs a specific reader; use its source link")
        self.blocked = set()
        self.auth_required = False
        # The same locks as automatic sync/login protect the persistent Chrome profile.
        await self.stack.enter_async_context(self.service.engine.queue_lock)
        await self.stack.enter_async_context(self.service.engine.locks[course["provider"]])
        if getattr(self.service.engine, "paused", False):
            raise ValueError("Source reading is paused")
        if not any(c["id"] == cid for c in self.courses()):
            raise ValueError("Course is no longer available in this conversation")
        if connector.connection_status() != "connected":
            raise ValueError("Source connection changed; finish login in Sources, then retry")
        self.context = await self.stack.enter_async_context(
            browser.session(connector.config.get("timezone"), read_only=True)
        )
        if course["provider"] == "brightspace" and hasattr(connector, "restore_browser_session"):
            # Cold profiles need the connector's fixed school SSO flow, including
            # authentication POSTs. No model URL/control is used during restoration.
            try:
                async with asyncio.timeout(30):
                    await connector.restore_browser_session(self.context)
            except Exception:
                raise ValueError(
                    "Brightspace sign-in could not be restored. Reconnect this source in "
                    "Sources, then retry; cached evidence is retained."
                ) from None
            # Retain the restored tab's session state while entering the course.
            self.page = next(
                (
                    page
                    for page in reversed(self.context.pages)
                    if urlsplit(page.url).netloc == urlsplit(connector.base_url).netloc
                    and urlsplit(page.url).path.startswith("/d2l/home")
                ),
                None,
            )
            if not self.page:
                raise ValueError("The restored source page is unavailable; reconnect in Sources")
            for page in self.context.pages:
                if page != self.page:
                    await page.close()
        await self.context.route("**/*", self.route)
        if hasattr(self.context, "route_web_socket"):
            await self.context.route_web_socket("**/*", lambda socket: socket.close())
        if course["provider"] == "webassign":
            session = connector.vault.get("webassign_session") or {}
            if session.get("cookies"):
                await self.context.add_cookies(session["cookies"])
            url = connector.session_url(url)
        self.page = self.page or await self.context.new_page()
        self.page.set_default_timeout(8000)
        self.page.on("dialog", lambda dialog: dialog.dismiss())
        self.context.on("page", self.close_popup)
        if document and course["provider"] == "brightspace":
            attachment = await self.material_attachment(url)
            if attachment:
                return await self.read_attachment(attachment)
        await self.navigate(url)

    async def material_attachment(self, url):
        # Classic topic links redirect to the Lessons SPA, which may not expose
        # attachment text. Use the connector's existing GET topic metadata format.
        parsed = urlsplit(url)
        identifier = dict(parse_qsl(parsed.query)).get("itemIdentifier", "")
        match = re.fullmatch(r"D2L\.LE\.Content\.ContentObject\.TopicCO-(\d+)", identifier)
        cid = str(self.course["external_id"])
        if not match or parsed.path != f"/d2l/le/content/{cid}/Home":
            return None
        version = str(self.connector.config.get("le_version", "1.82"))
        if not re.fullmatch(r"\d+\.\d+", version):
            raise ValueError("The source content API version is invalid")
        response = await self.context.request.get(
            self.connector.base_url + f"/d2l/api/le/{version}/{cid}/content/topics/{match[1]}",
            timeout=20000,
            max_redirects=0,
        )
        if response.status != 200:
            raise ValueError("The material source could not be read; cached evidence is retained")
        metadata = await response.json()
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("Id")) != match[1]
            or metadata.get("Type") != 1
            or metadata.get("IsHidden") is not False
            or metadata.get("IsLocked") is not False
        ):
            raise ValueError("The material source identity or visibility could not be confirmed")
        path = static_file_path(metadata.get("Url", ""), self.connector.base_url, cid)
        if path:
            return {
                "url": urljoin(self.connector.base_url, path),
                "label": redact(str(metadata.get("Title") or self.course["name"]))[:300],
            }
        return None

    async def close_popup(self, page):
        if page != self.page:
            await page.close()

    async def navigate(self, url):
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        if response and response.status >= 400:
            raise ValueError(
                "Source page is unavailable or needs sign-in; cached evidence is retained"
            )
        # A bounded settle catches basic async rendering; no claim that SPA activity is complete.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=3500)
        except Exception:
            self.blocked.add("The page was still loading when read")

    async def follow(self, args):
        if getattr(self.service.engine, "paused", False):
            raise ValueError("Source reading is paused")
        if not self.page or args.get("snapshot_id") != self.snapshot_id:
            raise ValueError("Page changed or closed; reopen it and use its latest targets")
        if not any(c["id"] == self.course["id"] for c in self.courses()):
            raise ValueError("Course is no longer available in this conversation")
        target = self.targets.get(args.get("target_id"))
        if not target:
            raise ValueError("Choose a target returned by the latest browser snapshot")
        self.blocked = set()
        self.auth_required = False
        if target["kind"] == "document":
            return await self.read_attachment(target)
        if target["kind"] == "link":
            url = target["url"]
            if not self.policy.allows(url):
                raise ValueError("This link is outside the selected course")
            if self.course["provider"] == "webassign":
                url = self.connector.session_url(url)
            await self.navigate(url)
        else:
            element = target["element"]
            label = await element.evaluate(
                "e => (e.getAttribute('aria-label') || e.innerText || '').trim().slice(0,180)"
            )
            if label != target["label"] or not await element.is_visible():
                raise ValueError("The control changed; reopen the page before using it")
            await element.click(timeout=8000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=3500)
            except Exception:
                self.blocked.add("The page was still loading when read")

    async def read_attachment(self, target):
        if getattr(self.service.engine, "paused", False):
            raise ValueError("Source reading is paused")
        course = self.course
        await self.close()  # The existing attachment reader acquires the same source locks.
        document = await self.service.link_reader.read(
            {
                "id": "browser-course:" + course["id"],
                "provider": course["provider"],
                "course_external_id": course["external_id"],
                "source_course_id": course["id"],
            },
            {"link_id": uuid4().hex, "url": target["url"], "title": target["label"]},
        )
        document.pop("source_task_id", None)  # Discovery is not a confirmed task association.
        document["body"] = redact(document["body"])
        key = uuid4().hex
        self.snapshots[key] = (document, [])
        return self.read({"snapshot_id": key})

    async def snapshot(self):
        if self.auth_required:
            raise ValueError(SIGN_IN_REQUIRED)
        if not self.policy.allows(self.page.url):
            raise ValueError(
                "Source redirected outside this course; reconnect in Sources if needed"
            )
        if await self.page.locator('input[type="password"],input[type="email"]').count():
            raise ValueError("Source needs sign-in; reconnect in Sources, then retry")
        self.snapshot_id = uuid4().hex
        self.targets = {}
        bodies, warnings = [], ["Only the visited page was checked; course coverage is unverified"]
        unread_links = unread_controls = 0
        frames = self.page.frames
        for frame in frames[:8]:
            if not self.policy.allows(frame.url):
                warnings.append("An embedded frame could not be read within this course")
                continue
            try:
                handle = await frame.evaluate_handle(SNAPSHOT)
                text = await (await handle.get_property("text")).json_value()
                bodies.append(redact(text))
                if await (await handle.get_property("truncated")).json_value():
                    warnings.append("Page text or controls exceeded the snapshot limit")
                if await (await handle.get_property("visualContent")).json_value():
                    warnings.append(
                        "Image, canvas, audio or video content was not read; "
                        "available image descriptions are included"
                    )
                controls = await (await handle.get_property("controls")).get_properties()
                for candidate in controls.values():
                    item = {
                        key: await (await candidate.get_property(key)).json_value()
                        for key in ("label", "kind", "role", "href", "nativeDisclosure")
                    }
                    item["label"] = redact(item["label"])
                    if item["kind"] == "link":
                        if self.is_document(item["href"]):
                            item["kind"] = "document"
                        elif not self.policy.allows(item["href"]):
                            unread_links += 1
                            continue
                        item["url"] = clean_url(item.pop("href"))
                    elif not permitted_control(item):
                        unread_controls += 1
                        continue
                    else:
                        item["element"] = (await candidate.get_property("element")).as_element()
                    self.targets[uuid4().hex[:16]] = item
                await handle.dispose()
            except Exception:
                warnings.append("Some page content or controls could not be read")
        if len(frames) > 8:
            warnings.append("Additional embedded frames were not read")
        if unread_links:
            warnings.append(
                f"{unread_links} visible links were not followed because they are outside "
                "the supported reading scope; linked content remains unverified"
            )
        if unread_controls:
            warnings.append(
                f"{unread_controls} visible controls are unsupported by the read-only browser; "
                "content behind these controls remains unverified"
            )
        body = "\n\n".join(bodies)
        if not body.strip():
            raise ValueError(
                "No readable page text was found; this does not prove the page is empty"
            )
        stamp, url = now().isoformat(), clean_url(self.page.url)
        title = redact((await self.page.title())[:300]) or self.course["name"]
        if re.match(r"\s*(?:Loading\b|正在加载|加载中)", title, re.I):
            warnings.append(
                "Source content is still loading; only the page shell may have been read"
            )
        document = {
            "id": "browser:" + hashlib.sha256((url + body).encode()).hexdigest()[:24],
            "title": title,
            "body": body,
            "url": url,
            "course_id": self.course["id"],
            "provider": self.course["provider"],
            "kind": "browser_page",
            "fetched_at": stamp,
            "checked_at": stamp,
            "complete": False,
            "warnings": list(dict.fromkeys(warnings + sorted(self.blocked))),
            "evidence": "on_demand_browser",
        }
        targets = [
            {"target_id": key, **{k: v for k, v in item.items() if k in {"label", "kind", "url"}}}
            for key, item in self.targets.items()
        ]
        self.snapshots[self.snapshot_id] = (document, targets)
        return self.read({"snapshot_id": self.snapshot_id})

    def is_document(self, url):
        clean = reference_url(url) if isinstance(url, str) else None
        if not clean or not clean_url(clean):
            return False
        p = urlsplit(clean)
        if p.hostname == "docs.google.com":
            return bool(re.fullmatch(r"/document/d/[A-Za-z0-9_-]+/(?:edit|view|preview)", p.path))
        if self.course["provider"] == "brightspace":
            if static_file_path(clean, self.connector.base_url, self.course["external_id"]):
                return True
        # Public static documents use the existing unauthenticated, address-checked reader.
        # Never hand a different course's LMS URL to that reader as a public attachment.
        protected_hosts = {
            urlsplit(getattr(getattr(c, "active", c), "base_url", "")).hostname
            for c in getattr(self.service.engine, "connectors", {}).values()
        } | {urlsplit(self.course.get("source_url") or "").hostname}
        return bool(
            p.hostname not in protected_hosts
            and p.hostname
            not in {
                "classroom.google.com",
                "drive.google.com",
                "mail.google.com",
            }
            and not p.query
            and not WRITE.search(unquote(p.path))
            and re.search(r"\.(?:pdf|txt|html?)$", p.path, re.I)
        )

    def read(self, args):
        saved = self.snapshots.get(args.get("snapshot_id"))
        if not saved:
            raise ValueError("Choose a snapshot returned in this question")
        document, targets = saved
        if not any(c["id"] == document["course_id"] for c in self.courses()):
            raise ValueError("Course is no longer available in this conversation")
        offset, target_offset = args.get("offset", 0), args.get("target_offset", 0)
        if any(type(n) is not int or n < 0 for n in (offset, target_offset)):
            raise ValueError("Invalid snapshot offset")
        documents = self.expose([document], offset=offset)
        if not documents:
            raise ValueError("Evidence limit reached; continue in a narrower question")
        return {
            "snapshot_id": args["snapshot_id"],
            "documents": documents,
            "targets": targets[target_offset : target_offset + 40],
            "target_offset": target_offset,
            "total_targets": len(targets),
            "next_target_offset": (
                target_offset + 40 if target_offset + 40 < len(targets) else None
            ),
            "browser_visit": {k: document[k] for k in ("url", "title", "checked_at", "warnings")},
        }
