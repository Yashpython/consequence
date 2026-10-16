"""Loads consequence/models.yaml and computes cost from pinned prices.

Adding a model is a config change here, not a rewrite: add a row to
models.yaml naming an already-supported `provider`, and make_provider()
picks it up with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from consequence.providers.base import Provider

MODELS_YAML_PATH = Path(__file__).resolve().parent.parent / "models.yaml"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    api_model: str
    input_price_per_million: float
    output_price_per_million: float
    max_turns: int | None = None
    # A pricing caveat the dry-run estimate prints: a price change from the
    # model this one replaced, or a scheduled change to this one.
    price_note: str | None = None


def load_model_registry(path: str | Path = MODELS_YAML_PATH) -> dict[str, ModelSpec]:
    """Parse models.yaml into {id: ModelSpec}."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    specs: dict[str, ModelSpec] = {}
    for entry in raw.get("models", []):
        spec = ModelSpec(
            id=entry["id"],
            provider=entry["provider"],
            api_model=entry["api_model"],
            input_price_per_million=float(entry["input_price_per_million"]),
            output_price_per_million=float(entry["output_price_per_million"]),
            max_turns=entry.get("max_turns"),
            price_note=entry.get("price_note"),
        )
        specs[spec.id] = spec
    return specs


@dataclass(frozen=True)
class JudgeSpec(ModelSpec):
    """A model used to grade transcripts (models.yaml `judges:`), kept apart
    from the models under test so a judge id never appears as an episode's
    model_id. context_window_tokens is None until it is set from the
    vendor's documentation; the judge refuses to run without it, because
    checking for context overflow is what stops a transcript being cut."""

    context_window_tokens: int | None = None
    primary: bool = False


def load_judge_registry(path: str | Path = MODELS_YAML_PATH) -> dict[str, JudgeSpec]:
    """Parse the `judges:` section into {id: JudgeSpec}. Exactly one judge is
    primary: the one every episode is graded by."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    specs: dict[str, JudgeSpec] = {}
    for entry in raw.get("judges", []):
        spec = JudgeSpec(
            id=entry["id"],
            provider=entry["provider"],
            api_model=entry["api_model"],
            input_price_per_million=float(entry["input_price_per_million"]),
            output_price_per_million=float(entry["output_price_per_million"]),
            context_window_tokens=entry.get("context_window_tokens"),
            primary=bool(entry.get("primary", False)),
            price_note=entry.get("price_note"),
        )
        specs[spec.id] = spec
    primaries = [s.id for s in specs.values() if s.primary]
    if specs and len(primaries) != 1:
        raise ValueError(f"models.yaml judges: exactly one must be primary, found {primaries}")
    return specs


def primary_judge(registry: dict[str, JudgeSpec]) -> JudgeSpec:
    return next(s for s in registry.values() if s.primary)


def compute_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    registry: dict[str, ModelSpec] | None = None,
) -> float | None:
    """Cost in USD from pinned prices in models.yaml plus actual token
    counts. Returns None if model_id isn't in the registry -- never a guess.
    """
    registry = registry if registry is not None else load_model_registry()
    spec = registry.get(model_id)
    if spec is None:
        return None
    return (
        input_tokens * spec.input_price_per_million
        + output_tokens * spec.output_price_per_million
    ) / 1_000_000


_PROVIDER_FACTORIES: dict[str, Any] = {}


def _factories() -> dict[str, Any]:
    """Lazy so importing this module never requires every vendor SDK."""
    if not _PROVIDER_FACTORIES:
        from consequence.providers.anthropic import AnthropicProvider
        from consequence.providers.google import GoogleProvider
        from consequence.providers.openai import OpenAIProvider
        from consequence.providers.openrouter import OpenRouterProvider

        _PROVIDER_FACTORIES.update(
            {
                "anthropic": AnthropicProvider,
                "openai": OpenAIProvider,
                "google": GoogleProvider,
                "openrouter": OpenRouterProvider,
            }
        )
    return _PROVIDER_FACTORIES


def make_provider(
    model_id: str, registry: dict[str, ModelSpec] | None = None, **kwargs: Any
) -> Provider:
    """Build the right adapter for model_id, per its `provider` field in
    models.yaml. kwargs (e.g. api_key, max_retries) pass through to the
    adapter's constructor."""
    registry = registry if registry is not None else load_model_registry()
    spec = registry.get(model_id)
    if spec is None:
        raise KeyError(f"no model {model_id!r} in models.yaml")
    factory = _factories().get(spec.provider)
    if factory is None:
        raise KeyError(f"no provider adapter registered for {spec.provider!r}")
    return factory(model_id=spec.id, api_model=spec.api_model, **kwargs)
