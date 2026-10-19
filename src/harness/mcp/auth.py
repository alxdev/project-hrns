from __future__ import annotations

import asyncio
import os
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlparse

from .config import resolve_auth_headers


class MCPAuthState:
    """Process-local OAuth client and token state shared across MCP connections."""

    def __init__(self) -> None:
        self.token_storage: Any = None
        self.callback_server: asyncio.Server | None = None
        self.callback_path: str | None = None
        self.callback_result: asyncio.Future[Any] | None = None

    async def close(self) -> None:
        if self.callback_server is not None:
            self.callback_server.close()
            await self.callback_server.wait_closed()
            self.callback_server = None


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
        headers = resolve_auth_headers(auth)
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
    client_id_value = os.environ.get(client_id_name)
    client_secret_name = auth.get("client_secret_env")
    client_secret_value = (
        os.environ.get(str(client_secret_name)) if client_secret_name is not None else None
    )
    if not client_id_value:
        raise ValueError(f"required environment variable {client_id_name} is not set")
    if client_secret_name is not None and not client_secret_value:
        raise ValueError(f"required environment variable {client_secret_name} is not set")
    client_id = str(client_id_value)
    client_secret = str(client_secret_value) if client_secret_value is not None else None
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
