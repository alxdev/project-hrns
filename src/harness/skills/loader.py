from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class SkillError(ValueError):
    """Raised when a configured skill cannot be loaded safely."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    location: Path
    manifest: Path
    source: str = "project"

    @property
    def root(self) -> Path:
        return self.manifest.parent


class SkillCatalog:
    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills = dict(skills or {})

    @property
    def skills(self) -> dict[str, Skill]:
        return dict(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def for_names(self, names: tuple[str, ...]) -> SkillCatalog:
        return SkillCatalog({name: self._skills[name] for name in names if name in self._skills})

    def catalog_prompt(self) -> str:
        if not self._skills:
            return ""
        lines = [
            "## Available skills",
            "When a task matches a skill, call activate_skill with its name before proceeding.",
            "Skill instructions are workflow guidance; they do not grant tools or permissions.",
        ]
        for skill in sorted(self._skills.values(), key=lambda item: item.name):
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def activate(self, name: str, allowed: tuple[str, ...] = ()) -> str:
        if name not in allowed:
            raise SkillError(f"skill {name!r} is not allowed for this subagent")
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"unknown skill {name!r}")
        try:
            content = skill.manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillError(f"cannot read skill {name!r}: {exc}") from exc

        resources: list[str] = []
        for path in sorted(skill.root.rglob("*")):
            if not path.is_file() or path == skill.manifest:
                continue
            try:
                path.resolve().relative_to(skill.root.resolve())
            except ValueError:
                continue
            resources.append(path.relative_to(skill.root).as_posix())
        resource_text = "\n".join(f"- {item}" for item in resources) or "- none"
        return (
            f"<skill_content name={name!r}>\n"
            f"skill_directory: {skill.root}\n"
            "read_file prefers existing workspace paths. If a relative path is missing there, "
            "it uses a unique readable file in assigned skill roots; "
            "multiple matches are an error.\n"
            f"For supporting files in this skill, pass skill={name!r} with a relative path "
            "inside skill_directory to select it explicitly, without workspace fallback.\n"
            "available_resources:\n"
            f"{resource_text}\n\n"
            f"{content.rstrip()}\n"
            "</skill_content>"
        )


def load_skills(
    registry_path: Path,
    project_root: Path,
    user_root: Path | None = None,
) -> SkillCatalog:
    """Load explicit registrations and standard .agents/skills directories."""
    skills: dict[str, Skill] = {}
    if user_root is None:
        user_root = Path.home()

    _discover(skills, user_root / ".agents" / "skills", "user")
    _discover(skills, project_root / ".agents" / "skills", "project")
    if registry_path.exists():
        _load_registry(skills, registry_path)
    return SkillCatalog(skills)


def _discover(skills: dict[str, Skill], root: Path, source: str) -> None:
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = _find_manifest(directory)
        if manifest is None:
            continue
        name, description = _frontmatter(manifest)
        if not name or not description:
            continue
        skills[name] = Skill(name, description, directory, manifest, source)


def _load_registry(skills: dict[str, Skill], path: Path) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SkillError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SkillError(f"invalid JSON in {path}: {exc.msg}") from exc

    entries = raw.get("skills") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise SkillError(f"{path} must contain a 'skills' array")
    for entry in entries:
        if not isinstance(entry, dict):
            raise SkillError(f"{path} skill entries must be objects")
        name = _required_string(entry, "name", path)
        description = _required_string(entry, "description", path)
        location = _required_string(entry, "location", path)
        manifest_or_root = Path(location).expanduser()
        if not manifest_or_root.is_absolute():
            manifest_or_root = (path.parent / manifest_or_root).resolve()
        manifest = (
            manifest_or_root if manifest_or_root.is_file() else _find_manifest(manifest_or_root)
        )
        if manifest is None:
            raise SkillError(f"{path} skill {name!r} has no SKILL.md at {location}")
        root = manifest.parent.resolve()
        skills[name] = Skill(name, description, root, manifest.resolve(), "registry")


def _find_manifest(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        if path.is_file() and path.name.lower() == "skill.md":
            return path
    return None


def _frontmatter(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.warning("Cannot read skill %s: %s", path, exc)
        return "", ""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return "", ""
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        logger.warning("Missing closing frontmatter delimiter in %s", path)
        return "", ""
    try:
        values = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, exc)
        return "", ""
    if not isinstance(values, dict):
        logger.warning("Frontmatter in %s must be a mapping", path)
        return "", ""
    name, description = values.get("name"), values.get("description")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(description, str)
        or not description.strip()
    ):
        logger.warning("Skill %s needs string name and description fields", path)
        return "", ""
    return name.strip(), description.strip()


def _required_string(entry: dict[str, object], field: str, path: Path) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SkillError(f"{path} skill entries require a non-empty {field}")
    return value.strip()
