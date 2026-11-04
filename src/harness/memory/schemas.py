"""Only root assessments are caller-supplied; runtime evidence has no input field."""

from typing import Any

TAGS: dict[str, Any] = {
    "type": "array",
    "maxItems": 12,
    "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 40},
}
RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "assessment": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
                "verification_status": {
                    "type": "string",
                    "enum": ["not_assessed", "verified", "failed", "partial"],
                },
                "rationale": {"type": "string", "maxLength": 1200},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        "tags": TAGS,
        "applicability": {"type": "string", "minLength": 1, "maxLength": 600},
        "lesson": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    "required": ["run_ids", "assessment", "applicability"],
    "additionalProperties": False,
}
RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "maxLength": 500},
        "tags": TAGS,
        "kind": {"type": "string", "enum": ["all", "experience", "lesson"]},
    },
    "additionalProperties": False,
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_NAME = {"type": "string", "minLength": 1, "maxLength": 256}
_COUNT = {"type": "integer", "minimum": 0}
_UUID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
_DATE = {"type": "string", "format": "date-time"}
_TOOL_USAGE = _object(
    {
        "name": _NAME,
        **{
            name: _COUNT
            for name in (
                "requested",
                "execution_started",
                "returned_success",
                "returned_error",
                "permission_denied",
            )
        },
    }
)
_EVIDENCE = _object(
    {
        "source": {"const": "harness_runtime"},
        "session_id": _UUID,
        "run_id": _NAME,
        "profile": _NAME,
        "lifecycle_status": {"enum": ["completed", "failed", "cancelled"]},
        "completed_at": _DATE,
        "verification_status": {"const": "not_assessed"},
        "capability_fingerprint": {"type": ["string", "null"], "pattern": "^sha256:[a-f0-9]{64}$"},
        "observation_status": {"enum": ["not_started", "observed", "partial", "unavailable"]},
        "tools": {"type": "array", "maxItems": 16, "items": _TOOL_USAGE},
        "tools_omitted": _COUNT,
        "skills_activated": {
            "type": "array",
            "maxItems": 16,
            "items": _object({"name": _NAME, "count": _COUNT}),
        },
        "skills_omitted": _COUNT,
        "tools_added": {"type": "array", "maxItems": 16, "items": _NAME},
        "tools_added_omitted": _COUNT,
        "error_types": {
            "type": "array",
            "maxItems": 16,
            "items": _object(
                {
                    "stage": _NAME,
                    "type": _NAME,
                    "tool": {"type": ["string", "null"], "maxLength": 256},
                }
            ),
        },
        "error_types_omitted": _COUNT,
    }
)
EXPERIENCE_SCHEMA = _object(
    {
        "id": _UUID,
        "workspace_key": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "record_type": {"const": "experience"},
        "created_at": _DATE,
        "runtime_evidence": {"type": "array", "minItems": 1, "maxItems": 4, "items": _EVIDENCE},
        "assessment": _object(
            {
                "source": {"const": "root_assessment"},
                "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
                "verification_status": {"enum": ["not_assessed", "verified", "failed", "partial"]},
                "rationale": {"type": "string", "maxLength": 1200},
            }
        ),
        "tags": TAGS,
        "applicability": {"type": "string", "minLength": 1, "maxLength": 600},
        "lesson": {
            "anyOf": [
                {"type": "null"},
                _object(
                    {
                        "source": {"const": "root_assessment"},
                        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                    }
                ),
            ]
        },
    }
)
