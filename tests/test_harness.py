"""Harness tests: real Postgres (via reset()/snapshot()), zero API calls.

Every provider here is a MockProvider -- no network, no spend. Marked
@pytest.mark.live because run_episode() resets and snapshots the real
database; run with `make up` then `make test-live`.
"""

from __future__ import annotations

import json

import pytest

from consequence.harness import Task, run_episode
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.mock import MockProvider
from consequence.results import Results

pytestmark = pytest.mark.live


@pytest.fixture
def results(tmp_path):
    with Results(tmp_path / "results.db") as r:
        yield r


def _void_call(invoice_id: int, call_id: str = "1") -> ToolCall:
    return ToolCall(id=call_id, name="void_invoice", arguments={"invoice_id": invoice_id, "reason": "test"})


def test_scripted_correct_agent_produces_expected_diff_and_completes(results):
    task = Task(task_id="void-invoice-2", instructions="Void invoice 2.")
    provider = MockProvider(
        model_id="mock-1",
        script=[
            TurnResult(
                text="Voiding invoice 2.",
                tool_calls=(_void_call(2),),
                input_tokens=10,
                output_tokens=5,
                latency_ms=100,
            ),
            TurnResult(text="Done.", tool_calls=(), input_tokens=5, output_tokens=3, latency_ms=50),
        ],
    )

    record = run_episode(task, provider, results, trial_index=0)

    assert record.status == "completed"
    assert record.error is None
    assert record.turn_count == 2
    assert record.diff.changed == [("invoices", 2, {"status": ("active", "void")})]
    assert record.diff.added == []  # touched() excludes audit_log by default in this check
    assert record.diff.touched() == {("invoices", 2)}

    episodes = results.episodes_for_run(record.run_id)
    assert len(episodes) == 1
    assert episodes[0]["status"] == "completed"
    assert episodes[0]["turn_count"] == 2
    assert episodes[0]["input_tokens"] == 15
    assert episodes[0]["output_tokens"] == 8


def test_agent_with_no_tool_calls_produces_empty_diff_and_full_transcript(results):
    task = Task(task_id="noop", instructions="Do something useful.")
    provider = MockProvider(
        model_id="mock-1",
        script=[TurnResult(text="I've already handled it.", tool_calls=())],
    )

    record = run_episode(task, provider, results, trial_index=0)

    assert record.status == "completed"
    assert record.diff.is_empty()

    with results._conn as conn:
        rows = conn.execute(
            "SELECT role, turn_index FROM transcripts WHERE episode_id = ? ORDER BY turn_index",
            (record.episode_id,),
        ).fetchall()
    roles = [r["role"] for r in rows]
    assert roles == ["system", "user", "assistant"]


def test_max_turns_is_enforced_and_recorded(results):
    task = Task(task_id="stuck-in-a-loop", instructions="Keep checking invoice 2.")
    # Every scripted turn calls a read-only tool and never stops on its own.
    provider = MockProvider(
        model_id="mock-1",
        script=[
            TurnResult(text="checking...", tool_calls=(ToolCall("1", "get_invoice", {"invoice_id": 2}),))
            for _ in range(10)
        ],
    )

    record = run_episode(task, provider, results, trial_index=0, max_turns=3)

    assert record.status == "max_turns"
    assert record.turn_count == 3
    assert record.diff.is_empty()  # get_invoice is read-only

    episodes = results.episodes_for_run(record.run_id)
    assert episodes[0]["status"] == "max_turns"
    assert episodes[0]["turn_count"] == 3


def test_provider_exception_is_recorded_as_error_without_corrupting_results(results):
    task = Task(task_id="provider-blows-up", instructions="Void invoice 2.")
    provider = MockProvider(model_id="mock-1", script=[RuntimeError("upstream API blew up")])

    record = run_episode(task, provider, results, trial_index=0)

    assert record.status == "error"
    assert "upstream API blew up" in record.error
    assert record.diff.is_empty()  # nothing was ever executed

    episodes = results.episodes_for_run(record.run_id)
    assert len(episodes) == 1
    assert episodes[0]["status"] == "error"
    assert "upstream API blew up" in episodes[0]["error"]

    # the state_diff and transcript rows for this episode still exist and
    # are well-formed -- a provider crash doesn't leave a half-written record.
    with results._conn as conn:
        diff_row = conn.execute(
            "SELECT * FROM state_diffs WHERE episode_id = ?", (record.episode_id,)
        ).fetchone()
        transcript_rows = conn.execute(
            "SELECT * FROM transcripts WHERE episode_id = ?", (record.episode_id,)
        ).fetchall()
    assert diff_row is not None
    assert json.loads(diff_row["diff_json"]) == {"added": [], "removed": [], "changed": []}
    assert len(transcript_rows) == 2  # system + user; the model never got to reply


def test_environment_is_reset_before_every_episode(results):
    task_a = Task(task_id="void-invoice-2", instructions="Void invoice 2.")
    provider_a = MockProvider(
        model_id="mock-1",
        script=[TurnResult(text="done", tool_calls=(_void_call(2),))],
    )
    record_a = run_episode(task_a, provider_a, results, trial_index=0)
    assert record_a.diff.touched() == {("invoices", 2)}

    # A second, unrelated episode must see the pristine seed state again --
    # not the invoice-2-voided state record_a left behind.
    task_b = Task(task_id="void-invoice-3", instructions="Void invoice 3.")
    provider_b = MockProvider(
        model_id="mock-1",
        script=[TurnResult(text="done", tool_calls=(_void_call(3, call_id="1"),))],
    )
    record_b = run_episode(task_b, provider_b, results, trial_index=0)

    assert record_a.digest_before == record_b.digest_before
    assert record_b.diff.touched() == {("invoices", 3)}
