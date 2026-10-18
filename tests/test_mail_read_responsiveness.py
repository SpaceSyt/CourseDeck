import asyncio
import threading

import httpx

from coursedeck.app import create_app
from coursedeck.mail import MailStore


async def test_mail_read_does_not_block_heartbeat_or_snapshot(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    captured = []

    def slow_list(self, deleted, ignored, offset, limit, **filters):
        captured.append((deleted, ignored, offset, limit, filters))
        started.set()
        release.wait(3)
        return {"messages": [], "total": 0, "has_more": False}

    monkeypatch.setattr(MailStore, "list", slow_list)
    app = create_app(tmp_path)
    maintenance_entered = asyncio.Event()

    async def maintenance():
        async with app.state.maintenance_gate.maintenance():
            maintenance_entered.set()

    maintenance_job = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        mail = asyncio.create_task(
            client.get(
                "/api/mail?ignored=true&offset=10&limit=20&attention_only=true&order=attention"
            )
        )
        try:
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            assert not mail.done()
            heartbeat, snapshot = await asyncio.wait_for(
                asyncio.gather(client.get("/api/heartbeat"), client.get("/api/snapshot")),
                timeout=1,
            )
            assert heartbeat.status_code == snapshot.status_code == 200
            assert not mail.done()
            maintenance_job = asyncio.create_task(maintenance())
            await asyncio.sleep(0)
            assert app.state.maintenance_gate.busy
            assert not maintenance_entered.is_set()
        finally:
            release.set()
            response = await mail
            if maintenance_job:
                await asyncio.wait_for(maintenance_job, timeout=1)
        assert response.status_code == 200
        assert maintenance_entered.is_set()
        assert captured == [(False, True, 10, 20, {"attention_only": True, "order": "attention"})]
