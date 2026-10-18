import asyncio

import pytest
from fastapi import HTTPException

from coursedeck.maintenance import MaintenanceGate


@pytest.mark.asyncio
async def test_recovery_drains_existing_requests_and_rejects_new_work():
    gate = MaintenanceGate()
    entered, release, restoring = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def existing():
        async with gate.request() as admitted:
            assert admitted
            entered.set()
            await release.wait()

    async def restore():
        async with gate.maintenance():
            assert gate.active_requests == 0
            restoring.set()

    request = asyncio.create_task(existing())
    await entered.wait()
    recovery = asyncio.create_task(restore())
    await asyncio.sleep(0)
    assert gate.busy and not restoring.is_set()
    async with gate.request() as admitted:
        assert not admitted
    release.set()
    await asyncio.gather(request, recovery)
    assert restoring.is_set() and not gate.busy
    async with gate.request() as admitted:
        assert admitted


@pytest.mark.asyncio
async def test_restore_timeout_does_not_cancel_existing_operation_or_leave_gate_busy():
    gate = MaintenanceGate(drain_timeout=0.001)
    async with gate.request() as admitted:
        assert admitted
        with pytest.raises(HTTPException) as error:
            async with gate.maintenance():
                pytest.fail("Must not restore while a request is still writing")
        assert error.value.status_code == 409
        assert gate.active_requests == 1 and not gate.busy


@pytest.mark.asyncio
async def test_unresolved_restore_journal_blocks_regular_requests_but_allows_recovery():
    pending = True
    gate = MaintenanceGate(lambda: pending)
    async with gate.request() as admitted:
        assert not admitted
    async with gate.maintenance():
        pending = False
    async with gate.request() as admitted:
        assert admitted
