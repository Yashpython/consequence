"""The model registry: consequence/models.yaml, loaded and validated.

Adding a model is a config change: a new entry in models.yaml, not code.
Every entry pins an exact API model string and the prices used to compute
cost; see docs/models.md for why each model was chosen.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from consequence.providers.common import HTTPProvider, PriceTier, Pricing, RetryPolicy

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

PROVIDERS = ("anthropic", "openai", "google", "openrouter")

# An OpenAI alias ("gpt-5.5") silently moves to a new snapshot; only the
# dated snapshot string is pinned.
_OPENAI_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    api_model: str
    pricing: Pricing
    max_output_tokens: int
    max_turns: int | None = None
    # Extra request-body fields, merged in verbatim (e.g. reasoning effort).
    # Pinned here so a provider-side default change can't alter a run.
    params: Mapping[str, Any] = field(default_factory=dict)
    # OpenRouter only: the provider-routing object, pinning which upstream
    # host (and so which weights/quantization and price) serves the model.
    routing: Mapping[str, Any] | None = None


def _price(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise RegistryError(f"{where}: price must be a number, got {value!r}")
    price = Decimal(str(value))
    if price < 0:
        raise RegistryError(f"{where}: price must be >= 0")
    return price


def _tier(raw: Mapping[str, Any], where: str) -> PriceTier:
    def opt(key: str) -> Decimal | None:
        return _price(raw[key], f"{where}.{key}") if raw.get(key) is not None else None

    try:
        return PriceTier(
            input_per_mtok=_price(raw["input_price_per_mtok"], f"{where}.input_price_per_mtok"),
            output_per_mtok=_price(raw["output_price_per_mtok"], f"{where}.output_price_per_mtok"),
            cached_input_per_mtok=opt("cached_input_price_per_mtok"),
            cache_write_per_mtok=opt("cache_write_price_per_mtok"),
        )
    except KeyError as exc:
        raise RegistryError(f"{where}: missing {exc.args[0]}") from None


def check_pinned(provider: str, api_model: str) -> None:
    """Reject strings that are, or could silently become, a floating alias."""
    lowered = api_model.lower()
    if "latest" in lowered:
        raise RegistryError(f"{api_model!r} is a floating alias; pin an exact version")
    if provider == "openai" and not _OPENAI_SNAPSHOT.search(api_model):
        raise RegistryError(f"{api_model!r} is not a dated OpenAI snapshot (…-YYYY-MM-DD)")
    if provider == "openrouter" and (":" in api_model or api_model.startswith("openrouter/")):
        # ":free"/":nitro"/":floor" variants and openrouter/auto re-route freely.
        raise RegistryError(f"{api_model!r} is a routing variant, not a pinned model")


def parse_entry(raw: Mapping[str, Any]) -> ModelSpec:
    where = f"model {raw.get('id', '?')!r}"
    for key in ("id", "provider", "api_model", "max_output_tokens"):
        if key not in raw:
            raise RegistryError(f"{where}: missing {key}")
    provider = raw["provider"]
    if provider not in PROVIDERS:
        raise RegistryError(f"{where}: unknown provider {provider!r}")
    check_pinned(provider, raw["api_model"])

    long_raw = raw.get("long_context")
    long_tier = threshold = None
    if long_raw is not None:
        long_tier = _tier(long_raw, f"{where}.long_context")
        threshold = long_raw.get("threshold_input_tokens")
        if not isinstance(threshold, int) or threshold <= 0:
            raise RegistryError(f"{where}.long_context: threshold_input_tokens must be > 0")

    routing = raw.get("routing")
    if provider == "openrouter":
        if not routing or routing.get("allow_fallbacks") is not False:
            raise RegistryError(
                f"{where}: OpenRouter models must pin routing with allow_fallbacks: false"
            )
    elif routing is not None:
        raise RegistryError(f"{where}: routing only applies to openrouter models")

    max_turns = raw.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or max_turns <= 0):
        raise RegistryError(f"{where}: max_turns must be a positive integer")

    return ModelSpec(
        id=raw["id"],
        provider=provider,
        api_model=raw["api_model"],
        pricing=Pricing(base=_tier(raw, where), long_context=long_tier,
                        long_context_threshold=threshold),
        max_output_tokens=int(raw["max_output_tokens"]),
        max_turns=max_turns,
        params=dict(raw.get("params") or {}),
        routing=dict(routing) if routing else None,
    )


def load_registry(path: str | Path | None = None) -> dict[str, ModelSpec]:
    with open(path or DEFAULT_REGISTRY_PATH, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    specs: dict[str, ModelSpec] = {}
    for raw in doc.get("models", []):
        spec = parse_entry(raw)
        if spec.id in specs:
            raise RegistryError(f"duplicate model id {spec.id!r}")
        specs[spec.id] = spec
    return specs


def build_provider(
    spec: ModelSpec, api_key: str | None = None, *, retry: RetryPolicy | None = None
) -> HTTPProvider:
    """Instantiate the adapter for a registry entry, keyed from the environment."""
    from consequence.config import load_config
    from consequence.providers.anthropic import AnthropicProvider
    from consequence.providers.google import GoogleProvider
    from consequence.providers.openai import OpenAIProvider
    from consequence.providers.openrouter import OpenRouterProvider

    adapters: dict[str, tuple[type[HTTPProvider], str]] = {
        "anthropic": (AnthropicProvider, "anthropic_api_key"),
        "openai": (OpenAIProvider, "openai_api_key"),
        "google": (GoogleProvider, "google_api_key"),
        "openrouter": (OpenRouterProvider, "openrouter_api_key"),
    }
    cls, key_field = adapters[spec.provider]
    key = api_key or getattr(load_config(), key_field)
    if not key:
        raise RegistryError(f"{spec.id}: no API key ({key_field.upper()} is unset)")
    return cls(spec, key, retry=retry)
