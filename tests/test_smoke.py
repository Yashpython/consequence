"""Trivial smoke test so CI is green from the first commit."""

from consequence.config import load_config


def test_import_and_load_config() -> None:
    config = load_config()
    assert config is not None
