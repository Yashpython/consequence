"""Provider adapters: translate the harness's provider-neutral turn protocol
into a specific model API's wire format, and back. See base.py for the
interface every adapter implements, mock.py for the zero-spend test double,
registry.py for models.yaml + cost calculation + the adapter factory, and
anthropic.py/openai.py/google.py/openrouter.py for the real adapters.
"""

from consequence.providers.base import Provider, ToolCall, TurnResult
from consequence.providers.mock import MockProvider
from consequence.providers.registry import (
    ModelSpec,
    compute_cost,
    load_model_registry,
    make_provider,
)

__all__ = [
    "MockProvider",
    "ModelSpec",
    "Provider",
    "ToolCall",
    "TurnResult",
    "compute_cost",
    "load_model_registry",
    "make_provider",
]
