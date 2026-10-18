"""Bounded retries for a material read, without repeating assignment synchronization."""

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..domain import Outcome
from .http import TransportError

RETRY_DELAYS = (0.5, 2)


def retry_after_seconds(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


async def retry_material_read(operation, *, errors, scope, stage, delays=None):
    delays = RETRY_DELAYS if delays is None else delays
    for attempt in range(len(delays) + 1):
        try:
            return await operation()
        except TransportError as error:
            transient = error.outcome in {Outcome.NETWORK_ERROR, Outcome.RATE_LIMITED}
            wait = getattr(error, "retry_after", 0)
            if not transient or attempt == len(delays) or wait > 10:
                errors.append(
                    {
                        "category": error.outcome.value,
                        "scope": scope,
                        "stage": stage,
                        "retry_attempt": attempt,
                    }
                )
                raise
            await asyncio.sleep(max(delays[attempt], wait))
