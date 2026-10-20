from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.api.types import Message, ProviderResponse, ToolDefinition
from harness.provider.base import ProviderStreamEvent


@dataclass(frozen=True)
class LocalModelDefinition:
    """A model exposed by one configured local provider."""

    model_id: str
    name: str


@dataclass(frozen=True)
class LocalProviderDefinition:
    """Provider-neutral local configuration consumed by a style adapter."""

    name: str
    display_name: str
    options: dict[str, Any]
    models: dict[str, LocalModelDefinition]


@dataclass(frozen=True)
class LocalModelsDefinition:
    """All local providers and the optional default provider/model pair."""

    providers: dict[str, LocalProviderDefinition]
    default_model: str | None


class LocalModelProvider(Protocol):
    """Interface implemented by each local model API style."""

    @property
    def model(self) -> str: ...

    system: str

    def set_model(self, model: str) -> None: ...

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse: ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderStreamEvent]: ...


def load_local_models(path: Path) -> LocalModelsDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"local provider configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid local provider JSON in {path}: {exc.msg}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("provider"), dict):
        raise ValueError("local_models.json must contain a 'provider' object")

    providers: dict[str, LocalProviderDefinition] = {}
    for provider_name, provider_data in raw["provider"].items():
        if not isinstance(provider_name, str) or not isinstance(provider_data, dict):
            raise ValueError("each local provider must be an object keyed by its provider name")
        options = provider_data.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"local provider {provider_name!r} options must be an object")
        raw_models = provider_data.get("models", {})
        if not isinstance(raw_models, dict):
            raise ValueError(f"local provider {provider_name!r} models must be an object")
        models: dict[str, LocalModelDefinition] = {}
        for model_id, model_data in raw_models.items():
            if not isinstance(model_id, str):
                raise ValueError(f"local provider {provider_name!r} has a non-string model id")
            if isinstance(model_data, dict):
                model_name = model_data.get("name", model_id)
            elif isinstance(model_data, str):
                model_name = model_data
            else:
                raise ValueError(f"local model {provider_name}/{model_id} must be an object")
            if not isinstance(model_name, str):
                raise ValueError(f"local model {provider_name}/{model_id} name must be a string")
            models[model_id] = LocalModelDefinition(model_id, model_name)
        providers[provider_name] = LocalProviderDefinition(
            name=provider_name,
            display_name=str(provider_data.get("name", provider_name)),
            options=options,
            models=models,
        )

    default_model = raw.get("model")
    if default_model is not None and not isinstance(default_model, str):
        raise ValueError("local_models.json 'model' must be a string")
    return LocalModelsDefinition(providers, default_model)


def resolve_local_model(
    config: LocalModelsDefinition,
    configured_model: str | None,
    path: Path,
) -> tuple[LocalProviderDefinition, str]:
    selected = configured_model or config.default_model
    if not selected:
        raise ValueError(f"local provider requires a model in {path.name} or HARNESS_MODEL")
    if "/" in selected:
        provider_name, model_id = selected.split("/", 1)
    elif len(config.providers) == 1:
        provider_name = next(iter(config.providers))
        model_id = selected
    else:
        raise ValueError(f"local model {selected!r} must use provider/model in {path.name}")
    provider = config.providers.get(provider_name)
    if provider is None:
        raise ValueError(f"unknown local provider {provider_name!r} in {path.name}")
    if model_id not in provider.models:
        raise ValueError(f"unknown local model {selected!r} in {path.name}")
    return provider, model_id
