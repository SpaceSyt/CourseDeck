"""Verify persistent browser storage without any real school account."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock

from coursedeck.browser import BrowserManager


async def main():
    with tempfile.TemporaryDirectory() as folder:
        records = {}
        vault = Mock(get=records.get, set=records.__setitem__, delete=records.pop)
        manager = BrowserManager(Path(folder), "gradescope", vault=vault)
        async with manager.session() as context:
            await context.add_cookies(
                [
                    {
                        "name": "fixture",
                        "value": "synthetic",
                        "domain": "example.test",
                        "path": "/",
                        "expires": 2000000000,
                    },
                    {
                        "name": "session-fixture",
                        "value": "synthetic-session",
                        "domain": "example.test",
                        "path": "/",
                    },
                ]
            )
        async with manager.session() as context:
            assert {c["name"] for c in await context.cookies()} == {"fixture", "session-fixture"}
        await manager.reset()
        assert not manager.exists()
        await manager.close()
    print("Persistent browser profile survives reopen; reset stays in its own directory.")


if __name__ == "__main__":
    asyncio.run(main())
