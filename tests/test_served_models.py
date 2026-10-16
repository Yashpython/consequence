"""Served-version provenance and verbatim replay, with fake vendor responses:
no SDK is imported and no network is touched.

- Each adapter extracts the model that actually served a call from the raw
  response it stores (`served_model_from_raw`).
- Adapters whose models think in tool-use loops send their own reply back
  verbatim on the next call (Anthropic thinking blocks, Gemini thought
  signatures), because both vendors require it.
- The harness and the judge record the served model; metrics flag a
  configured model served by more than one version within a run; grade
  warns and records it; the report leads with it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from helpers import TEST_JUDGE, StubJudge, record_episode_with_state, seed_run, verdict

from consequence import experiment_log
from consequence.cli import main
from consequence.graders.judge import grade_run_with_judge
from consequence.harness import run_episode
from consequence.metrics import RunMetrics
from consequence.providers import anthropic as anthropic_adapter
from consequence.providers import google as google_adapter
from consequence.providers import openai as openai_adapter
from consequence.providers import openrouter as openrouter_adapter
from consequence.providers.base import ToolCall, TurnResult
from consequence.results import Results
from consequence.tasks import TASKS_BY_ID

# -- adapters: what served the call ------------------------------------------------------------


def test_each_adapter_reads_the_served_model_from_its_raw_response():
    assert anthropic_adapter.served_model_from_raw({"model": "claude-opus-5-5"}) == (
        "claude-opus-5-5")
    assert openai_adapter.served_model_from_raw({"model": "gpt-5.1-2025-11-13"}) == (
        "gpt-5.1-2025-11-13")
    assert google_adapter.served_model_from_raw({"model_version": "gemini-4-argon"}) == (
        "gemini-4-argon")
    # OpenRouter: the upstream host is part of what served the call.
    assert openrouter_adapter.served_model_from_raw(
        {"model": "deepseek/deepseek-v3.2", "provider": "HostA"}) == "deepseek/deepseek-v3.2@HostA"
    for read in (anthropic_adapter.served_model_from_raw, openai_adapter.served_model_from_raw,
                 google_adapter.served_model_from_raw, openrouter_adapter.served_model_from_raw):
        assert read({}) is None and read("not a dict") is None


def _anthropic_response():
    raw = {"model": "claude-opus-5-5", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "tool_use", "id": "tu_1", "name": "get_invoice", "input": {"invoice_id": 12}},
    ]}
    blocks = [SimpleNamespace(type="thinking"),
              SimpleNamespace(type="tool_use", id="tu_1", name="get_invoice",
                              input={"invoice_id": 12})]
    return SimpleNamespace(content=blocks, stop_reason="tool_use",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                           to_dict=lambda: raw), raw


def test_anthropic_replays_its_own_blocks_verbatim():
    """'pass thinking blocks back unmodified in tool-use loops'
    (platform.claude.com, Claude Opus 5.5 what's new)."""
    response, raw = _anthropic_response()
    turn = anthropic_adapter.parse_response(response, latency_ms=1, retry_count=0)
    assert turn.served_model == "claude-opus-5-5"
    assert turn.replay == raw["content"]  # thinking block and signature included
    _, out = anthropic_adapter.translate_messages([
        {"role": "user", "content": "Void 12."},
        {"role": "assistant", "content": turn.text,
         "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in turn.tool_calls], "replay": turn.replay},
    ])
    assert out[1] == {"role": "assistant", "content": raw["content"]}


def test_google_replays_its_content_and_counts_thinking_as_output():
    content = SimpleNamespace(parts=[SimpleNamespace(
        text=None, function_call=SimpleNamespace(name="get_invoice", args={"invoice_id": 12}))])
    raw = {"model_version": "gemini-4-argon", "candidates": []}
    seen = {}

    def model_dump(mode=None):
        seen["mode"] = mode
        return raw

    response = SimpleNamespace(
        candidates=[SimpleNamespace(content=content)], model_dump=model_dump,
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20,
                                       thoughts_token_count=300))
    turn = google_adapter.parse_response(response, latency_ms=1, retry_count=0)
    assert turn.output_tokens == 320  # thinking is billed as output
    assert seen["mode"] == "json"  # signatures (bytes) stored JSON-safe
    assert turn.served_model == "gemini-4-argon"
    assert turn.replay is content  # the SDK object itself: signature bytes untouched
    _, contents = google_adapter.translate_messages([
        {"role": "user", "content": "Void 12."},
        {"role": "assistant", "content": None, "tool_calls": [], "replay": turn.replay},
    ])
    assert contents[1] is content


def test_openai_and_openrouter_report_what_served():
    raw = {"model": "deepseek/deepseek-v3.2", "provider": "HostB"}
    message = SimpleNamespace(content="Done.", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                               to_dict=lambda: raw)
    turn = openai_adapter.parse_response(response, latency_ms=1, retry_count=0)
    assert turn.served_model == "deepseek/deepseek-v3.2" and turn.replay is None


# -- the harness and the judge record it ----------------------------------------------------------


class RecordingProvider:
    """Two turns: a tool call (served by A, with replay content), then done
    (served by B). Keeps every request so the replay can be checked."""

    model_id = "mock"

    def __init__(self, second_served="model-A"):
        self.requests = []
        self.second_served = second_served

    def run_turn(self, messages, tools):
        self.requests.append([dict(m) for m in messages])
        if len(self.requests) == 1:
            return TurnResult(text=None, tool_calls=(ToolCall("c0", "get_invoice",
                                                              {"invoice_id": 12}),),
                              served_model="model-A", replay={"native": "turn-1"})
        return TurnResult(text="Done.", served_model=self.second_served)


def test_the_harness_replays_and_records_the_served_model(offline_harness, tmp_path):
    provider = RecordingProvider(second_served="model-B")
    with Results(tmp_path / "r.db") as results:
        record = run_episode(TASKS_BY_ID["001_void_duplicate"], provider, results, 0)
        [episode] = results.episodes_for_run(record.run_id)
    assistant = [m for m in provider.requests[1] if m["role"] == "assistant"]
    assert assistant[0]["replay"] == {"native": "turn-1"}  # sent back as produced
    assert episode["served_model"] == "model-A,model-B"  # both, when they differ


def test_the_judge_records_the_model_that_served_its_votes(tmp_path):
    with Results(tmp_path / "r.db") as results:
        run_id = seed_run(results)
        ep = record_episode_with_state(results, run_id, task_id="001_void_duplicate")

        class Served(StubJudge):
            def run_turn(self, messages, tools):
                turn = super().run_turn(messages, tools)
                return TurnResult(text=None, tool_calls=turn.tool_calls, input_tokens=1,
                                  output_tokens=1, served_model="judge-served-1")

        grade_run_with_judge(run_id, results, Served(model_id=TEST_JUDGE.id,
                                                     decide=lambda p, v: verdict(True, "completed")),
                             TEST_JUDGE)
        [row] = results.gradings_for_episode(ep)
    assert row["served_model"] == "judge-served-1"


# -- drift: flagged, reported, logged, never blocking ---------------------------------------------


def _run_with_served(tmp_path, served):
    path = tmp_path / "r.db"
    with Results(path) as results:
        run_id = seed_run(results)
        for i, value in enumerate(served):
            ep = record_episode_with_state(results, run_id, task_id="001_void_duplicate",
                                           model_id="gpt-5.1", trial_index=i)
            results._conn.execute("UPDATE episodes SET served_model = ? WHERE id = ?",
                                  (value, ep))
            results._conn.commit()
            results.record_grading(ep, "state_verifier", passed=True)
    return path, run_id


def test_metrics_flag_a_model_served_by_more_than_one_version(tmp_path):
    path, run_id = _run_with_served(tmp_path, ["gpt-5.1-2025-11-13"] * 3 + ["gpt-5.1-x", None])
    with RunMetrics(path, run_id) as m:
        s = m.served_models("gpt-5.1")
        assert s.api_model == "gpt-5.1-2025-11-13"
        assert s.served == {"gpt-5.1-2025-11-13": 3, "gpt-5.1-x": 1} and s.unknown == 1
        assert s.drift is True and [d.configured for d in m.served_model_drift()] == ["gpt-5.1"]


def test_one_version_or_an_unknown_one_is_not_drift(tmp_path):
    path, run_id = _run_with_served(tmp_path, ["gpt-5.1-2025-11-13", "gpt-5.1-2025-11-13", None])
    with RunMetrics(path, run_id) as m:
        assert m.served_models("gpt-5.1").drift is False
        assert m.served_model_drift() == []


def test_disagreement_within_one_episode_is_drift(tmp_path):
    path, run_id = _run_with_served(tmp_path, ["model-A,model-B"])
    with RunMetrics(path, run_id) as m:
        assert m.served_models("gpt-5.1").drift is True


def test_grade_warns_records_in_the_log_and_still_grades(tmp_path, monkeypatch, capsys):
    path, run_id = _run_with_served(tmp_path, ["gpt-5.1-2025-11-13", "gpt-5.1-x"])
    log = tmp_path / "experiment-log.md"
    log.write_text(f"# Log\n\n{experiment_log.BLOCK_START}\n{experiment_log.EMPTY}\n"
                   f"{experiment_log.BLOCK_END}\n", encoding="utf-8")
    monkeypatch.setattr(experiment_log, "LOG_PATH", log)
    for _ in range(2):  # re-grading updates the entry, never duplicates it
        rc = main(["--results-db", str(path), "grade", "--run", str(run_id), "--grader",
                   "state_verifier", "--regrade"])
        assert rc == 0  # not blocked
    captured = capsys.readouterr()
    assert f"WARNING: SERVED MODEL CHANGED WITHIN RUN {run_id} for gpt-5.1" in captured.out
    assert "SERVED MODEL CHANGED" in captured.err
    text = log.read_text(encoding="utf-8")
    assert text.count(f"**Run {run_id}, `gpt-5.1`**") == 1
    assert experiment_log.EMPTY not in text
    with Results(path) as results:
        assert results._conn.execute("SELECT COUNT(*) FROM gradings").fetchone()[0] == 4


def test_the_report_leads_with_the_drift_and_lists_served_versions(tmp_path, capsys):
    path, run_id = _run_with_served(tmp_path, ["gpt-5.1-2025-11-13", "gpt-5.1-x"])
    main(["--results-db", str(path), "report", "--run", str(run_id)])
    md = capsys.readouterr().out
    warning = md.index("**WARNING: served model changed within this run for gpt-5.1**")
    assert warning < md.index("## Grading coverage")
    assert "served: `gpt-5.1-2025-11-13` x1, `gpt-5.1-x` x1" in md
    main(["--results-db", str(path), "report", "--run", str(run_id), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["models"]["gpt-5.1"]["served_models"]["drift"] is True


def test_no_drift_writes_nothing_to_the_log(tmp_path, monkeypatch):
    path, run_id = _run_with_served(tmp_path, ["gpt-5.1-2025-11-13"])
    log = tmp_path / "experiment-log.md"
    log.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(experiment_log, "LOG_PATH", log)
    main(["--results-db", str(path), "grade", "--run", str(run_id), "--grader", "state_verifier"])
    assert log.read_text(encoding="utf-8") == "unchanged"


def test_the_real_experiment_log_has_its_block():
    text = experiment_log.LOG_PATH.read_text(encoding="utf-8")
    assert text.count(experiment_log.BLOCK_START) == 1
    assert text.count(experiment_log.BLOCK_END) == 1


# -- the dry run says when a price changed -----------------------------------------------------------


def test_the_dry_run_prints_price_notes(tmp_path, capsys):
    rc = main(["--results-db", str(tmp_path / "r.db"), "run", "--all", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "price note, claude-opus-5.5: replaces claude-opus-5 ($5 / $25)" in out
    assert "price note, gemini-4-argon: doubles to $4 / $20 from December 1, 2026" in out


@pytest.mark.parametrize("model_id", ["claude-opus-5.5", "gemini-4-argon",
                                      "gemini-3.5-flash-lite"])
def test_new_models_are_registered(model_id):
    from consequence.providers.registry import load_model_registry
    assert model_id in load_model_registry()
