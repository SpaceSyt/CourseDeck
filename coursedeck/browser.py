import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright


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

    def __init__(self, data_dir: Path, provider: str, channel: str | None = None):
        if provider not in {"google_classroom", "gradescope", "webassign", "brightspace", "gmail"}:
            raise ValueError("Unknown browser profile")
        self.root = (data_dir / "browser-profiles").resolve()
        self.path = (self.root / provider).resolve()
        self.channel = channel
        self.playwright = None
        self.interactive: BrowserContext | None = None

    def exists(self):
        return self.path.is_dir()

    async def launch(self, headless: bool, timezone: str | None = None):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        self.path.mkdir(parents=True, exist_ok=True)
        return await self.playwright.chromium.launch_persistent_context(
            str(self.path),
            headless=headless,
            channel=self.channel,
            timezone_id=timezone,
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-background-networking"],
        )

    async def login(self, url: str, timezone: str | None = None):
        if self.interactive is not None:
            try:
                if self.interactive.pages:
                    await self.interactive.pages[-1].bring_to_front()
                    return
            except Exception:
                pass
            await self.close_login()
        self.interactive = await self.launch(False, timezone)
        try:
            page = (
                self.interactive.pages[0]
                if self.interactive.pages
                else await self.interactive.new_page()
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except BaseException:
            await self.close_login()
            raise

    async def close_login(self):
        context, self.interactive = self.interactive, None
        if context is not None:
            await context.close()

    @asynccontextmanager
    async def session(self, timezone: str | None = None):
        if self.interactive is not None:
            raise ValueError("Finish interactive login before background sync")
        context = await self.launch(True, timezone)
        try:
            yield context
        finally:
            await context.close()

    async def reset(self):
        await self.close_login()
        # Explicitly resolve and confine recursive deletion to this provider's profile.
        target = self.path.resolve()
        if target.parent != self.root or target.name not in {
            "google_classroom",
            "gradescope",
            "webassign",
            "brightspace",
            "gmail",
        }:
            raise ValueError("Refusing to reset a path outside the profile root")
        if target.exists():
            shutil.rmtree(target)

    async def close(self):
        await self.close_login()
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
