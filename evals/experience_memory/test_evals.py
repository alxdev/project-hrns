import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.experience_memory.__main__ import main
from evals.experience_memory.runner import (
    Budget,
    Limits,
    ProviderSettings,
    Session,
    _memory_metrics,
    run_suite,
    write_reports,
)
from evals.experience_memory.scenarios import SCENARIOS, Fixture
from harness.agent.loop import ToolCallEvent, ToolResultEvent
from harness.api.types import ProviderResponse, TextBlock, ToolResult
from harness.provider import MockProvider
from harness.tools.base import ToolContext


async def test_smoke_uses_real_persistence_and_fresh_transfer_without_home_access(monkeypatch):
    def no_home():
        pytest.fail("evaluation must not access production home storage")

    monkeypatch.setattr(Path, "home", no_home)
    original = ProviderSettings.build
    starts = []

    def tracked(self, role, system, fixture, enabled, limits):
        provider = original(self, role, system, fixture, enabled, limits)
        responder = provider.responder
        first = True

        async def respond(messages, definitions):
            nonlocal first
            if first:
                starts.append((role, fixture.phase, enabled, len(messages), str(messages)))
                first = False
                assert fixture.result is None
            return await responder(messages, definitions)

        provider.responder = respond
        return provider

    monkeypatch.setattr(ProviderSettings, "build", tracked)
    report = await run_suite(ProviderSettings(), list(SCENARIOS), 1, Limits())
    assert len(report["runs"]) == 12
    assert all(
        row["status"] == "completed" and row["metrics"]["task_success"] for row in report["runs"]
    )
    assert report["aggregate"]["mode"] == "smoke"
    assert "no learning" in report["aggregate"]["interpretation"]
    assert report["total_provider_calls"] == 84 and report["total_admitted_tool_calls"] == 60
    transfer = [row for row in report["runs"] if row["phase"] == "transfer"]
    for row in transfer:
        assert row["metrics"]["application_proxy"] == (row["arm"] == "enabled")
        assert row["metrics"]["training_records_recalled"] == int(row["arm"] == "enabled")
        assert bool(row["memory_records_for_review"]) == (row["arm"] == "enabled")
        assert row["exact_input_tokens"] is None and row["cost"] is None
        assert row["fixture_actions"][0]["arguments"]["values"] == [7, 4]
    assert all(count == 1 for _role, _phase, _arm, count, _text in starts)
    assert all(
        "-training" not in text
        for role, phase, _arm, _count, text in starts
        if role == "root" and phase == "transfer"
    )


@pytest.mark.parametrize(
    "limits,expected_calls,expected_tools",
    [(Limits(provider_calls=1), 1, 1), (Limits(tool_calls=1), 2, 1)],
)
async def test_global_budget_includes_children_and_marks_all_remaining_sessions(
    limits, expected_calls, expected_tools
):
    report = await run_suite(ProviderSettings(), ["exact_handoff"], 1, limits)
    assert report["total_provider_calls"] == expected_calls
    assert report["total_admitted_tool_calls"] == expected_tools
    assert report["runs"][0]["status"] == "budget_exhausted"
    assert report["runs"][0]["fixture_actions"] == []
    assert all(row["status"] == "not_run_budget" for row in report["runs"][1:])
    assert report["aggregate"]["paired_transfer"][0]["task_success_delta"] is None
    assert sum(item["scheduled"] for item in report["aggregate"]["conditions"].values()) == 4


async def test_session_cap_does_not_reset_global_counts_or_omit_cells():
    report = await run_suite(ProviderSettings(), ["exact_handoff"], 1, Limits(session_calls=1))
    assert report["total_provider_calls"] == 4
    assert all(row["status"] == "budget_exhausted" for row in report["runs"])
    assert report["terminal_limit"] is None


class NoSaveSettings:
    provider = "fake-real"
    model = "fake-real"

    def build(self, *_args):
        return MockProvider(
            [ProviderResponse([TextBlock('{"status":"completed","result":5}')], "end_turn")]
        )


