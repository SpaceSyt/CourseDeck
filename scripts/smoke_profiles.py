"""Verify persistent browser storage without any real school account."""

import asyncio
import tempfile
from pathlib import Path

from coursedeck.browser import BrowserManager


async def main():
    with tempfile.TemporaryDirectory() as folder:
        manager = BrowserManager(Path(folder), "gradescope")
        async with manager.session() as context:
            await context.add_cookies(
                [
                    {
                        "name": "fixture",
                        "value": "synthetic",
                        "domain": "example.test",
                        "path": "/",
                        "expires": 2000000000,
                    }
                ]
            )
        async with manager.session() as context:
            assert any(c["name"] == "fixture" for c in await context.cookies())
        await manager.reset()
        assert not manager.exists()
        await manager.close()
    print("Persistent browser profile survives reopen; reset stays in its own directory.")


if __name__ == "__main__":
    asyncio.run(main())
