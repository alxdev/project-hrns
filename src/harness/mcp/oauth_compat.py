from __future__ import annotations

from functools import wraps
from typing import Any, cast
from urllib.parse import urlsplit

_installed = False


def install_root_slash_issuer_compatibility() -> None:
    """Accept OAuth issuer URLs that differ only by an empty root-path slash.

    Google Workspace MCP protected-resource metadata currently advertises
    ``https://accounts.google.com/`` while Google's authorization metadata
    reports ``https://accounts.google.com``. The MCP SDK otherwise rejects
    that pair before authorization can proceed. All other issuer mismatches
    continue to use the SDK's strict validator.
    """
    global _installed
    if _installed:
        return
    try:
        from mcp.client.auth import oauth2
    except ImportError as exc:
        raise RuntimeError("MCP OAuth support requires the installed MCP SDK") from exc

    validator = oauth2.validate_metadata_issuer

    @wraps(validator)
    def validate(metadata: Any, expected_issuer: str) -> None:
        actual_issuer = str(metadata.issuer)
        if _differ_only_by_root_slash(actual_issuer, expected_issuer):
            return
        validator(metadata, expected_issuer)

    oauth2.validate_metadata_issuer = cast(Any, validate)
    _installed = True


def _differ_only_by_root_slash(actual: str, expected: str) -> bool:
    actual_parts = urlsplit(actual)
    expected_parts = urlsplit(expected)
    if (
        actual_parts.scheme.lower() != expected_parts.scheme.lower()
        or actual_parts.hostname is None
        or expected_parts.hostname is None
        or actual_parts.hostname.lower() != expected_parts.hostname.lower()
        or actual_parts.port != expected_parts.port
        or actual_parts.username is not None
        or actual_parts.password is not None
        or expected_parts.username is not None
        or expected_parts.password is not None
        or actual_parts.query
        or expected_parts.query
        or actual_parts.fragment
        or expected_parts.fragment
    ):
        return False
    return {actual_parts.path, expected_parts.path} == {"", "/"}
