from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx


def _has_dns_cause(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return False


def _transport_category(exc: httpx.TransportError) -> str:
    import httpx

    for error_type, category in (
        (httpx.ConnectTimeout, "connection timed out"),
        (httpx.ReadTimeout, "response read timed out"),
        (httpx.WriteTimeout, "request write timed out"),
        (httpx.PoolTimeout, "connection pool timed out"),
        (httpx.TimeoutException, "transport timed out"),
    ):
        if isinstance(exc, error_type):
            return category
    if isinstance(exc, httpx.ConnectError):
        return "DNS resolution failed" if _has_dns_cause(exc) else "connection failed"
    if isinstance(exc, httpx.ReadError):
        return "response read failed"
    if isinstance(exc, httpx.WriteError):
        return "request write failed"
    if isinstance(exc, httpx.ProtocolError):
        return "HTTP protocol error"
    if isinstance(exc, httpx.ProxyError):
        return "proxy connection failed"
    return "transport failure"


@asynccontextmanager
async def provider_http_client(
    *, provider: str, base_url: str, operation: Literal["request", "streaming request"]
) -> AsyncIterator[httpx.AsyncClient]:
    """Add safe context to transport failures, including errors while reading a stream."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            yield client
    except httpx.TransportError as exc:
        # Never interpolate the exception or full URL: either can contain secrets.
        try:
            hostname = urlsplit(base_url).hostname or "unknown host"
        except ValueError:
            hostname = "unknown host"
        raise RuntimeError(
            f"provider {operation} failed ({provider}; host={hostname}): {_transport_category(exc)}"
        ) from exc
