import asyncio
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

from .credentials import CredentialStore, IncompleteCredentialError, storage_failure_details
from .session_cookies import restore_session_cookies


class SessionPersistenceError(RuntimeError):
    """A fixed, safe error for unavailable durable browser-session storage."""

    def __init__(self, message=None, *, operation=None, cause=None):
        messages = {
            "read": (
                "Browser session could not be read from the OS credential store. "
                "Retry after checking the credential store."
            ),
            "restore": (
                "Saved browser session could not be restored into the browser. Retry this source."
            ),
            "capture": (
                "Browser session could not be captured before closing the browser. "
                "Retry this source."
            ),
            "save": (
                "Browser session could not be saved to the OS credential store. "
                "Retry after checking the credential store."
            ),
            "delete": (
                "Browser session could not be removed from the OS credential store. "
                "Retry after checking the credential store."
            ),
            "snapshot": "Saved browser session is incomplete. Reconnect this source to repair it.",
        }
        self.stage = f"session_{operation}" if operation in messages else "session_storage"
        self.category = (
            "session_incomplete"
            if operation == "snapshot"
            else f"{self.stage}_failed"
            if operation in messages
            else "session_storage"
        )
        self.safe_message = messages.get(
            operation,
            "Browser session storage is unavailable. Check the OS credential store and retry.",
        )
        self.diagnostics = {"stage": self.stage, "category": self.category}
        if cause is not None:
            self.diagnostics["storage"] = storage_failure_details(cause)
        super().__init__(self.safe_message)


class SessionSnapshotInvalid(SessionPersistenceError):
    """A damaged snapshot that explicit reconnect can replace without profile reset."""

    def __init__(self, message=None):
        super().__init__(operation="snapshot")


async def _complete_session_operation(operation):
    # Vault workers cannot be cancelled, and interrupted browser teardown can leave
    # a profile locked. Finish these operations before releasing provider access.
    task = asyncio.ensure_future(operation)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
    if cancelled is not None:
        raise cancelled
    return result


