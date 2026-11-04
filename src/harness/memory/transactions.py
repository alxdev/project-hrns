"""Settle an already-started atomic write before delivering cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def settle_write(
    operation: Callable[[], T], on_success: Callable[[T], None] | None = None
) -> T:
    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            if worker.cancelled():
                # Shielding a worker cancelled during shutdown would spin.
                raise
            cancelled = True
            # Repeated cancellation still must not leave a write running after
            # the caller observes cancellation. Store lock waits are bounded.
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if on_success is not None:
        on_success(result)
    if cancelled:
        raise asyncio.CancelledError
    return result
