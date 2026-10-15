from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class OperationResult(Generic[T]):
    value: T | None = None
    cancelled: bool = False


class OperationController:
    """Own cancellation and cleanup for one user-requested operation."""

    async def run(self, operation: Awaitable[T]) -> OperationResult[T]:
        operation_task = asyncio.ensure_future(operation)
        try:
            return OperationResult(value=await operation_task)
        except asyncio.CancelledError:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            current_task = asyncio.current_task()
            if current_task is not None:
                while current_task.cancelling():
                    current_task.uncancel()
            return OperationResult(cancelled=True)