def installed_chrome_channel() -> str | None:
    """Use installed Chrome when available; always with our own separate profile."""
    if sys.platform == "win32":
        roots = [
            os.environ.get(key) for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        found = any(
            Path(root, "Google/Chrome/Application/chrome.exe").is_file() for root in roots if root
        )
    elif sys.platform == "darwin":
        found = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file()
    else:
        found = bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable"))
    return "chrome" if found else None


class BrowserManager:
    """One dedicated persistent profile. Callers serialize access via provider locks."""

    def __init__(self, data_dir: Path, provider: str, channel: str | None = None, *, vault=None):
        if provider not in {
            "google_classroom",
            "gradescope",
            "webassign",
            "brightspace",
            "rephactor",
            "gmail",
        }:
            raise ValueError("Unknown browser profile")
        self.root = (data_dir / "browser-profiles").resolve()
        self.path = (self.root / provider).resolve()
        self.channel = channel
        self.vault = vault if vault is not None else CredentialStore(data_dir)
        self.session_key = f"browser_session_{provider}"
        self._has_session_snapshot = False
        self._pending_session_save = None
        self.playwright = None
        self.interactive: BrowserContext | None = None

    def exists(self):
        return self.path.is_dir()

    def has_session_snapshot(self):
        return self._has_session_snapshot

    async def save_session(self, context):
        # Chromium drops session-only cookies on clean shutdown. Keep one current
        # snapshot in the OS vault, including an empty snapshot after sign-out.
        async def capture_and_save():
            try:
                cookies = await context.cookies()
            except Exception:
                raise SessionPersistenceError(operation="capture") from None
            await self._write_session(cookies)

        await _complete_session_operation(capture_and_save())

    async def _write_session(self, cookies):
        try:
            await _complete_session_operation(
                asyncio.to_thread(self.vault.set, self.session_key, {"cookies": cookies})
            )
        except Exception as exc:
            raise SessionPersistenceError(operation="save", cause=exc) from None
        self._has_session_snapshot = True

    async def _flush_session_save(self):
        task, self._pending_session_save = self._pending_session_save, None
        if task is not None:
            await _complete_session_operation(task)

    async def launch(self, headless: bool, timezone: str | None = None, *, read_only: bool = False):
        await self._flush_session_save()
        try:
            saved = await _complete_session_operation(
                asyncio.to_thread(self.vault.get, self.session_key)
            )
        except IncompleteCredentialError:
            raise SessionSnapshotInvalid(
                "Saved browser session is incomplete. Reconnect this source."
            ) from None
        except Exception as exc:
            raise SessionPersistenceError(operation="read", cause=exc) from None
        self._has_session_snapshot = saved is not None
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        self.path.mkdir(parents=True, exist_ok=True)
        context = await self.playwright.chromium.launch_persistent_context(
            str(self.path),
            headless=headless,
            channel=self.channel,
            timezone_id=timezone,
            accept_downloads=False,
            service_workers="block" if read_only else "allow",
            viewport={"width": 1280, "height": 900},
            args=["--disable-background-networking"],
        )
        try:
            if saved is not None:
                try:
                    await restore_session_cookies(context, saved.get("cookies", []))
                except Exception:
                    raise SessionPersistenceError(operation="restore") from None
            return context
        except BaseException:
            await _complete_session_operation(context.close())
            raise

    async def login(self, url: str, timezone: str | None = None):
        if self.interactive is not None:
            try:
                if self.interactive.pages:
                    await self.interactive.pages[-1].bring_to_front()
                    return
            except Exception:
                pass
            await self.close_login()
        try:
            self.interactive = await self.launch(False, timezone)
        except SessionSnapshotInvalid:
            # Explicit reconnect can discard a broken snapshot, retaining Chrome's
            # existing profile and all application data. A locked vault is not damage.
            await self._delete_session_snapshot()
            self.interactive = await self.launch(False, timezone)
        context = self.interactive

        def closed():
            # A window closed by the user must not block later background sync.
            if self.interactive is context:
                self.interactive = None
                # No cookies can be inspected once Chrome has closed. Do not replay
                # an older snapshot that could predate a manual sign-out.
                self._pending_session_save = asyncio.create_task(self._write_session([]))
                self._pending_session_save.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )

        context.once("close", closed)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except BaseException:
            await self.close_login()
            raise

    async def close_login(self):
        await self._flush_session_save()
        context, self.interactive = self.interactive, None
        if context is not None:
            try:
                await self.save_session(context)
            finally:
                await _complete_session_operation(context.close())

    @asynccontextmanager
    async def session(self, timezone: str | None = None, *, read_only: bool = False):
        if self.interactive is not None:
            raise ValueError("Finish interactive login before background sync")
        context = await self.launch(True, timezone, read_only=read_only)
        cancelled = None
        try:
            yield context
        except asyncio.CancelledError as exc:
            cancelled = exc
            raise
        finally:
            try:
                try:
                    await self.save_session(context)
                finally:
                    await _complete_session_operation(context.close())
            except Exception:
                # Cleanup must finish, but a vault failure must not turn a cancelled
                # source operation into a normally returned error result.
                if cancelled is not None:
                    raise cancelled from None
                raise

    async def reset(self):
        await self.close_login()
        # Explicitly resolve and confine recursive deletion to this provider's profile.
        target = self.path.resolve()
        if target.parent != self.root or target.name not in {
            "google_classroom",
            "gradescope",
            "webassign",
            "brightspace",
            "rephactor",
            "gmail",
        }:
            raise ValueError("Refusing to reset a path outside the profile root")
        if target.exists():
            shutil.rmtree(target)
        await self._delete_session_snapshot()

    async def _delete_session_snapshot(self):
        try:
            await _complete_session_operation(
                asyncio.to_thread(self.vault.delete, self.session_key)
            )
        except Exception as exc:
            raise SessionPersistenceError(operation="delete", cause=exc) from None
        self._has_session_snapshot = False

    async def close(self):
        try:
            await self.close_login()
        finally:
            playwright, self.playwright = self.playwright, None
            if playwright is not None:
                await _complete_session_operation(playwright.stop())
