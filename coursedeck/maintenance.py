"""Coordinate recovery with in-flight local requests without blocking the heartbeat."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException


class MaintenanceGate:
    def __init__(self, recovery_required=lambda: False, drain_timeout=30):
        self.recovery_required = recovery_required
        self.drain_timeout = drain_timeout
        self.busy = False
        self.active_requests = 0
        self.condition = asyncio.Condition()
        self.owner = asyncio.Lock()

    @asynccontextmanager
    async def request(self):
        async with self.condition:
            admitted = not self.busy and not self.recovery_required()
            if admitted:
                self.active_requests += 1
        try:
            yield admitted
        finally:
            if admitted:
                async with self.condition:
                    self.active_requests -= 1
                    self.condition.notify_all()

    @asynccontextmanager
    async def maintenance(self):
        if self.owner.locked():
            raise HTTPException(409, "A backup or restore is already in progress")
        async with self.owner:
            async with self.condition:
                self.busy = True
            try:
                async with self.condition:
                    try:
                        await asyncio.wait_for(
                            self.condition.wait_for(lambda: self.active_requests == 0),
                            self.drain_timeout,
                        )
                    except TimeoutError as exc:
                        raise HTTPException(
                            409, "An operation is still running. Retry recovery when it finishes."
                        ) from exc
                yield
            finally:
                async with self.condition:
                    self.busy = False
                    self.condition.notify_all()
