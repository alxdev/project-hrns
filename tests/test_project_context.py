from harness.project_context import load_project_context


def test_load_project_context_reads_only_the_configured_working_directory(tmp_path):
    (tmp_path / "AGENTS.md").write_text("shared rules", encoding="utf-8")

    context = load_project_context(tmp_path)

    assert context.root_content == "shared rules"
    assert context.root_path == tmp_path / "AGENTS.md"
    assert context.system_prompt("base prompt") == (
        "base prompt\n\n# Project context (from AGENTS.md)\n\nshared rules"
    )


def test_missing_project_context_is_safe(tmp_path):
    context = load_project_context(tmp_path)

    assert context.root_content == ""
    assert context.root_path is None
    assert context.system_prompt("base prompt") == "base prompt"