async def test_natural_no_save_and_unsupported_claim_are_reported_without_seeded_memory():
    report = await run_suite(NoSaveSettings(), ["unverified_completion"], 1, Limits())
    assert report["aggregate"]["mode"] == "real_model"
    for row in report["runs"]:
        assert row["metrics"]["save_calls_successful"] == 0
        assert row["metrics"]["recall_calls_successful"] == 0
        assert row["metrics"]["unsupported_completion_claim"]
        assert not row["metrics"]["task_success"]
        assert row["persisted_ids"] == []


async def test_timeout_cancels_inflight_provider_and_reports_remaining_cells():
    stopped = asyncio.Event()

    class Hanging(NoSaveSettings):
        def build(self, *_args):
            async def respond(_messages, _tools):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            return MockProvider(responder=respond)

    report = await run_suite(Hanging(), ["missing_source"], 1, Limits(total_seconds=0.02))
    assert stopped.is_set()
    assert report["terminal_limit"] == "global_deadline"
    assert report["total_provider_calls"] == 1
    assert all(row["status"] == "not_run_budget" for row in report["runs"][1:])


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_oracles_reject_claims_wrong_scope_and_unverified_state(tmp_path, name):
    fixture = Fixture(SCENARIOS[name], "transfer", 0)
    tools = {tool.definition.name: tool for tool in fixture.tools()}
    context = ToolContext(tmp_path)
    claim = json.dumps({"status": "completed", "result": fixture.expected})
    assert not fixture.metrics(claim)["task_success"]
    wrong = await tools["perform_task"].execute(
        context,
        {"task_id": "wrong", "values": fixture.values, "strategy": fixture.scenario.strategy},
    )
    assert wrong.is_error and fixture.result is None
    await tools["verify_result"].execute(context, {"task_id": fixture.task_id})
    await tools["perform_task"].execute(
        context,
        {
            "task_id": fixture.task_id,
            "values": fixture.values,
            "strategy": fixture.scenario.strategy,
        },
    )
    assert not fixture.metrics(claim)["task_success"]  # verification before action is insufficient
    await tools["verify_result"].execute(context, {"task_id": "wrong"})
    assert not fixture.metrics(claim)["task_success"]
    await tools["verify_result"].execute(context, {"task_id": fixture.task_id})
    assert fixture.metrics(claim)["task_success"]


def test_application_proxy_requires_training_recall_before_action_not_a_magic_tag():
    fixture = Fixture(
        SCENARIOS["missing_source"],
        "transfer",
        0,
        result=11,
        verified=True,
        attempts=[{"valid": True}],
    )
    recalled = ToolResultEvent(
        "recall_experience",
        ToolResult(
            "id",
            json.dumps(
                {
                    "status": "recalled",
                    "records": [
                        {
                            "record": {"id": "training", "tags": ["fallback"]},
                            "retrieval_reason": {"keywords": ["fallback"]},
                        }
                    ],
                }
            ),
        ),
    )
    action = ToolCallEvent("perform_task", {})
    session = Session(Budget(Limits()), events=[recalled, action])
    metrics = _memory_metrics(session, {"training"}, fixture)
    assert metrics["application_proxy"] and metrics["training_records_matching_scenario_tag"] == 0
    assert metrics["recall_selections"][0]["retrieval_reason"] == {"keywords": ["fallback"]}
    session.events = [action, recalled]
    assert not _memory_metrics(session, {"training"}, fixture)["application_proxy"]
    session.events = [recalled, action]
    assert not _memory_metrics(session, {"different-record"}, fixture)["application_proxy"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_calls", 0),
        ("tool_calls", 257),
        ("session_seconds", float("nan")),
        ("total_seconds", float("inf")),
        ("output_tokens", 4096),
    ],
)
def test_invalid_budgets_fail_before_launch(field, value):
    with pytest.raises(ValueError):
        replace(Limits(), **{field: value}).validate()


