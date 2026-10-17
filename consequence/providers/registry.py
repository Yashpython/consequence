"""Loads consequence/models.yaml and computes cost from pinned prices.

Adding a model is a config change here, not a rewrite: add a row to
models.yaml naming an already-supported `provider`, and make_provider()
picks it up with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # The output cap sent with every call (thinking tokens count toward it on
    # models that think). None keeps the adapter's default.
    max_output_tokens: int | None = None
    # Adapter-specific settings from models.yaml `options:`, passed to the
    # adapter's constructor as they are (e.g. the Anthropic adapter's
    # thinking_display). Unknown keys fail when the adapter is built.
    options: dict[str, Any] = field(default_factory=dict)


def load_model_registry(path: str | Path = MODELS_YAML_PATH) -> dict[str, ModelSpec]:
    """Parse models.yaml into {id: ModelSpec}.

    Every model under test gets the same output cap, models.yaml's top-level
    `agent_max_output_tokens`: one experimental condition for all of them. A
    per-model `max_output_tokens` under `models:` is refused, so the cap can't
    quietly differ between the models being compared."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cap = raw.get("agent_max_output_tokens")
    if raw.get("models") and (not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0):
        raise ValueError(f"models.yaml: agent_max_output_tokens must be a positive integer, "
                         f"got {cap!r}")
    specs: dict[str, ModelSpec] = {}
    for entry in raw.get("models", []):
        if "max_output_tokens" in entry:
            raise ValueError(f"models.yaml model {entry.get('id')}: no per-model "
                             "max_output_tokens; every model under test runs at "
                             "agent_max_output_tokens")
        spec = ModelSpec(
            id=entry["id"],
            provider=entry["provider"],
            api_model=entry["api_model"],
            input_price_per_million=float(entry["input_price_per_million"]),
            output_price_per_million=float(entry["output_price_per_million"]),
            max_turns=entry.get("max_turns"),
            price_note=entry.get("price_note"),
            max_output_tokens=cap,
            options=dict(entry.get("options") or {}),
        )
        specs[spec.id] = spec
    return specs


# How a judge samples. TEMPERATURE: the judge is sent an explicit temperature
# (0 for its verdicts, STABILITY_TEMPERATURE for the stability subset).
# PROVIDER_DEFAULT: no temperature is ever sent, because the model rejects
# one (current Claude models do), so its votes are draws at the provider's
# default sampling and their agreement is a stability measurement.
TEMPERATURE = "temperature"
PROVIDER_DEFAULT = "provider_default"
SAMPLING_MODES = (TEMPERATURE, PROVIDER_DEFAULT)


@dataclass(frozen=True)
class JudgeSpec(ModelSpec):
    """A model used to grade transcripts (models.yaml `judges:`), kept apart
    from the models under test so a judge id never appears as an episode's
    model_id. context_window_tokens is None until it is set from the
    vendor's documentation; the judge refuses to run without it, because
    checking for context overflow is what stops a transcript being cut."""

    context_window_tokens: int | None = None
    primary: bool = False
    sampling: str = TEMPERATURE


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
            max_output_tokens=entry.get("max_output_tokens"),
            options=dict(entry.get("options") or {}),
            sampling=entry.get("sampling", TEMPERATURE),
        )
        if spec.sampling not in SAMPLING_MODES:
            raise ValueError(f"models.yaml judge {spec.id}: sampling must be one of "
                             f"{SAMPLING_MODES}, got {spec.sampling!r}")
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


# Each adapter's own name for the output cap.
_OUTPUT_CAP_KWARG = {"google": "max_output_tokens"}


def make_provider(
    model_id: str, registry: dict[str, ModelSpec] | None = None, *,
    max_output_tokens: int | None = None, **kwargs: Any
) -> Provider:
    """Build the right adapter for model_id, per its `provider` field in
    models.yaml, with the entry's `options` and `max_output_tokens`.
    max_output_tokens here overrides the entry's; other kwargs (e.g. api_key,
    max_retries, temperature) pass through to the adapter's constructor."""
    registry = registry if registry is not None else load_model_registry()
    spec = registry.get(model_id)
    if spec is None:
        raise KeyError(f"no model {model_id!r} in models.yaml")
    factory = _factories().get(spec.provider)
    if factory is None:
        raise KeyError(f"no provider adapter registered for {spec.provider!r}")
    cap = max_output_tokens if max_output_tokens is not None else spec.max_output_tokens
    if cap is not None:
        kwargs.setdefault(_OUTPUT_CAP_KWARG.get(spec.provider, "max_tokens"), cap)
    return factory(model_id=spec.id, api_model=spec.api_model, **{**spec.options, **kwargs})
