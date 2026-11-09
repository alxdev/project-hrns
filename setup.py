"""Build hook for including only registered skill resources in wheels."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithRegisteredSkills(build_py):
    """Bundle source skill files referenced by the root skills registry."""

    def run(self) -> None:
        super().run()
        repository_root = Path(__file__).resolve().parent
        registry_path = repository_root / "skills.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        entries = registry.get("skills") if isinstance(registry, dict) else None
        if not isinstance(entries, list):
            raise ValueError(f"{registry_path} must contain a 'skills' array")

        self._registered_skill_outputs: list[str] = []
        asset_root = Path(self.build_lib) / "harness" / "_init_assets"
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("location"), str):
                raise ValueError("skills.json entries require a string location")
            location = Path(entry["location"])
            if location.is_absolute() or ".." in location.parts:
                raise ValueError(f"skill location must be relative to the repository: {location}")
            source = (repository_root / location).resolve()
            try:
                source.relative_to((repository_root / ".agents" / "skills").resolve())
            except ValueError as exc:
                raise ValueError(
                    f"registered skill location must be under .agents/skills: {location}"
                ) from exc
            if not source.is_dir():
                raise FileNotFoundError(f"skill resources not found: {source}")
            target = asset_root / location
            shutil.copytree(source, target, dirs_exist_ok=True)
            self._registered_skill_outputs.extend(
                str(path) for path in target.rglob("*") if path.is_file()
            )

    def get_outputs(self, include_bytecode: int = 1) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        return outputs + getattr(self, "_registered_skill_outputs", [])


setup(cmdclass={"build_py": BuildPyWithRegisteredSkills})
