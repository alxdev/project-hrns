from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.agent import Agent
from harness.agent.loop import ToolCallEvent, ToolResultEvent
from harness.api.types import (
    Message,
    ProviderResponse,
    TextBlock,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from harness.memory import (
    MemoryConfig,
    MemoryManager,
    MemorySnapshot,
    MemoryStore,
    RecallExperience,
    RecordExperience,
)
from harness.orchestration import root_system_prompt
from harness.permissions import AlwaysAllow
from harness.provider import AnthropicProvider, LocalProvider, MockProvider, OpenAIProvider
from harness.provider.base import Provider
from harness.subagents import DelegationTool, SubagentManager, SubagentProfile
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry

from .scenarios import SCENARIOS, Fixture


class LimitReached(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    provider_calls: int = 96
    tool_calls: int = 128
    session_calls: int = 12
    session_seconds: float = 45
    total_seconds: float = 180
    output_tokens: int = 512

    def validate(self) -> None:
        for name, low, high in (
            ("provider_calls", 1, 128),
            ("tool_calls", 1, 256),
            ("session_calls", 1, 20),
            ("output_tokens", 64, 2048),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        for name, high in (("session_seconds", 60), ("total_seconds", 300)):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= high:
                raise ValueError(f"{name} must be finite, positive, and <= {high}")


@dataclass
class Budget:
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    provider_calls: int = 0
    tool_calls: int = 0
    exhausted: str | None = None

    def remaining(self) -> float:
        return self.limits.total_seconds - (time.monotonic() - self.started)

    def check(self) -> None:
        if self.remaining() <= 0:
            self.exhausted = "global_deadline"
        if self.exhausted:
            raise LimitReached(self.exhausted)


@dataclass
class Session:
    budget: Budget
    calls: int = 0
    tool_calls: int = 0
    context_bytes: int = 0
    stop_reason: str | None = None
    events: list[object] = field(default_factory=list)

    def check(self) -> None:
        self.budget.check()
        if self.stop_reason:
            raise LimitReached(self.stop_reason)

    async def emit(self, event: object) -> None:
        if isinstance(event, ToolCallEvent):
            self.check()
            if self.budget.tool_calls >= self.budget.limits.tool_calls:
                self.budget.exhausted = "global_tool_call_cap"
                raise LimitReached(self.budget.exhausted)
            self.tool_calls += 1
            self.budget.tool_calls += 1
        self.events.append(event)


class BoundedProvider:
    def __init__(self, provider: Provider, session: Session) -> None:
        self.inner, self.session = provider, session

    @property
    def model(self) -> str:
        return self.inner.model

    async def send(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> ProviderResponse:
        session = self.session
        session.check()
        if session.calls >= session.budget.limits.session_calls:
            session.stop_reason = "session_provider_call_cap"
            raise LimitReached(session.stop_reason)
        if session.budget.provider_calls >= session.budget.limits.provider_calls:
            session.budget.exhausted = "global_provider_call_cap"
            raise LimitReached(session.budget.exhausted)
        context = json.dumps(
            {
                "system": getattr(self.inner, "system", ""),
                "messages": [asdict(item) for item in messages],
                "tools": [asdict(item) for item in tools],
            },
            ensure_ascii=False,
        )
        size = len(context.encode())
        if size > 128000:
            session.stop_reason = "request_context_byte_cap"
            raise LimitReached(session.stop_reason)
        session.calls += 1
        session.budget.provider_calls += 1
        session.context_bytes += size
        response = await self.inner.send(messages, tools)
        if (
            len(json.dumps(asdict(response), ensure_ascii=False).encode()) > 8192
            or sum(isinstance(item, ToolCall) for item in response.content) > 8
        ):
            session.stop_reason = "response_size_cap"
            raise LimitReached(session.stop_reason)
        return response


def _last_result(messages: Sequence[Message]) -> dict[str, Any]:
    for message in reversed(messages):
        for block in reversed(message.content):
            if isinstance(block, ToolResult):
                return json.loads(block.content)
    return {}


def _response(name: str, arguments: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse([ToolCall("fixture-call", name, arguments)], "tool_use")


class ScriptedProvider(MockProvider):
    system: str = ""


def scripted_provider(role: str, fixture: Fixture, enabled: bool) -> ScriptedProvider:
    """Deliberate plumbing script, never evidence of model learning."""
    step = 0
    remembered: str | None = None
    outcome: dict[str, Any] = {}
    actions: list[tuple[str, dict[str, Any]]] | None = None

    async def respond(
        messages: Sequence[Message], _tools: Sequence[ToolDefinition]
    ) -> ProviderResponse:
        nonlocal step, remembered, outcome, actions
        if role == "worker":
            if actions is None:
                handoff = messages[0].content[0]
                assert isinstance(handoff, TextBlock)
                assigned = json.loads(handoff.text.split("Focused assigned task:\n", 1)[1])
                strategy = assigned.get("remembered_strategy")
                args = {
                    "task_id": assigned["task_id"],
                    "values": assigned["values"],
                    "strategy": strategy or fixture.scenario.initial_strategy,
                }
                actions = [("perform_task", args)]
                if strategy is None:
                    actions += [
                        ("inspect_rules", {}),
                        ("perform_task", {**args, "strategy": fixture.scenario.strategy}),
                    ]
                actions.append(("verify_result", {"task_id": assigned["task_id"]}))
            if step < len(actions):
                name, arguments = actions[step]
                step += 1
                return _response(name, arguments)
            return ProviderResponse(
                [
                    TextBlock(
                        json.dumps(
                            {
                                "status": "completed",
                                "result": fixture.result,
                                "reason": "Persisted state checked",
                            }
                        )
                    )
                ],
                "end_turn",
            )
        if step == 0 and enabled and fixture.phase == "transfer":
            step = 1
            return _response("recall_experience", {"tags": [fixture.scenario.name]})
        if step <= 1:
            if step == 1:
                recalled = _last_result(messages)
                if recalled.get("records"):
                    remembered = json.loads(recalled["records"][0]["record"]["lesson"]["text"])[
                        "strategy"
                    ]
            task = json.loads(fixture.prompt())
            if remembered:
                task["remembered_strategy"] = remembered
            step = 2
            return _response("delegate_worker", {"task": json.dumps(task)})
        if step == 2:
            outcome = _last_result(messages)
            step = 3
            if enabled and fixture.phase == "training" and outcome.get("run_id"):
                return _response(
                    "record_experience",
                    {
                        "run_ids": [outcome["run_id"]],
                        "assessment": {"summary": fixture.scenario.rule},
                        "tags": [fixture.scenario.name],
                        "applicability": "Other tasks in this fixture workspace",
                        "lesson": json.dumps({"strategy": fixture.scenario.strategy}),
                    },
                )
        return ProviderResponse([TextBlock(outcome.get("child_response", ""))], "end_turn")

    return ScriptedProvider(responder=respond)


@dataclass(frozen=True)
class ProviderSettings:
    provider: str = "smoke"
    model: str = "scripted"
    config_dir: Path = field(default_factory=Path.cwd)

    def build(
        self, role: str, system: str, fixture: Fixture, enabled: bool, limits: Limits
    ) -> Provider:
        if self.provider == "smoke":
            result = scripted_provider(role, fixture, enabled)
            result.system = system
            return result
        if self.provider == "openai":
            return OpenAIProvider(model=self.model, system=system, max_tokens=limits.output_tokens)
        if self.provider == "anthropic":
            return AnthropicProvider(
                model=self.model, system=system, max_tokens=limits.output_tokens
            )
        if self.provider == "local":
            return LocalProvider(
                model=self.model,
                config_path=self.config_dir / "local_models.json",
                system=system,
                max_tokens=limits.output_tokens,
            )
        raise ValueError("unsupported evaluation provider")


def _memory_metrics(session: Session, training_ids: set[str], fixture: Fixture) -> dict[str, Any]:
    saves = recalls = hits = 0
    retrieved_training: set[str] = set()
    tagged_training: set[str] = set()
    recalled_before_action = False
    action_seen = False
    selections = []
    for event in session.events:
        if isinstance(event, ToolCallEvent) and event.name == "perform_task":
            action_seen = True
        if (
            not isinstance(event, ToolResultEvent)
            or event.result.is_error
            or event.name not in {"record_experience", "recall_experience"}
        ):
            continue
        try:
            payload = json.loads(event.result.content)
        except ValueError:
            continue
        if event.name == "record_experience":
            saves += payload.get("status") == "recorded"
        elif payload.get("status") == "recalled":
            recalls += 1
            hits += bool(payload.get("records"))
            for item in payload.get("records", []):
                record = item.get("record", {})
                selections.append(
                    {
                        "id": record.get("id"),
                        "from_training": record.get("id") in training_ids,
                        "retrieval_reason": item.get("retrieval_reason", {}),
                        "before_first_action": not action_seen,
                    }
                )
                if record.get("id") in training_ids:
                    retrieved_training.add(record["id"])
                    recalled_before_action |= not action_seen
                    if fixture.scenario.name in record.get("tags", []):
                        tagged_training.add(record["id"])
    return {
        "save_calls_successful": saves,
        "recall_calls_successful": recalls,
        "recall_calls_with_hits": hits,
        "training_records_recalled": len(retrieved_training),
        "training_record_ids_recalled": sorted(retrieved_training),
        "training_records_matching_scenario_tag": len(tagged_training),
        "training_recall_before_first_action": recalled_before_action,
        "recall_selections": selections[:16],
        "recall_selections_omitted": max(0, len(selections) - 16),
        "application_proxy": bool(
            recalled_before_action
            and fixture.attempts
            and fixture.attempts[0]["valid"]
            and fixture.verified
        ),
        "lesson_quality": "manual_review_required",
    }


async def run_session(
    fixture: Fixture,
    workspace: Path,
    data: Path,
    enabled: bool,
    settings: ProviderSettings,
    budget: Budget,
    training_ids: set[str],
) -> dict[str, Any]:
    session = Session(budget)
    started = time.monotonic()
    answer, status, error_type = "", "completed", None
    subagents: SubagentManager | None = None
    store = MemoryStore(data, workspace)
    try:
        budget.check()
        tools = fixture.tools()
        profile = SubagentProfile(
            "worker",
            "Complete one local fixture task using inspect_rules, perform_task, verify_result. "
            "Preserve exact scope and inputs; return observed result or blocker.",
            "Use only the supplied safe fixture tools. Do not invent task inputs. "
            "Inspect conventions if needed, apply exact task, verify persisted state, "
            "and report result or blocker.",
            tools=tools,
            max_turns=8,
        )

        def child(effective: SubagentProfile) -> Agent:
            provider = settings.build(
                "worker", effective.system_prompt, fixture, enabled, budget.limits
            )
            return Agent(
                BoundedProvider(provider, session),
                ToolRegistry(effective.tools),
                AlwaysAllow(),
                ToolContext(workspace),
                events=session,
                name="worker",
                max_turns=8,
                quiet=True,
                streaming=False,
            )

        subagents = SubagentManager(
            child, {"worker": profile}, events=session, working_directory=workspace
        )
        delegation = DelegationTool("worker", profile.description)
        delegation.bind(subagents)
        registry = ToolRegistry([delegation])
        memory = MemoryManager(
            MemoryConfig(
                enabled=enabled, max_records=12, max_recall_records=2, recall_token_budget=1500
            ),
            store,
            subagents,
        )
        hook = None
        if enabled:
            recall = RecallExperience(memory)
            registry.register(recall)
            registry.register(RecordExperience(memory))
            hook = MemorySnapshot(recall, registry)
        system = root_system_prompt(
            "Complete the user's local fixture task using the worker. "
            "Return only the requested final JSON object. "
            "Acknowledgements and child claims alone are not verification."
        )
        provider = settings.build("root", system, fixture, enabled, budget.limits)
        root = Agent(
            BoundedProvider(provider, session),
            registry,
            AlwaysAllow(),
            ToolContext(workspace),
            events=session,
            name="root",
            max_turns=8,
            streaming=False,
            quiet=True,
            after_tool_batch=hook,
        )
        awaitable = root.send(fixture.prompt())
        answer = await asyncio.wait_for(
            awaitable, min(budget.limits.session_seconds, max(0.001, budget.remaining()))
        )
    except TimeoutError:
        status = "timeout"
        if budget.remaining() <= 0:
            budget.exhausted = "global_deadline"
    except asyncio.CancelledError:
        status, error_type = "cancelled", "CancelledError"
        budget.exhausted = "cancelled"
    except Exception as exc:
        status, error_type = "error", type(exc).__name__
    if budget.exhausted or session.stop_reason:
        status = "budget_exhausted" if budget.exhausted != "cancelled" else "cancelled"
    metrics = fixture.metrics(answer)
    if status != "completed":
        metrics["task_success"] = False
    metrics.update(_memory_metrics(session, training_ids, fixture))
    try:
        records = store.load() if enabled else []
    except (ValueError, OSError) as exc:
        records = []
        status, error_type = "error", type(exc).__name__
        metrics["task_success"] = False
    return {
        "scenario": fixture.scenario.name,
        "repeat": fixture.repeat,
        "arm": "enabled" if enabled else "disabled",
        "phase": fixture.phase,
        "status": status,
        "stop_reason": budget.exhausted or session.stop_reason,
        "error_type": error_type,
        "answer": answer[:8000],
        "metrics": metrics,
        "delegations": len(subagents.records()) if subagents else 0,
        "failed_delegations": sum(
            record.status.value != "completed" for record in subagents.records()
        )
        if subagents
        else 0,
        "delegation_error_types": sorted(
            {error.type for record in subagents.records() for error in record.errors}
        )[:16]
        if subagents
        else [],
        "extra_delegations": max(0, len(subagents.records()) - 1) if subagents else 0,
        "provider_calls": session.calls,
        "admitted_tool_calls": session.tool_calls,
        "serialized_context_bytes": session.context_bytes,
        "estimated_context_tokens": math.ceil(session.context_bytes / 4),
        "exact_input_tokens": None,
        "exact_output_tokens": None,
        "cost": None,
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "persisted_ids": [record["id"] for record in records],
        "memory_records_for_review": [
            {key: record[key] for key in ("id", "tags", "assessment", "applicability", "lesson")}
            for record in records
        ],
        "delegation_tasks": [record.prompt[:2000] for record in subagents.records()]
        if subagents
        else [],
        "fixture_actions": fixture.attempts,
    }


def aggregate(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    conditions = {}
    for arm in ("disabled", "enabled"):
        for phase in ("training", "transfer"):
            selected = [row for row in rows if row["arm"] == arm and row["phase"] == phase]
            observed = [row for row in selected if row["metrics"] is not None]
            successes = sum(bool(row["metrics"]["task_success"]) for row in observed)
            conditions[f"{arm}/{phase}"] = {
                "scheduled": len(selected),
                "statuses": dict(Counter(row["status"] for row in selected)),
                "observed": len(observed),
                "completed": sum(row["status"] == "completed" for row in selected),
                "task_successes": successes,
                "success_rate_scheduled": successes / len(selected) if selected else None,
                "successful_saves": sum(
                    row["metrics"]["save_calls_successful"] for row in observed
                ),
                "successful_recalls": sum(
                    row["metrics"]["recall_calls_successful"] for row in observed
                ),
                "no_save_sessions": sum(
                    row["metrics"]["save_calls_successful"] == 0 for row in observed
                ),
                "no_save_fraction_observed": sum(
                    row["metrics"]["save_calls_successful"] == 0 for row in observed
                )
                / len(observed)
                if observed
                else None,
            }
    pairs = []
    for scenario, repeat in sorted({(row["scenario"], row["repeat"]) for row in rows}):
        paired = {
            row["arm"]: row
            for row in rows
            if row["scenario"] == scenario
            and row["repeat"] == repeat
            and row["phase"] == "transfer"
        }
        complete = all(
            paired.get(arm, {}).get("status") == "completed" for arm in ("disabled", "enabled")
        )
        pairs.append(
            {
                "scenario": scenario,
                "repeat": repeat,
                "comparable_completed_pair": complete,
                "task_success_delta": int(paired["enabled"]["metrics"]["task_success"])
                - int(paired["disabled"]["metrics"]["task_success"])
                if complete
                else None,
                "failed_action_delta": paired["enabled"]["metrics"]["failed_actions"]
                - paired["disabled"]["metrics"]["failed_actions"]
                if complete
                else None,
            }
        )
    return {
        "mode": mode,
        "interpretation": "Scripted plumbing only; no learning or model-effect claim."
        if mode == "smoke"
        else "Small behavioral sample; application proxies are not causal proof. "
        "Inspect lessons and paired traces manually.",
        "conditions": conditions,
        "paired_transfer": pairs,
        "exact_tokens": None,
        "cost": None,
        "seed_control": "uncontrolled: provider interfaces expose no seed",
    }


async def run_suite(
    settings: ProviderSettings, scenario_names: list[str], repeats: int, limits: Limits
) -> dict[str, Any]:
    limits.validate()
    if (
        not 1 <= repeats <= 3
        or not scenario_names
        or len(set(scenario_names)) != len(scenario_names)
        or any(name not in SCENARIOS for name in scenario_names)
    ):
        raise ValueError("select 1-3 unique known scenarios and 1-3 repeats")
    budget = Budget(limits)
    rows: list[dict[str, Any]] = []
    schedule = []
    with tempfile.TemporaryDirectory(prefix="harness-memory-eval-") as directory:
        root = Path(directory)
        for repeat in range(repeats):
            arms = (False, True) if repeat % 2 == 0 else (True, False)
            for name in scenario_names:
                for enabled in arms:
                    workspace = (
                        root
                        / name
                        / str(repeat)
                        / ("enabled" if enabled else "disabled")
                        / "workspace"
                    )
                    workspace.mkdir(parents=True)
                    data = workspace.parent / "data"
                    training_ids: set[str] = set()
                    for phase in ("training", "transfer"):
                        schedule.append(
                            {
                                "scenario": name,
                                "repeat": repeat,
                                "arm": "enabled" if enabled else "disabled",
                                "phase": phase,
                            }
                        )
                        if budget.exhausted or budget.remaining() <= 0:
                            budget.exhausted = budget.exhausted or "global_deadline"
                            rows.append(
                                {
                                    **schedule[-1],
                                    "status": "not_run_budget",
                                    "stop_reason": budget.exhausted,
                                    "metrics": None,
                                }
                            )
                            continue
                        fixture = Fixture(SCENARIOS[name], phase, repeat)
                        row = await run_session(
                            fixture, workspace, data, enabled, settings, budget, training_ids
                        )
                        rows.append(row)
                        if phase == "training":
                            training_ids = set(row["persisted_ids"])
    mode = "smoke" if settings.provider == "smoke" else "real_model"
    return {
        "schema_version": 1,
        "provider": settings.provider,
        "model": settings.model,
        "repeats": repeats,
        "scenario_names": scenario_names,
        "order": "disabled first on even repeats; enabled first on odd repeats",
        "limits": asdict(limits),
        "schedule": schedule,
        "runs": rows,
        "aggregate": aggregate(rows, mode),
        "total_provider_calls": budget.provider_calls,
        "total_admitted_tool_calls": budget.tool_calls,
        "terminal_limit": budget.exhausted,
    }


def write_reports(report: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (output / "runs.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in report["runs"])
    )
    summary = report["aggregate"]
    lines = [
        "# Experience memory evaluation",
        "",
        summary["interpretation"],
        "",
        "| Condition | Scheduled | Observed | Task successes | Saves | Recalls |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, counts in summary["conditions"].items():
        lines.append(
            f"| {name} | {counts['scheduled']} | {counts['observed']} | "
            f"{counts['task_successes']} | {counts['successful_saves']} | "
            f"{counts['successful_recalls']} |"
        )
    lines += [
        "",
        "Exact token usage and cost: unknown (null in JSON). Context-token estimates use "
        "serialized UTF-8 bytes / 4, not billing tokens.",
        "",
        "Application requires same-scenario training-record recall before the first action, "
        "plus a correct first action and verification. This proxy is not proof of learning. "
        "Review saved assessments/lessons in memory_records_for_review.",
        "",
        f"Provider calls: {report['total_provider_calls']}; "
        f"admitted tool calls: {report['total_admitted_tool_calls']}; "
        f"terminal limit: {report['terminal_limit'] or 'none'}.",
        "",
        "See report.json for paired deltas, failures, omitted sessions, action traces, "
        "and delegation text.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
