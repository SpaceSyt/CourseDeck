"""Opt-in HTTPS access; configuration and administrative operations stay local."""

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import ssl
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

COOKIE = "__Host-coursedeck"
SESSION_SECONDS = 8 * 60 * 60
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
LOCAL_ADMIN = (
    "/api/network",
    "/api/sources",
    "/api/oauth",
    "/api/desktop",
    "/api/recovery",
    "/api/collection",
    "/api/settings",
    "/api/backup",
    "/api/debug",
    "/api/tray",
    "/api/chat/config",
    "/api/mail/connection",
)
PUBLIC_API = {"/api/network/session", "/api/network/login", "/api/network/logout"}


class NetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    host: str = Field(default="", max_length=64)
    port: int = Field(default=48322, ge=1024, le=65535)
    certfile: str = Field(default="", max_length=2048)
    keyfile: str = Field(default="", max_length=2048)
    password_salt: str = Field(default="", pattern=r"^(?:[0-9a-f]{32})?$", repr=False)
    password_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$", repr=False)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.host:
            address = ipaddress.IPv4Address(self.host)
            if not any(address in network for network in PRIVATE_NETWORKS):
                raise ValueError("Use an RFC1918 IPv4 address on this computer")
        if bool(self.password_salt) != bool(self.password_hash):
            raise ValueError("Incomplete password configuration")
        if self.enabled and not all((self.host, self.certfile, self.keyfile, self.password_hash)):
            raise ValueError("LAN access requires an address, TLS certificate/key and password")
        return self


class NetworkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    host: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1024, le=65535)
    certfile: str | None = Field(default=None, max_length=2048)
    keyfile: str | None = Field(default=None, max_length=2048)
    password: str | None = Field(default=None, min_length=16, max_length=256, repr=False)


def password_digest(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
    ).hex()


