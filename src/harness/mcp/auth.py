from __future__ import annotations

import asyncio
import getpass
import os
import sys
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlparse

from .config import resolve_auth_headers


class MCPAuthState:
    """Process-local secrets, OAuth tokens, and callback state for MCP connections."""

    def __init__(self, prompt_secret: Callable[[str], Awaitable[str]] | None = None) -> None:
        self.prompt_secret = prompt_secret
        self.secrets: dict[str, str] = {}
        self._secret_locks: dict[str, asyncio.Lock] = {}
        self.token_storage: Any = None
        self.callback_server: asyncio.Server | None = None
        self.callback_path: str | None = None
        self.callback_result: asyncio.Future[Any] | None = None

    async def close(self) -> None:
        if self.callback_server is not None:
            self.callback_server.close()
            await self.callback_server.wait_closed()
            self.callback_server = None

    async def secret(self, variable: str) -> str:
        """Read a configured environment value or prompt once and retain it in memory."""
        value = os.environ.get(variable) or self.secrets.get(variable)
        if value:
            return value

        lock = self._secret_locks.setdefault(variable, asyncio.Lock())
        async with lock:
            value = os.environ.get(variable) or self.secrets.get(variable)
            if value:
                return value
            if self.prompt_secret is not None:
                value = await self.prompt_secret(variable)
            elif not sys.stdin.isatty() or not sys.stderr.isatty():
                raise ValueError(
                    f"required environment variable {variable} is not set; "
                    "run the harness in an interactive terminal to enter it for this session"
                )
            else:
                value = await asyncio.to_thread(
                    getpass.getpass, f"Enter {variable} (kept only for this session): "
                )
            if not value:
                raise ValueError(f"no value provided for {variable}")
            self.secrets[variable] = value
            return value


@asynccontextmanager
async def authenticated_client(
    url: str, auth: dict[str, object] | None, state: MCPAuthState
) -> AsyncIterator[Any]:
    """Open an MCP client using environment-backed headers or generic OAuth."""
    try:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise RuntimeError(
            "MCP authentication requires the installed MCP application dependency"
        ) from exc

    if auth is None or auth.get("type") in {"bearer", "headers"}:
        headers = await _resolve_auth_headers(auth, state)
        async with httpx2.AsyncClient(headers=headers) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport) as client:
                yield client
        return

    if auth.get("type") != "oauth":
        raise ValueError(f"unsupported MCP authentication type: {auth.get('type')}")

    async with _oauth_provider(url, auth, state) as oauth_provider:
        async with httpx2.AsyncClient(auth=oauth_provider) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport) as client:
                yield client


@asynccontextmanager
async def _oauth_provider(
    url: str, auth: dict[str, object], state: MCPAuthState
) -> AsyncIterator[Any]:
    """Create an OAuth authorization-code client and temporary loopback callback."""
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    client_id_name = str(auth["client_id_env"])
    client_id_value = await state.secret(client_id_name)
    client_secret_name = auth.get("client_secret_env")
    client_secret_value = (
        await state.secret(str(client_secret_name)) if client_secret_name is not None else None
    )
    client_id = client_id_value
    client_secret = client_secret_value
    token_auth_method = cast(
        Literal["none", "client_secret_basic", "client_secret_post"],
        str(auth.get("token_auth_method", "client_secret_post" if client_secret else "none")),
    )

    redirect_uri = str(auth["redirect_uri"])
    parsed = urlparse(redirect_uri)
    if parsed.hostname != "127.0.0.1" or parsed.port is None or not parsed.path:
        raise ValueError("OAuth redirect_uri must be http://127.0.0.1:<port>/<path>")
    callback_result: asyncio.Future[AuthorizationCodeResult] = (
        asyncio.get_running_loop().create_future()
    )
    state.callback_result = callback_result

    async def receive_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15)
            request = request_line.decode("ascii", errors="replace").split()
            for _ in range(64):
                header = await asyncio.wait_for(reader.readline(), timeout=5)
                if header in {b"\r\n", b"\n", b""}:
                    break
            if len(request) < 2 or request[0] != "GET":
                raise ValueError("expected an OAuth redirect GET request")
            returned = urlparse(request[1])
            if returned.path != parsed.path:
                raise ValueError("OAuth callback path did not match configured redirect_uri")
            query = parse_qs(returned.query)
            if "error" in query:
                raise ValueError("OAuth authorization failed: " + query["error"][0])
            code_values = query.get("code", [])
            if not code_values:
                raise ValueError("OAuth callback did not include an authorization code")
            state_values = query.get("state", [])
            issuer_values = query.get("iss", [])
            result = AuthorizationCodeResult(
                code=code_values[0],
                state=state_values[0] if state_values else None,
                iss=issuer_values[0] if issuer_values else None,
            )
            if state.callback_result is not None and not state.callback_result.done():
                state.callback_result.set_result(result)
            body = b"Authorization complete. You can return to the harness."
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except Exception as exc:
            if state.callback_result is not None and not state.callback_result.done():
                state.callback_result.set_exception(exc)
            body = b"OAuth callback failed. Return to the harness for details."
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    class MemoryTokenStorage(TokenStorage):
        def __init__(self) -> None:
            self.tokens: OAuthToken | None = None
            self.client_info = OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uris=[AnyUrl(redirect_uri)],
                token_endpoint_auth_method=token_auth_method,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )

        async def get_tokens(self) -> OAuthToken | None:
            return self.tokens

        async def set_tokens(self, tokens: OAuthToken) -> None:
            self.tokens = tokens

        async def get_client_info(self) -> OAuthClientInformationFull | None:
            return self.client_info

        async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
            self.client_info = client_info

    async def open_authorization_page(authorization_url: str) -> None:
        if not webbrowser.open(authorization_url):
            raise RuntimeError(
                "could not open a browser for MCP authorization; open this URL manually: "
                + authorization_url
            )

    async def wait_for_callback() -> AuthorizationCodeResult:
        return await callback_result

    if state.token_storage is None:
        state.token_storage = MemoryTokenStorage()
    if state.callback_server is None:
        state.callback_server = await asyncio.start_server(
            receive_callback, "127.0.0.1", parsed.port
        )
        state.callback_path = parsed.path
    elif state.callback_path != parsed.path:
        raise ValueError("OAuth redirect path changed during this harness session")

    yield OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Python Harness MCP client",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(str(scope) for scope in cast(list[object], auth.get("scopes", []))),
            token_endpoint_auth_method=token_auth_method,
        ),
        storage=state.token_storage,
        redirect_handler=open_authorization_page,
        callback_handler=wait_for_callback,
    )


async def _resolve_auth_headers(
    auth: dict[str, object] | None, state: MCPAuthState
) -> dict[str, str]:
    """Resolve header credentials without exposing prompted values to the model."""
    if auth is None:
        return {}
    if auth["type"] == "bearer":
        variable = str(auth["token_env"])
        return {"Authorization": f"Bearer {await state.secret(variable)}"}
    if auth["type"] == "headers":
        headers = cast(dict[str, str], auth["headers"])
        return {header: await state.secret(variable) for header, variable in headers.items()}
    return resolve_auth_headers(auth)
