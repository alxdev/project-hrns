from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.provider import (
    AnthropicProvider,
    LocalOpenAICompatibleProvider,
    OpenAIProvider,
)
from harness.provider.local_models import load_local_models


@dataclass(frozen=True)
class ModelChoice:
    """A selectable runtime model from either the local or cloud catalog."""

    key: str
    provider: str
    model: str
    name: str
    options: dict[str, Any]


@dataclass(frozen=True)
class ModelCatalog:
    choices: tuple[ModelChoice, ...]

    @classmethod
    def load(cls, root: Path) -> ModelCatalog:
        choices: list[ModelChoice] = []
        local_path = root / "local_models.json"
        if local_path.exists():
            local = load_local_models(local_path)
            for provider_name, provider in local.providers.items():
                for model in provider.models.values():
                    choices.append(
                        ModelChoice(
                            key=f"local/{provider_name}/{model.model_id}",
                            provider="local",
                            model=f"{provider_name}/{model.model_id}",
                            name=model.name,
                            options=provider.options,
                        )
                    )

        cloud_path = root / "cloud_models.json"
        if cloud_path.exists():
            choices.extend(_load_cloud_models(cloud_path))
        return cls(tuple(choices))

    def find(self, key: str) -> ModelChoice:
        normalized = key.removeprefix("/").strip()
        for choice in self.choices:
            if choice.key == normalized:
                return choice
        raise ValueError(f"unknown model {key!r}; use /models to list available models")

    def describe(self, current: str | None = None) -> str:
        if not self.choices:
            return "No models configured in local_models.json or cloud_models.json."
        lines = ["Available models"]
        for choice in self.choices:
            marker = " *" if choice.key == current else ""
            lines.append(f"  {choice.key} — {choice.name}{marker}")
        return "\n".join(lines)


ConfiguredProvider = OpenAIProvider | AnthropicProvider | LocalOpenAICompatibleProvider


def build_provider_for_choice(choice: ModelChoice) -> ConfiguredProvider:
    if choice.provider == "openai":
        api_key_env = choice.options.get("apiKeyEnv", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env) if isinstance(api_key_env, str) else None
        base_url = choice.options.get("baseURL", "https://api.openai.com/v1")
        if not isinstance(base_url, str):
            raise ValueError(f"baseURL for {choice.key} must be a string")
        return OpenAIProvider(api_key=api_key, model=choice.model, base_url=base_url)
    if choice.provider == "anthropic":
        api_key_env = choice.options.get("apiKeyEnv", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env) if isinstance(api_key_env, str) else None
        base_url = choice.options.get("baseURL", "https://api.anthropic.com")
        if not isinstance(base_url, str):
            raise ValueError(f"baseURL for {choice.key} must be a string")
        return AnthropicProvider(api_key=api_key, model=choice.model, base_url=base_url)
    if choice.provider == "local":
        return LocalOpenAICompatibleProvider(model=choice.model)
    raise ValueError(f"unsupported model provider: {choice.provider}")


def _load_cloud_models(path: Path) -> list[ModelChoice]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid cloud model JSON in {path}: {exc.msg}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("provider"), dict):
        raise ValueError("cloud_models.json must contain a 'provider' object")

    choices: list[ModelChoice] = []
    for provider_name, provider_data in raw["provider"].items():
        if not isinstance(provider_name, str) or not isinstance(provider_data, dict):
            raise ValueError("each cloud provider must be an object keyed by its provider name")
        raw_models = provider_data.get("models", {})
        if not isinstance(raw_models, dict):
            raise ValueError(f"cloud provider {provider_name!r} models must be an object")
        options = provider_data.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"cloud provider {provider_name!r} options must be an object")
        for model_id, model_data in raw_models.items():
            if not isinstance(model_id, str):
                raise ValueError(f"cloud provider {provider_name!r} has a non-string model id")
            name = model_id
            if isinstance(model_data, dict):
                configured_name = model_data.get("name")
                if configured_name is not None:
                    if not isinstance(configured_name, str):
                        raise ValueError(
                            f"cloud model {provider_name}/{model_id} name must be a string"
                        )
                    name = configured_name
            elif isinstance(model_data, str):
                name = model_data
            else:
                raise ValueError(f"cloud model {provider_name}/{model_id} must be an object")
            model_options = dict(options)
            if isinstance(model_data, dict):
                model_options.update(
                    {key: value for key, value in model_data.items() if key != "name"}
                )
            choices.append(
                ModelChoice(
                    key=f"{provider_name}/{model_id}",
                    provider=provider_name,
                    model=model_id,
                    name=name,
                    options=model_options,
                )
            )
    return choices
