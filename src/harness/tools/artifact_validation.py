"""Definition checks shared by artifact authoring and manifest loading."""

from __future__ import annotations

import re
import warnings
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

_ARTIFACT_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def portable_component(part: str) -> bool:
    return (
        bool(part)
        and part not in (".", "..")
        and not part.endswith((".", " "))
        and not any(c in '<>:"/\\|?*' or ord(c) < 32 for c in part)
        and part.split(".")[0].casefold() not in _RESERVED
    )


def is_valid_artifact_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and _ARTIFACT_NAME.fullmatch(name) is not None
        and portable_component(name)
    )


def validate_artifact_definition(description: Any, input_schema: Any, command: Any) -> None:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string")
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise ValueError('input_schema must explicitly declare type: "object"')
    if "$schema" in input_schema and not isinstance(input_schema["$schema"], str):
        raise ValueError("input_schema $schema must name a supported JSON Schema dialect")
    # jsonschema warns and falls back for unknown dialects. Reject that fallback
    # without exposing a warning or fetching the submitted dialect URI.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            validator = validator_for(input_schema)
    except DeprecationWarning as exc:
        raise ValueError("input_schema $schema names an unsupported JSON Schema dialect") from exc
    try:
        validator.check_schema(input_schema)
    except SchemaError as exc:
        # SchemaError.__str__ includes schema/source dumps. Report only its location.
        location = "/".join(str(part)[:80] for part in exc.path)[:240] or "<root>"
        raise ValueError(
            f"invalid input_schema at {location}; check JSON Schema keyword structure "
            "(including properties, types, and required arrays)"
        ) from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and "\0" not in part for part in command)
        or not command[0].strip()
    ):
        raise ValueError("command must be an argv array with a non-blank executable and no NULs")