def test_cli_real_mode_is_dry_plan_unless_execute_and_reports_never_overwrite(tmp_path, capsys):
    output = tmp_path / "reports"
    assert (
        main(
            [
                "run",
                "--provider",
                "openai",
                "--model",
                "example",
                "--scenario",
                "missing_source",
                "--output",
                str(output),
                "--config-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert not output.exists()
    output.mkdir()
    with pytest.raises(FileExistsError):
        write_reports({}, output)


@pytest.mark.parametrize("command", [[], ["list"], ["--help"]])
def test_cli_help_and_list_do_not_resolve_home(command, monkeypatch, capsys):
    def no_home():
        pytest.fail("help and list must not resolve home storage")

    monkeypatch.setattr(Path, "home", no_home)
    if command == ["--help"]:
        with pytest.raises(SystemExit) as exc:
            main(command)
        assert exc.value.code == 0
    else:
        assert main(command) == 0
    assert capsys.readouterr().out


@pytest.mark.parametrize("name_source", ["default", "environment", "dotenv", "argument"])
def test_cli_default_report_path_uses_runtime_name_without_dry_plan_writes(
    tmp_path, monkeypatch, capsys, name_source
):
    import harness.config

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(harness.config, "_default_harness_name", lambda: "default-harness")
    monkeypatch.delenv("HARNESS_NAME", raising=False)
    expected_name = "default-harness"
    extra = []
    if name_source == "dotenv":
        (tmp_path / ".env").write_text("HARNESS_NAME=dotenv-harness\n")
        expected_name = "dotenv-harness"
    elif name_source in {"environment", "argument"}:
        monkeypatch.setenv("HARNESS_NAME", "environment-harness")
        (tmp_path / ".env").write_text("HARNESS_NAME=ignored-harness\n")
        expected_name = "environment-harness"
        if name_source == "argument":
            extra = ["--harness-name", "explicit-harness"]
            expected_name = "explicit-harness"
    argv = [
        "run",
        "--provider",
        "openai",
        "--model",
        "unused",
        "--scenario",
        "missing_source",
        "--config-dir",
        str(tmp_path),
        *extra,
    ]
    outputs = []
    for _ in range(2):
        assert main(argv) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["dry_run"]
        output = Path(plan["output"])
        assert output.parent == home / f".{expected_name}" / "evals" / "experience-memory"
        outputs.append(output)
    assert outputs[0] != outputs[1]
    assert not home.exists()


def test_cli_smoke_default_creates_unique_reports_and_override_never_overwrites(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_NAME", "test-harness")
    outputs = []
    for _ in range(2):
        assert main(["smoke", "--scenario", "missing_source"]) == 0
        result = json.loads(capsys.readouterr().out)
        report = Path(result["report"])
        assert report.is_file() and Path(result["summary"]).is_file()
        assert report.parent.parent == home / ".test-harness" / "evals" / "experience-memory"
        outputs.append(report.parent)
    assert outputs[0] != outputs[1]
    override = tmp_path / "explicit-report"
    argv = ["smoke", "--scenario", "missing_source", "--output", str(override)]
    assert main(argv) == 0
    assert Path(json.loads(capsys.readouterr().out)["report"]) == override / "report.json"
    before = (override / "report.json").read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert (override / "report.json").read_bytes() == before
    assert set(outputs[0].parent.iterdir()) == set(outputs)


async def test_report_keeps_bounded_lessons_for_review_and_counterbalanced_schedule(tmp_path):
    report = await run_suite(ProviderSettings(), ["missing_source"], 2, Limits())
    assert report["schedule"][0]["arm"] == "disabled"
    assert report["schedule"][4]["arm"] == "enabled"
    write_reports(report, tmp_path / "report")
    loaded = json.loads((tmp_path / "report" / "report.json").read_text())
    lessons = [
        record["lesson"] for row in loaded["runs"] for record in row["memory_records_for_review"]
    ]
    assert lessons and all("snapshot" in lesson["text"] for lesson in lessons)
    assert len((tmp_path / "report" / "runs.jsonl").read_text().splitlines()) == 8
    assert "no learning" in (tmp_path / "report" / "report.md").read_text()