def validate_tls(config):
    if not all(
        Path(value).is_absolute() and Path(value).is_file()
        for value in (config.certfile, config.keyfile)
    ):
        raise ValueError("TLS certificate and key must be existing absolute file paths.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # A password callback prevents OpenSSL prompting in a tray process.
    context.load_cert_chain(config.certfile, config.keyfile, password=lambda: "")
    return context


class NetworkAccess:
    def __init__(self, directory):
        self.path = Path(directory) / "network.json"
        self.config = NetworkConfig()
        self.error = None
        self._running = None
        self._sessions = {}
        self._attempts = deque()
        self._lock = threading.Lock()
        try:
            if self.path.exists():
                if self.path.stat().st_size > 16384:
                    raise ValueError("Oversized configuration")
                self.config = NetworkConfig.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError):
            self.error = "LAN configuration could not be read. Local access only; save to repair."

    @property
    def active(self):
        return bool(self.config.enabled and self._running == self.config and not self.error)

    @property
    def active_url(self):
        return f"https://{self.config.host}:{self.config.port}" if self.active else None

    def status(self):
        return self.config.model_dump(exclude={"password_hash", "password_salt"}) | {
            "password_set": bool(self.config.password_hash),
            "active": self.active,
            "active_url": self.active_url,
            "restart_required": bool(
                self.config.enabled
                and not self.active
                or self._running is not None
                and self._running != self.config
            ),
            "error": self.error,
        }

    def starting(self):
        if not self.config.enabled or self.error:
            return None
        try:
            validate_tls(self.config)
        except (OSError, ValueError):
            self.error = "LAN TLS files could not be loaded. Local access remains available."
            return None
        return self.config.model_copy()

    def mark_started(self, host, port):
        if (host, port) != (self.config.host, self.config.port):
            raise ValueError("LAN listener does not match configuration")
        self._running = self.config.model_copy()
        self.error = None

    def mark_stopped(self, error=None):
        self._running = None
        self._sessions.clear()
        self.error = error

    def save(self, patch):
        with self._lock:
            return self._save(patch)

    def _save(self, patch):
        fields = patch.model_dump(exclude_unset=True, exclude={"password"})
        if any(value is None for value in fields.values()):
            raise ValueError("Network settings cannot be null.")
        fields = self.config.model_dump() | fields
        if patch.password is not None:
            fields["password_salt"] = secrets.token_hex(16)
            fields["password_hash"] = password_digest(patch.password, fields["password_salt"])
        candidate = NetworkConfig.model_validate(fields)
        if candidate.enabled:
            validate_tls(candidate)
        temporary = self.path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(candidate.model_dump(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        self.config = candidate
        self.error = None
        self._sessions.clear()
        return self.status()

    def authenticated(self, connection):
        token = connection.cookies.get(COOKIE, "")
        if not self.active or len(token) != 43:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        return self._sessions.get(key, 0) > time.monotonic()

    def allow_attempt(self, peer):
        now = time.monotonic()
        while self._attempts and self._attempts[0][0] <= now - 60:
            self._attempts.popleft()
        if len(self._attempts) >= 30 or sum(ip == peer for _, ip in self._attempts) >= 5:
            return False
        self._attempts.append((now, peer))
        return True

    def login(self, password):
        config = self.config
        if not self.active or not hmac.compare_digest(
            password_digest(password, config.password_salt), config.password_hash
        ):
            return None
        with self._lock:
            if not self.active or config != self.config:
                return None
            now = time.monotonic()
            self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}
            if len(self._sessions) >= 128:
                self._sessions.pop(next(iter(self._sessions)))
            token = secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_SECONDS
            return token

    def logout(self, connection):
        token = connection.cookies.get(COOKIE, "")
        self._sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def loopback(peer):
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def private_peer(peer):
    try:
        address = ipaddress.IPv4Address(peer)
        return any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


class NetworkBoundary:
    """Check the socket peer and exact authority before any routes, files or streams."""

    def __init__(self, app, access):
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            # CourseDeck has no WebSocket endpoints. Future endpoints must opt into authentication.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)

        async def reject(status, detail):
            response = JSONResponse(
                {"detail": detail},
                status,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
            await response(scope, receive, send)

        authority = headers.get("host", "")
        try:
            parsed = urlsplit("//" + authority)
            port = parsed.port or (443 if scope["scheme"] == "https" else 80)
            if (
                len(headers.getlist("host")) != 1
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or not parsed.hostname
                or any(char.isspace() for char in authority)
            ):
                raise ValueError("Invalid host")
        except ValueError:
            await reject(400, "Invalid host header")
            return
        peer = (scope.get("client") or ("", 0))[0]
        local = (
            loopback(peer)
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and scope["scheme"] == "http"
        )
        if not local and not (
            self.access.active
            and (loopback(peer) or private_peer(peer))
            and scope["scheme"] == "https"
            and parsed.hostname == self.access.config.host
            and port == self.access.config.port
        ):
            await reject(
                403 if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else 400,
                "LAN access is disabled or the address is not allowed",
            )
            return
        origin = headers.get("origin")
        expected_origin = f"{scope['scheme']}://{authority}"
        path = scope["path"]
        oauth_return = (
            local
            and scope["method"] == "GET"
            and path.startswith("/api/oauth/")
            and path.endswith("/callback")
            and path.count("/") == 4
        )
        if (
            (origin and origin != expected_origin)
            or headers.get("sec-fetch-site") == "cross-site"
            and not oauth_return
            or len(headers.getlist("origin")) > 1
        ):
            await reject(403, "Cross-origin access is not allowed")
            return
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and (
            headers.get("x-coursedeck") != "1" or (not local and not origin)
        ):
            await reject(403, "Use the CourseDeck UI to make changes")
            return
        scope.setdefault("state", {})["network_local"] = local
        connection = HTTPConnection(scope)
        public_shell = scope["method"] in {"GET", "HEAD"} and (
            path == "/" or path.startswith("/assets/")
        )
        if not local and path not in PUBLIC_API and not public_shell:
            if not self.access.authenticated(connection):
                await reject(401, "Sign in to CourseDeck on this device")
                return
            recovery_status = path == "/api/recovery/status" and scope["method"] == "GET"
            if not recovery_status and any(
                path == prefix or path.startswith(prefix + "/") for prefix in LOCAL_ADMIN
            ):
                await reject(403, "This operation is available only on the CourseDeck computer")
                return

        revoked = False

        async def protected_send(message):
            nonlocal revoked
            if revoked:
                return
            private_response = not local and path not in PUBLIC_API and not public_shell
            if private_response and not self.access.authenticated(connection):
                if message["type"] == "http.response.start":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"cache-control", b"no-store")],
                        }
                    )
                    await send({"type": "http.response.body", "body": b""})
                else:
                    await send({"type": "http.response.body", "body": b""})
                revoked = True
                return
            if message["type"] == "http.response.start" and not local:
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, protected_send)


async def bounded_json(request):
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 8192:
            raise HTTPException(413, "Network request is too large")
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Not an object")
        return value
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Invalid network request") from exc


def build_network_router(access):
    router = APIRouter(prefix="/api/network")

    @router.get("")
    async def status():
        return access.status()

    @router.put("")
    async def configure(request: Request):
        value = await bounded_json(request)
        try:
            patch = NetworkPatch.model_validate(value)
            before = access.config
            await run_in_threadpool(access.save, patch)
            listener = getattr(request.app.state, "lan_server", None)
            if listener is not None and before != access.config:
                await listener.stop()
            return access.status()
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                400,
                "Use a private IPv4 address, port 1024–65535, valid TLS "
                "certificate/key paths and a password of 16–256 characters.",
            ) from exc
        except OSError as exc:
            raise HTTPException(400, "Could not read TLS files or save LAN settings.") from exc

    @router.get("/session")
    async def session(request: Request):
        local = request.state.network_local
        return {"local": local, "authenticated": local or access.authenticated(request)}

    @router.post("/login")
    async def login(request: Request):
        if request.state.network_local:
            raise HTTPException(400, "Use the HTTPS LAN address to sign in")
        if not access.allow_attempt(request.client.host):
            raise HTTPException(
                429,
                "Too many sign-in attempts. Try again in a minute.",
                headers={"Retry-After": "60"},
            )
        value = await bounded_json(request)
        password = value.get("password")
        if (
            set(value) != {"password"}
            or not isinstance(password, str)
            or not 16 <= len(password) <= 256
        ):
            raise HTTPException(401, "Invalid access password")
        token = await run_in_threadpool(access.login, password)
        if token is None:
            raise HTTPException(401, "Invalid access password")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request):
        access.logout(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    return router
