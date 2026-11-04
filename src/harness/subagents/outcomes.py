"""Versioned delegation envelopes built only from harness observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .capabilities import CapabilitySummary
from .models import CapabilityEvidence, ResourceUsage, SubagentError, SubagentRecord

EVIDENCE_LIMIT = 64
ERROR_LIMIT = 20


def capability_evidence(summary: CapabilitySummary, stage: str) -> CapabilityEvidence:
    resources = sorted(
        (asdict(resource) for resource in summary.resources),
        key=lambda resource: tuple(resource.values()),
    )
    canonical = json.dumps(
        {
            "version": 1,
            "role": summary.role,
            "permission_mode": summary.permission_mode,
            "resources": resources,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return CapabilityEvidence(
        fingerprint="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        stage=stage,
        resources=tuple(
            {key: value for key, value in resource.items() if key != "description"}
            for resource in resources[:EVIDENCE_LIMIT]
        ),
        resources_omitted=max(0, len(resources) - EVIDENCE_LIMIT),
    )


def add_error(record: SubagentRecord, error: SubagentError) -> None:
    """Aggregate repeated failures without retaining arguments or tool outputs."""
    for existing in record.errors:
        if (existing.stage, existing.type, existing.message, existing.tool) == (
            error.stage,
            error.type,
            error.message,
            error.tool,
        ):
            existing.count += error.count
            return
    record.errors.append(error)


def runtime_error(record: SubagentRecord, stage: str, exc: BaseException) -> None:
    add_error(record, SubagentError(stage, type(exc).__name__, str(exc)[:500]))


def _usage(usage: ResourceUsage) -> dict[str, Any]:
    tools = sorted(usage.tools.items())
    skills = sorted(usage.skills_activated.items())
    added = sorted(usage.tools_added)
    return {
        "observation_status": usage.observation_status,
        "tools": [{"name": name, **asdict(counts)} for name, counts in tools[:EVIDENCE_LIMIT]],
        "tools_omitted": max(0, len(tools) - EVIDENCE_LIMIT),
        "skills_activated": [
            {"name": name, "count": count} for name, count in skills[:EVIDENCE_LIMIT]
        ],
        "skills_omitted": max(0, len(skills) - EVIDENCE_LIMIT),
        "tools_added": [
            {"name": name, "persistence": "not_assessed"} for name in added[:EVIDENCE_LIMIT]
        ],
        "tools_added_omitted": max(0, len(added) - EVIDENCE_LIMIT),
    }


def serialize_outcome(record: SubagentRecord) -> str:
    # Keep run-level failures visible even after many distinct tool failures.
    errors = sorted(record.errors, key=lambda error: error.stage in {"tool_result", "permission"})
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": record.id,
            "profile": record.profile,
            "objective": record.prompt,
            "lifecycle_status": record.status.value,
            "child_response": record.result,
            "verification_status": "not_assessed",
            "resource_usage": _usage(record.resource_usage),
            "errors": [asdict(error) for error in errors[:ERROR_LIMIT]],
            "errors_omitted": max(0, len(record.errors) - ERROR_LIMIT),
            "capabilities": asdict(record.capabilities) if record.capabilities else None,
        },
        ensure_ascii=False,
    )


def rejected_outcome(profile: str, objective: str, exc: Exception) -> str:
    """A routing/preflight rejection has no child run or runtime observations."""
    record = SubagentRecord("", profile, objective)
    runtime_error(record, "preflight", exc)
    envelope = json.loads(serialize_outcome(record))
    envelope.update(run_id=None, lifecycle_status="failed")
    return json.dumps(envelope, ensure_ascii=False)
