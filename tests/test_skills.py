import json
from pathlib import Path

import pytest

from harness.skills import SkillError, load_skills


def write_skill(root: Path, name: str, description: str, body: str) -> Path:
    skill_root = root / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_root


def test_loads_standard_project_and_user_skills_with_registry_metadata(tmp_path):
    project_root = tmp_path / "project"
    user_root = tmp_path / "user"
    project_skills = project_root / ".agents" / "skills"
    user_skills = user_root / ".agents" / "skills"
    project_skill = write_skill(
        project_skills, "project-skill", "From frontmatter", "Do project work."
    )
    write_skill(user_skills, "user-skill", "From frontmatter", "Do user work.")
    (project_skill / "references.txt").write_text("supporting data", encoding="utf-8")
    registry = project_root / "skills.json"
    registry.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "name": "project-skill",
                        "description": "Registry description",
                        "location": ".agents/skills/project-skill",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    catalog = load_skills(registry, project_root, user_root)

    assert catalog.get("project-skill") is not None
    assert catalog.get("project-skill").description == "Registry description"
    assert catalog.get("user-skill") is not None
    prompt = catalog.catalog_prompt()
    assert "project-skill: Registry description" in prompt
    assert "activate_skill" in prompt
    activated = catalog.activate("project-skill", ("project-skill",))
    assert "Do project work." in activated
    assert "references.txt" in activated


def test_missing_registry_still_discovers_standard_skills(tmp_path):
    project_root = tmp_path / "project"
    write_skill(
        project_root / ".agents" / "skills",
        "discovered",
        "A discovered skill",
        "Instructions",
    )

    catalog = load_skills(project_root / "missing.json", project_root, tmp_path / "user")

    assert catalog.get("discovered") is not None


def test_activation_rejects_skill_not_allowed_for_subagent(tmp_path):
    project_root = tmp_path / "project"
    write_skill(
        project_root / ".agents" / "skills",
        "private",
        "Private skill",
        "Do private work.",
    )
    catalog = load_skills(project_root / "missing.json", project_root, tmp_path / "user")

    with pytest.raises(SkillError, match="not allowed"):
        catalog.activate("private", ("other",))
    with pytest.raises(SkillError, match="not allowed"):
        catalog.activate("private", ())


@pytest.mark.parametrize(
    ("frontmatter", "description"),
    [
        (
            "description: >-\n  Inspect the project.\n  Use for structure questions.",
            "Inspect the project. Use for structure questions.",
        ),
        (
            "description: |\n  Inspect the project.\n  Use for structure questions.",
            "Inspect the project.\nUse for structure questions.",
        ),
        ('description: "Inspect: the project"', "Inspect: the project"),
        ("description: 'Inspect the user''s project'", "Inspect the user's project"),
    ],
)
def test_discovers_standard_yaml_descriptions(tmp_path, frontmatter, description):
    root = tmp_path / ".agents" / "skills" / "yaml-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\nname: yaml-skill\n{frontmatter}\n---\nDo work.\n")

    catalog = load_skills(tmp_path / "missing.json", tmp_path, tmp_path / "user")

    assert catalog.get("yaml-skill").description == description


def test_malformed_yaml_is_skipped_with_diagnostic(tmp_path, caplog):
    root = tmp_path / ".agents" / "skills" / "broken"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: broken\ndescription: [unclosed\n---\n")

    catalog = load_skills(tmp_path / "missing.json", tmp_path, tmp_path / "user")

    assert catalog.get("broken") is None
    assert "Invalid YAML frontmatter" in caplog.text
