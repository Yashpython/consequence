"""Test helpers for building results-store runs without Postgres, and a
stubbed judge.

A run stores its initial snapshot and every episode stores its full after
snapshot, verified on write, so tests can't hand-write a state diff. These
build real ones from the parsed seed and the offline tool layer. No helper
here ever calls a real model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from consequence.environment import SEED_PATH
from consequence.harness import SYSTEM_PROMPT
from consequence.offline import OfflineEnvironment, parse_seed, snapshot_of
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.registry import JudgeSpec
from consequence.results import Results


def seed_run(results: Results, **kwargs) -> int:
    """Start a run whose initial state is the seed (what reset() produces)."""
    return results.start_run(
        snapshot_of(parse_seed()), SEED_PATH.read_text(encoding="utf-8"), **kwargs
    )


def record_episode_with_state(
    results: Results,
    run_id: int,
    actions: list[tuple[str, dict]] = (),
    *,
    task_id: str = "t1",
    model_id: str = "mock-1",
    trial_index: int = 0,
    status: str = "completed",
    instruction: str = "Do the task.",
    **episode_fields,
) -> int:
    """Record an episode whose stored state is what `actions` really produce,
    with the transcript the harness would have written for them."""
    from consequence.diff import diff

    env = OfflineEnvironment()
    turns = [{"turn_index": 0, "role": "system", "content": SYSTEM_PROMPT},
             {"turn_index": 1, "role": "user", "content": instruction}]
    for tool, args in actions:
        result = env.call_tool(tool, args)
        assert result["ok"], (tool, args)
        turns.append({"turn_index": len(turns), "role": "assistant", "stop_reason": "end_turn", "output_capped": False, "reasoning_state": False, "content": None})
        turns.append({"turn_index": len(turns), "role": "tool", "tool_name": tool,
                      "tool_args": args, "tool_result": result})
    turns.append({"turn_index": len(turns), "role": "assistant", "stop_reason": "end_turn", "output_capped": False, "reasoning_state": False, "content": "Done."})
    before, after = env.snapshots()
    state_diff = diff(before, after)
    episode_id = results.record_episode(
        run_id=run_id, task_id=task_id, model_id=model_id, trial_index=trial_index,
        status=status, **episode_fields,
    )
    results.record_transcript(episode_id, turns)
    results.record_state_diff(
        episode_id, diff_json=state_diff.to_json(), digest_before=before.digest,
        digest_after=after.digest, after_snapshot=after,
    )
    return episode_id


# -- a stubbed judge ---------------------------------------------------------------------


TEST_JUDGE = JudgeSpec(id="judge-test", provider="stub", api_model="stub-1",
                       input_price_per_million=2.0, output_price_per_million=8.0,
                       context_window_tokens=200_000, primary=True)
SECOND_JUDGE = JudgeSpec(id="judge-other-family", provider="stub", api_model="stub-2",
                         input_price_per_million=1.0, output_price_per_million=4.0,
                         context_window_tokens=200_000)
TEST_JUDGES = {TEST_JUDGE.id: TEST_JUDGE, SECOND_JUDGE.id: SECOND_JUDGE}
# A judge that rejects a temperature, like Claude Opus 5.5: votes at the
# provider's default sampling, never sent a temperature.
DEFAULT_SAMPLING_JUDGE = JudgeSpec(id="judge-no-temperature", provider="stub",
                                   api_model="stub-3", input_price_per_million=4.0,
                                   output_price_per_million=20.0,
                                   context_window_tokens=200_000, primary=True,
                                   sampling="provider_default")
DEFAULT_SAMPLING_JUDGES = {DEFAULT_SAMPLING_JUDGE.id: DEFAULT_SAMPLING_JUDGE,
                           SECOND_JUDGE.id: SECOND_JUDGE}


def verdict(passed: bool, behaviour: str, confidence: float = 0.9,
            reasoning: str = "evidence") -> dict[str, Any]:
    return {"passed": passed, "behaviour": behaviour, "confidence": confidence,
            "reasoning": reasoning}


@dataclass
class StubJudge:
    """A Provider standing in for the judge model. `decide(user_message,
    vote_index)` returns the verdict dict to submit via record_verdict, or a
    TurnResult to return as-is (e.g. a malformed reply). Every request is
    kept in `requests`, so tests can inspect exactly what the judge saw."""

    model_id: str
    decide: Callable[[str, int], dict[str, Any] | TurnResult]
    temperature: float | None = None
    requests: list[tuple[list[dict[str, Any]], Sequence[Any]]] = field(default_factory=list)

    def run_turn(self, messages, tools) -> TurnResult:
        self.requests.append((messages, tools))
        user = next(m["content"] for m in messages if m["role"] == "user")
        decision = self.decide(user, len(self.requests) - 1)
        if isinstance(decision, TurnResult):
            return decision
        return TurnResult(text=None, tool_calls=(ToolCall("v", "record_verdict", decision),),
                          input_tokens=1000, output_tokens=100)


def stub_judge_factory(decide, made: list | None = None):
    """A judge_provider_factory for main(): builds StubJudges, keeping each."""
    made = [] if made is None else made

    def factory(model_id, registry=None, temperature=None):
        judge = StubJudge(model_id=model_id, decide=decide, temperature=temperature)
        made.append(judge)
        return judge

    return factory
