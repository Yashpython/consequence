"""Reasoning state round trips: stored per agent turn, counted per model.

Claude thinking blocks and Gemini thought signatures must go back verbatim on
the next call. The smoke tests show that once per model; the full run shows
it at scale: how many agent turns carried reasoning state, how many of those
were sent back on a next call, and how many of those calls were accepted.
"""

from __future__ import annotations

from dataclasses import asdict

from helpers import seed_run

from consequence.harness import run_episode
from consequence.metrics import RunMetrics
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.mock import MockProvider
from consequence.report import build_report, render_markdown
from consequence.results import Results
from consequence.tasks import TASKS_BY_ID


def test_every_agent_turn_stores_whether_it_carried_reasoning_state(offline_harness, tmp_path):
    provider = MockProvider(model_id="mock", script=[
        TurnResult(text=None, tool_calls=(ToolCall("c0", "get_invoice", {"invoice_id": 12}),),
                   reasoning_state=True, replay=[{"type": "thinking"}]),
        TurnResult(text="Done."),
    ])
    with Results(tmp_path / "r.db") as results:
        record = run_episode(TASKS_BY_ID["001_void_duplicate"], provider, results, 0)
        turns = results.transcript_for_episode(record.episode_id)
    assert [t["reasoning_state"] for t in turns if t["role"] == "assistant"] == [1, 0]
    assert all(t["reasoning_state"] is None for t in turns if t["role"] != "assistant")


def _agent(index, carried):
    return {"turn_index": index, "role": "assistant", "content": None, "stop_reason": "x",
            "output_capped": False, "reasoning_state": carried}


def _tool(index):
    return {"turn_index": index, "role": "tool", "tool_name": "get_invoice",
            "tool_args": {"invoice_id": 12}, "tool_result": {"ok": True}}


def _episode(results, run_id, model, status, turns):
    episode = results.record_episode(run_id=run_id, task_id="001_void_duplicate",
                                     model_id=model, trial_index=0, status=status)
    results.record_transcript(episode, turns)


def test_round_trips_separate_accepted_failed_and_never_sent_back(tmp_path):
    path = tmp_path / "r.db"
    with Results(path) as results:
        run_id = seed_run(results)
        # Carried, sent back, accepted; then carried again as the final answer.
        _episode(results, run_id, "m-a", "completed",
                 [_agent(0, True), _tool(1), _agent(2, True)])
        # Carried, sent back, and the next call raised.
        _episode(results, run_id, "m-a", "error", [_agent(0, True), _tool(1)])
        # Carried, but the turn limit ended the episode before a next call.
        _episode(results, run_id, "m-a", "max_turns", [_agent(0, True), _tool(1)])
        # No reasoning state at all.
        _episode(results, run_id, "m-a", "completed", [_agent(0, False)])
        _episode(results, run_id, "m-b", "completed",
                 [_agent(0, True), _tool(1), _agent(2, False)])
    with RunMetrics(path, run_id) as m:
        mine, theirs = m.reasoning_round_trips("m-a"), m.reasoning_round_trips("m-b")
        data = build_report(m)
    assert (mine.agent_turns, mine.carried, mine.sent_back) == (5, 4, 2)
    assert (mine.accepted.successes, mine.accepted.n) == (1, 2)
    assert (mine.next_call_failed, mine.not_sent_back) == (1, 2)
    assert (theirs.carried, theirs.accepted.successes, theirs.accepted.n) == (1, 1, 1)
    assert data["models"]["m-a"]["reasoning_round_trips"] == asdict(mine)
    md = render_markdown(data)
    assert "Reasoning state sent back" in md and "1 of 2" in md


def test_a_model_that_never_carries_reasoning_state_reports_zero_not_a_rate(tmp_path):
    path = tmp_path / "r.db"
    with Results(path) as results:
        run_id = seed_run(results)
        _episode(results, run_id, "m-a", "completed", [_agent(0, False)])
    with RunMetrics(path, run_id) as m:
        trips = m.reasoning_round_trips("m-a")
    assert (trips.carried, trips.sent_back, trips.accepted.n, trips.accepted.value) == (
        0, 0, 0, None)
