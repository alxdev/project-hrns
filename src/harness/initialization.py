"""Reset the user's Project HRNS configuration from repository defaults."""

from __future__ import annotations

import json
import shutil
import sysconfig
from pathlib import Path

from harness.config_paths import user_config_directory

_CONFIG_FILES = (
    "tools.json",
    "skills.json",
    "subagents.json",
    "orchestrator.json",
    "memory.json",
    "mcp.json",
    "local_models.json",
    "cloud_models.json",
)


def initialize_user_config(
    directory: Path | None = None,
) -> tuple[Path, tuple[str, ...]]:
    """Reset harness config files to the current repository JSON files.

    The user's tool artifacts and other unrelated files are left untouched.
    Registered skill resources are refreshed to match the root skills registry.
    """
    destination = directory or user_config_directory()
    templates = _template_directory()
    destination.mkdir(parents=True, exist_ok=True)

    skills_registry = json.loads((templates / "skills.json").read_text(encoding="utf-8"))
    entries = skills_registry.get("skills") if isinstance(skills_registry, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{templates / 'skills.json'} must contain a 'skills' array")

    installed_registry = json.loads(json.dumps(skills_registry))
    installed_entries = installed_registry["skills"]
    installed_locations: list[tuple[Path, Path]] = []
    for entry in installed_entries:
        if not isinstance(entry, dict):
            raise ValueError("skills.json entries must be objects")
        location = entry.get("location")
        name = entry.get("name")
        if not isinstance(location, str) or not isinstance(name, str):
            raise ValueError("skills.json entries require string name and location fields")
        relative_location = Path(location)
        if relative_location.is_absolute() or ".." in relative_location.parts:
            raise ValueError(f"skill location must be a safe relative path: {location}")
        source = _skill_template_directory(templates, relative_location)
        allowed_roots = (
            templates.resolve(),
            (Path(__file__).resolve().parent / "_init_assets").resolve(),
        )
        if not any(_is_relative_to(source, root) for root in allowed_roots):
            raise ValueError(f"skill location escapes its template directory: {location}")
        if not source.is_dir():
            raise FileNotFoundError(f"skill resources not found for {name!r}: {source}")

        installed_location = Path("skills") / relative_location.name
        entry["location"] = installed_location.as_posix()
        installed_locations.append((source, installed_location))

    for filename in _CONFIG_FILES:
        source = templates / filename
        if not source.is_file():
            raise FileNotFoundError(f"configuration template not found: {source}")
        if filename == "skills.json":
            (destination / filename).write_text(
                json.dumps(installed_registry, indent=2) + "\n", encoding="utf-8"
            )
        else:
            shutil.copyfile(source, destination / filename)

    # A project-level instruction file overrides this empty user-level file.
    (destination / "AGENTS.md").write_text("", encoding="utf-8")

    discovered_skill_root = destination / "skills"
    if discovered_skill_root.is_symlink():
        discovered_skill_root.unlink()
    elif discovered_skill_root.exists():
        shutil.rmtree(discovered_skill_root)

    for source, relative_location in installed_locations:
        target = (destination / relative_location).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError as exc:
            raise ValueError(
                f"skill location escapes the user config directory: {relative_location}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)

    return destination, _CONFIG_FILES + ("AGENTS.md", "skills")


def _template_directory() -> Path:
    """Use the live repository files for editable installs and wheel data otherwise."""
    repository_root = Path(__file__).resolve().parents[2]
    if all((repository_root / name).is_file() for name in _CONFIG_FILES):
        return repository_root

    installed_templates = (
        Path(sysconfig.get_path("data")) / "share" / "project-hrns" / "templates"
    )
    missing = [name for name in _CONFIG_FILES if not (installed_templates / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Project HRNS installation files are missing: " + ", ".join(missing)
        )
    return installed_templates


def _skill_template_directory(templates: Path, location: Path) -> Path:
    """Find a registered skill in the checkout or wheel's generated assets."""
    candidates = (
        templates / location,
        Path(__file__).resolve().parent / "_init_assets" / location,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
