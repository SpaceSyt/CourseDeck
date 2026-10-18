"""Restore browser-session cookies without rolling back a refreshed profile."""

from time import time


def _identity(cookie):
    return (
        cookie["name"],
        cookie["domain"],
        cookie.get("path", "/"),
        cookie.get("partitionKey"),
    )


async def restore_session_cookies(context, saved):
    if not saved:
        return
    current = {_identity(cookie): cookie for cookie in await context.cookies()}
    # A changed cookie proves the profile has moved beyond the backup. Mixing
    # old missing authentication cookies into that profile can invalidate it.
    if any(
        identity in current and current[identity]["value"] != cookie["value"]
        for cookie in saved
        for identity in [_identity(cookie)]
    ):
        return
    now = time()
    missing = [
        cookie
        for cookie in saved
        if _identity(cookie) not in current
        and (cookie.get("expires", -1) == -1 or cookie["expires"] > now)
    ]
    if missing:
        await context.add_cookies(missing)
