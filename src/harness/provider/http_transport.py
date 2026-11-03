from __future__ import annotations

import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx


_prompt_logging_enabled = False


def configure_prompt_logging(enabled: bool) -> None:
    """Configure process-wide request logging, including new/switched providers."""
    global _prompt_logging_enabled
    _prompt_logging_enabled = enabled


async def _log_prompt(request: httpx.Request, *, provider: str) -> None:
    # Read the serialized wire context, not a reconstruction of agent history.
    # Headers, URLs, and unrelated generation/configuration settings are excluded.
    payload = json.loads(request.content)
    context = {
        key: payload[key] for key in ("model", "system", "messages", "tools") if key in payload
    }
    print(json.dumps({"provider": provider, **context}, ensure_ascii=False), file=sys.stderr)


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
            if _prompt_logging_enabled:

                async def log_request(request: httpx.Request) -> None:
                    await _log_prompt(request, provider=provider)

                client.event_hooks["request"].append(log_request)
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
