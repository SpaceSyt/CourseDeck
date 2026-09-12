"""Read one evidenced task link on demand. Bodies live only in the current chat turn."""

import asyncio
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .connectors.brightspace_content import document_text, static_file_path
from .connectors.classroom_materials import read_google_doc_text, reference_url
from .domain import now

MAX_BYTES = 2_000_000


def task_links(task, documents):
    links = {}

    def add(url, title, via):
        url = reference_url(url) if isinstance(url, str) else None
        if not url or len(links) >= 64:
            return
        key = hashlib.sha256((task["id"] + url).encode()).hexdigest()[:24]
        links.setdefault(key, {"link_id": key, "url": url, "title": str(title)[:300], "via": via})

    def text_links(body, via):
        if not isinstance(body, str):
            return
        for match in re.finditer(r'https?://[^\s<>"\']+', body):
            add(match[0].rstrip(".,);]"), "Linked page", via)
        if "<a" in body:
            for link in BeautifulSoup(body, "html.parser").select("a[href]"):
                add(link["href"], link.get_text(" ", strip=True) or "Linked page", via)

    def raw_links(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"url", "alternatelink", "href", "link"} and isinstance(
                    child, str
                ):
                    add(
                        child,
                        value.get("title") or value.get("Name") or "Attachment",
                        "task_attachment",
                    )
                elif key.lower() in {"html", "text", "description"}:
                    text_links(child, "task_body")
                elif isinstance(child, (dict, list)):
                    raw_links(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                raw_links(child, depth + 1)

    add(task.get("url"), task["title"], "task")
    text_links(task.get("description"), "task_body")
    raw_links(task.get("raw_data", {}))
    for document in documents:
        via = document.get("related_via", "related_material")
        add(document.get("url"), document["title"], via)
        text_links(document.get("body"), via)
    return list(links.values())


async def public_address(host):
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Local and private network links cannot be read")
    return ips[0]


class PublicTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        # Pin the validated address; retain TLS hostname verification and the Host header.
        host = request.url.host
        address = await public_address(host)
        request.headers["host"] = request.url.netloc.decode()
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=address)
        return await super().handle_async_request(request)


async def public_body(url, transport=None):
    async with httpx.AsyncClient(
        transport=transport or PublicTransport(), timeout=20, trust_env=False
    ) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.port not in (None, 443) or not reference_url(url):
                raise ValueError("This link is not a supported public HTTPS document")
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Linked file exceeds the 2 MB reading limit")
                return bytes(data), response.headers.get("content-type", "")
    raise ValueError("Linked page redirected too many times")


class TaskLinkReader:
    def __init__(self, engine):
        self.engine = engine

    async def read(self, task, link):
        url = link["url"]
        target = urlsplit(url)
        provider = task["provider"]
        connectors = getattr(self.engine, "connectors", {})
        connector = getattr(connectors.get(provider), "active", connectors.get(provider))
        base = getattr(connector, "base_url", "")
        static = (
            static_file_path(url, base, task.get("course_external_id", ""))
            if base and provider == "brightspace"
            else None
        )
        google_doc = target.hostname == "docs.google.com" and bool(
            re.fullmatch(r"/document/d/[A-Za-z0-9_-]+/(?:edit|view|preview)", target.path)
        )
        warnings = []
        if static or google_doc:
            if google_doc and provider != "google_classroom":
                candidate = connectors.get("google_classroom")
                connector = getattr(candidate, "active", candidate)
                provider = "google_classroom"
            browser = getattr(connector, "browser", None)
            if not browser or connector.connection_status() != "connected":
                raise ValueError("Connect the source to read this protected document")
            async with self.engine.queue_lock, self.engine.locks[provider]:
                if getattr(self.engine, "paused", False):
                    raise ValueError("Source reading is paused")
                async with browser.session(connector.config.get("timezone")) as context:
                    if google_doc:
                        text = await read_google_doc_text(context, url)
                        if not text:
                            raise ValueError(
                                "Google document could not be read; open its source link"
                            )
                    else:
                        await connector.restore_browser_session(context)
                        response = await context.request.get(url, timeout=20000, max_redirects=0)
                        if response.status != 200:
                            raise ValueError("Linked file is unavailable in the source")
                        body = await response.body()
                        if len(body) > MAX_BYTES:
                            raise ValueError("Linked file exceeds the 2 MB reading limit")
                        text, warnings = await asyncio.to_thread(
                            document_text, body, response.headers.get("content-type", "")
                        )
        else:
            protected_hosts = {
                urlsplit(getattr(getattr(c, "active", c), "base_url", "")).hostname
                for c in connectors.values()
            }
            if target.hostname in protected_hosts or target.hostname in {
                "classroom.google.com",
                "docs.google.com",
                "drive.google.com",
                "mail.google.com",
            }:
                raise ValueError(
                    "This source page needs its specific reader; use cached evidence or Open source"
                )
            body, mime = await public_body(url)
            if "html" in mime:
                soup = BeautifulSoup(body, "html.parser")
                if soup.select_one('input[type="password"]'):
                    raise ValueError("This linked page requires login")
            text, warnings = await asyncio.to_thread(document_text, body, mime)
        stamp = now().isoformat()
        return {
            "id": "link:" + link["link_id"],
            "title": link["title"],
            "url": url,
            "body": text,
            "course_id": task.get("source_course_id"),
            "provider": task["provider"],
            "source_task_id": task["id"],
            "kind": "linked_document",
            "fetched_at": stamp,
            "checked_at": stamp,
            "complete": not warnings,
            "warnings": warnings,
            "evidence": "on_demand_link",
        }
