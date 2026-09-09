"""Settings loaded from the environment. No secrets are hardcoded here."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # python-dotenv is a dev dependency; optional at runtime.
    pass


def _get(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


@dataclass(frozen=True)
class Config:
    """Runtime configuration, populated entirely from environment variables."""

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None
    openrouter_api_key: str | None = None
    database_url: str | None = None
    results_db_path: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            openai_api_key=_get("OPENAI_API_KEY"),
            google_api_key=_get("GOOGLE_API_KEY"),
            openrouter_api_key=_get("OPENROUTER_API_KEY"),
            database_url=_get("DATABASE_URL"),
            results_db_path=_get("RESULTS_DB_PATH"),
        )


def load_config() -> Config:
    """Return a Config built from the current environment."""
    return Config.from_env()
