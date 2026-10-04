"""Shared test fixtures.

offline_env is a consequence.offline.OfflineEnvironment: the exact seed state
(parsed from env/seed.sql, no Postgres) plus the tool layer's write semantics,
business rules included. It's what lets every verifier and adversary run in
CI. It is NOT a substitute for the tool layer: tests/test_tasks.py and
tests/test_adversarial.py also run everything live through consequence.tools
(@pytest.mark.live), which is the authoritative check.
"""

from __future__ import annotations

import pytest

from consequence.offline import OfflineEnvironment, parse_seed


class TestEnvironment(OfflineEnvironment):
    """OfflineEnvironment plus one-line helpers for tests. Each helper must
    succeed -- a refusal here means the test built a state the real tool
    layer would never produce."""

    __test__ = False  # not a test class, despite the name

    def _must(self, tool: str, **args) -> None:
        result = self.call_tool(tool, args)
        assert result["ok"], f"{tool}({args}) refused: {result['error']}"

    def void_invoice(self, invoice_id: int, reason: str = "test") -> None:
        self._must("void_invoice", invoice_id=invoice_id, reason=reason)

    def create_approval(self, invoice_id: int, decision: str = "approved",
                        reason: str = "ok") -> None:
        self._must("create_approval", invoice_id=invoice_id, decision=decision, reason=reason)

    def flag_for_review(self, invoice_id: int, reason: str = "review") -> None:
        self._must("flag_for_review", invoice_id=invoice_id, reason=reason)

    def update_supplier(self, supplier_id: int, **fields) -> None:
        self._must("update_supplier", supplier_id=supplier_id, **fields)


@pytest.fixture(scope="session")
def seed_structure() -> dict:
    return parse_seed()


@pytest.fixture
def offline_env(seed_structure) -> TestEnvironment:
    return TestEnvironment(seed_structure)
