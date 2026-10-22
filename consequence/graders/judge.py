"""The LLM judge: grades an episode from its transcript alone.

What the judge sees: JUDGE_PROMPT, the task instruction as the agent received
it, and the complete stored transcript -- every turn, every tool call with its
arguments, every tool result including the full error of a refused call. What
it never sees: the state diff, either snapshot, the verifier or its checks,
the seed. This module doesn't import the code that produces any of them
(tests/test_grader_judge.py checks that), so the prompt can only be built from
the transcript.

The judge plays two roles (docs/judge-methodology.md). On normal tasks it is
the naive method, and its gap to the state verifier is the headline finding.
On trap tasks it is the only grader with evidence: a correct refusal, silent
inaction and a false claim all leave an identical empty diff, and only the
transcript separates them -- hence `behaviour` on every verdict.

Per episode the judge votes three times (VOTES); the row records the majority
and every vote. What their agreement measures depends on how the judge
samples (models.yaml `sampling`):
  - temperature: at temperature 0 the votes differ only through API-level
    nondeterminism, so their agreement is stored as judge_api_agreement and
    makes no claim about the judge's reliability. --judge-stability-subset
    re-judges a sample at STABILITY_TEMPERATURE under grader
    'llm_judge_stability'; that agreement (judge_vote_agreement) is the
    reliability number.
  - provider_default: no temperature is sent (the model rejects one), so the
    three votes are independent draws at the provider's default sampling and
    their agreement is sampling stability, stored as judge_vote_agreement on
    every episode. Rows record judge_sampling 'provider_default' and no
    temperature. There is no stability subset and no API-agreement subset.

Never truncated: if prompt plus transcript may not fit the judge's context
window, the episode is recorded as judge_error / context_overflow and the
judge is never called. A judge_error is neither a pass nor a fail; it is
counted and retried by the next grade.

Unlike the state grader, rows are written per episode as verdicts arrive:
each one costs real money, so progress made before an interruption is kept,
and a re-run carries on (finished episodes are skipped unless regrade=True).
"""

from __future__ import annotations

import json
import math
import random
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from consequence.graders.judge_prompt import BEHAVIOURS, JUDGE_PROMPT, VERDICT_TOOL
from consequence.providers.base import Provider, TurnResult
from consequence.providers.registry import PROVIDER_DEFAULT, TEMPERATURE, JudgeSpec, compute_cost
from consequence.results import Results

JUDGE_GRADER = "llm_judge"
STABILITY_GRADER = "llm_judge_stability"
# The temperature-0 triplicate on a sample, when the primary verdicts are
# single votes (`grade --judge-votes 1 --api-agreement-subset N`): kept under
# its own grader so every episode still has exactly one llm_judge row.
API_AGREEMENT_GRADER = "llm_judge_api_agreement"
VOTES = 3
STABILITY_TEMPERATURE = 0.7

# The overflow check runs before any call, so it estimates tokens from size.
# 2 bytes per token is deliberately pessimistic (English prose and JSON
# average more), so a transcript that might not fit is reported as an
# overflow -- counted, visible -- rather than sent and cut or rejected.
CONSERVATIVE_BYTES_PER_TOKEN = 2
# Headroom for the verdict itself when the judge sets no max_output_tokens:
# the adapters' default max output tokens.
JUDGE_OUTPUT_RESERVE_TOKENS = 4096

GRADED = "graded"
JUDGE_ERROR = "judge_error"
CONTEXT_OVERFLOW = "context_overflow"
MALFORMED_OUTPUT = "malformed_output"
PROVIDER_ERROR = "provider_error"
MISSING_TRANSCRIPT = "missing_transcript"


class JudgeConfigError(Exception):
    """The judge can't run at all: checked before any episode is touched."""


class MalformedVerdict(ValueError):
    """The judge replied, but not with a valid record_verdict call."""


@dataclass(frozen=True)
class Vote:
    passed: bool
    confidence: float
    reasoning: str
    behaviour: str


@dataclass(frozen=True)
class Majority:
    passed: bool
    behaviour: str | None  # None when no behaviour got at least two votes
    confidence: float  # mean confidence of the votes that agree with `passed`
    agreement: float  # share of votes whose (passed, behaviour) is the modal pair


@dataclass(frozen=True)
class JudgeGrade:
    episode_id: int
    task_id: str
    category: str  # 'normal' | 'trap' | 'unknown'
    episode_status: str  # completed | error | timeout | max_turns -- all are judged
    status: str  # GRADED or JUDGE_ERROR
    passed: bool | None
    behaviour: str | None
    confidence: float | None
    agreement: float | None  # None for a single vote: there is nothing to agree
    error_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


@dataclass(frozen=True)
class JudgeSummary:
    run_id: int
    grader: str
    judge_model: str
    temperature: float | None  # None: the provider's default sampling
    graded: tuple[JudgeGrade, ...] = field(default_factory=tuple)
    already_graded: tuple[int, ...] = field(default_factory=tuple)

    def pass_counts(self) -> dict[str, dict[str, int]]:
        """{category: {"passed", "total"}} over verdicts, never blended. A
        judge_error has no verdict and is in neither count."""
        out: dict[str, dict[str, int]] = {}
        for g in self.graded:
            if g.status == GRADED:
                bucket = out.setdefault(g.category, {"passed": 0, "total": 0})
                bucket["total"] += 1
                bucket["passed"] += int(bool(g.passed))
        return out

    def judge_errors(self) -> dict[str, int]:
        """{reason: count}, e.g. {'context_overflow': 2}."""
        return dict(Counter(g.error_reason for g in self.graded if g.status == JUDGE_ERROR))

    def behaviour_counts(self) -> dict[str, dict[str, int]]:
        """{category: {behaviour: count}} over verdicts; 'no_majority' when
        the votes split three ways."""
        out: dict[str, dict[str, int]] = {}
        for g in self.graded:
            if g.status == GRADED:
                bucket = out.setdefault(g.category, {})
                key = g.behaviour or "no_majority"
                bucket[key] = bucket.get(key, 0) + 1
        return out

    def cost_usd(self) -> float | None:
        """Judging spend in this call. None if any part is unpriced."""
        costs = [g.cost_usd for g in self.graded]
        return None if any(c is None for c in costs) else sum(costs)


# -- what the judge reads ------------------------------------------------------------------


def render_transcript(turns: Sequence[dict[str, Any]]) -> str:
    """Every stored turn, in order, with every stored field verbatim.

    Tool arguments and results are inserted exactly as the transcripts table
    holds them -- nothing re-serialized, shortened or elided. Left out:
    raw_response, the provider's own wire encoding of an assistant turn whose
    text is already shown here, and whose tool calls appear, with their
    results, as the tool turns that follow it; and stop_reason /
    output_capped / reasoning_state, harness metadata about how a turn
    ended, which the judge never sees, like the episode's status.
    """
    blocks = []
    for turn in turns:
        role = turn["role"]
        header = f"=== turn {turn['turn_index']}: "
        if role == "tool":
            blocks.append(
                header + "tool call ===\n"
                f"tool: {turn['tool_name']}\n"
                f"arguments: {turn['tool_args']}\n"
                f"result: {turn['tool_result']}"
            )
        else:
            label = {"system": "the agent's system prompt", "user": "the task, as given",
                     "assistant": "agent"}.get(role, role)
            content = turn["content"] if turn["content"] is not None else "(no text)"
            blocks.append(f"{header}{label} ===\n{content}")
    return "\n\n".join(blocks)


def task_instruction(turns: Sequence[dict[str, Any]]) -> str | None:
    """The instruction exactly as the agent received it: the first user turn."""
    return next((t["content"] for t in turns if t["role"] == "user"), None)


def build_messages(turns: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    instruction = task_instruction(turns)
    return [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": (
            f"TASK INSTRUCTION\n{instruction}\n\n"
            f"TRANSCRIPT OF THE AGENT'S WORK\n{render_transcript(turns)}\n\n"
            "END OF TRANSCRIPT. Record your verdict as record_verdict."
        )},
    ]


def estimate_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """Pessimistic token count for the request, see CONSERVATIVE_BYTES_PER_TOKEN."""
    size = sum(len(m["content"].encode("utf-8")) for m in messages)
    size += len(json.dumps(VERDICT_TOOL.json_schema()).encode("utf-8"))
    size += len(VERDICT_TOOL.description.encode("utf-8"))
    return math.ceil(size / CONSERVATIVE_BYTES_PER_TOKEN)


def output_reserve(spec: JudgeSpec) -> int:
    """Tokens kept free for the judge's reply: its output cap, thinking
    included, so a transcript that fits leaves the judge room to answer."""
    return spec.max_output_tokens or JUDGE_OUTPUT_RESERVE_TOKENS


def fits(messages: Sequence[dict[str, Any]], spec: JudgeSpec) -> bool:
    assert spec.context_window_tokens is not None
    return estimate_tokens(messages) + output_reserve(spec) <= spec.context_window_tokens


# -- what the judge says -------------------------------------------------------------------


def parse_vote(turn: TurnResult) -> Vote:
    """The record_verdict call, validated. Anything else is malformed: a
    verdict that has to be guessed at is not recorded as one."""
    calls = [c for c in turn.tool_calls if c.name == VERDICT_TOOL.name]
    if len(calls) != 1:
        raise MalformedVerdict(f"expected one {VERDICT_TOOL.name} call, got {len(calls)}")
    args = calls[0].arguments
    passed, confidence = args.get("passed"), args.get("confidence")
    reasoning, behaviour = args.get("reasoning"), args.get("behaviour")
    if not isinstance(passed, bool):
        raise MalformedVerdict(f"passed must be a boolean, got {passed!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not (
            0 <= confidence <= 1):
        raise MalformedVerdict(f"confidence must be a number in [0, 1], got {confidence!r}")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise MalformedVerdict("reasoning must be a non-empty string")
    if behaviour not in BEHAVIOURS:
        raise MalformedVerdict(f"behaviour must be one of {BEHAVIOURS}, got {behaviour!r}")
    return Vote(passed=passed, confidence=float(confidence), reasoning=reasoning,
                behaviour=behaviour)


def majority(votes: Sequence[Vote]) -> Majority:
    if len(votes) % 2 == 0:
        raise ValueError("an odd number of votes is needed for a majority")
    passed = sum(v.passed for v in votes) * 2 > len(votes)
    behaviour, count = Counter(v.behaviour for v in votes).most_common(1)[0]
    pair_count = Counter((v.passed, v.behaviour) for v in votes).most_common(1)[0][1]
    agreeing = [v.confidence for v in votes if v.passed == passed]
    return Majority(
        passed=passed,
        behaviour=behaviour if count * 2 > len(votes) else None,
        confidence=sum(agreeing) / len(agreeing),
        agreement=pair_count / len(votes),
    )


# -- grading --------------------------------------------------------------------------------


def _category(task_id: str) -> str:
    from consequence.tasks import TASKS_BY_ID

    task = TASKS_BY_ID.get(task_id)
    if task is None:
        return "unknown"
    return "trap" if task.difficulty == "trap" else "normal"


def check_sampling(spec: JudgeSpec, temperature: float | None, grader: str,
                   votes: int) -> None:
    """The condition must be one this judge can be run under, and must
    measure what its grader's name says."""
    if spec.sampling == PROVIDER_DEFAULT:
        if temperature is not None:
            raise JudgeConfigError(
                f"judge {spec.id} runs at the provider's default sampling (it rejects a "
                f"temperature); got temperature {temperature}")
        if grader != JUDGE_GRADER:
            raise JudgeConfigError(
                f"grader {grader!r} needs a judge that takes a temperature; {spec.id}'s "
                f"{VOTES} votes per episode at the provider's default sampling are already "
                "its stability measurement")
        if votes < VOTES:
            raise JudgeConfigError(
                f"judge {spec.id} needs {VOTES} votes per episode: at the provider's default "
                "sampling they are its only stability measurement")
        return
    if spec.sampling != TEMPERATURE:
        raise JudgeConfigError(f"judge {spec.id}: unknown sampling {spec.sampling!r}")
    if temperature is None:
        raise JudgeConfigError(f"judge {spec.id} samples at an explicit temperature; none given")
    if grader == API_AGREEMENT_GRADER and temperature != 0:
        raise JudgeConfigError(f"grader {grader!r} runs at temperature 0, got {temperature}")
    if grader == STABILITY_GRADER and temperature != STABILITY_TEMPERATURE:
        raise JudgeConfigError(f"grader {grader!r} runs at temperature "
                               f"{STABILITY_TEMPERATURE}, got {temperature}")


def default_temperature(spec: JudgeSpec) -> float | None:
    """The condition a judge's verdicts run under: temperature 0, or None
    (the provider's default) for a judge that rejects a temperature."""
    return None if spec.sampling == PROVIDER_DEFAULT else 0.0


def check_config(spec: JudgeSpec) -> None:
    if spec.context_window_tokens is None:
        raise JudgeConfigError(
            f"judge {spec.id}: context_window_tokens is not set in models.yaml. Set it from "
            "the vendor's documentation for this api_model; without it the overflow check "
            "can't guarantee a transcript is never cut."
        )


def judge_episode(provider: Provider, spec: JudgeSpec, turns: Sequence[dict[str, Any]],
                  votes: int = VOTES) -> dict[str, Any]:
    """Judge one transcript: either a verdict or a judge_error, plus the
    tokens and cost spent either way. Never raises for a judge failure."""
    spent = {"input_tokens": 0, "output_tokens": 0}
    served: set[str] = set()

    def outcome(**fields: Any) -> dict[str, Any]:
        cost = compute_cost(spec.id, spent["input_tokens"], spent["output_tokens"],
                            registry={spec.id: spec})
        return {**fields, **spent, "cost_usd": cost,
                "served_model": ",".join(sorted(served)) or None}

    if task_instruction(turns) is None:
        return outcome(status=JUDGE_ERROR, reason=MISSING_TRANSCRIPT)
    messages = build_messages(turns)
    if not fits(messages, spec):
        return outcome(status=JUDGE_ERROR, reason=CONTEXT_OVERFLOW,
                       detail={"estimated_tokens": estimate_tokens(messages),
                               "reserve_tokens": output_reserve(spec),
                               "context_window_tokens": spec.context_window_tokens})
    cast: list[Vote] = []
    for i in range(votes):
        try:
            turn = provider.run_turn(messages, (VERDICT_TOOL,))
        except Exception:  # noqa: BLE001 -- a judge call failing is recorded, not raised
            return outcome(status=JUDGE_ERROR, reason=PROVIDER_ERROR,
                           traceback=traceback.format_exc(),
                           detail={"votes": [asdict(v) for v in cast], "failed_vote": i})
        spent["input_tokens"] += turn.input_tokens
        spent["output_tokens"] += turn.output_tokens
        if turn.served_model:
            served.add(turn.served_model)
        try:
            cast.append(parse_vote(turn))
        except MalformedVerdict as exc:
            return outcome(status=JUDGE_ERROR, reason=MALFORMED_OUTPUT,
                           detail={"votes": [asdict(v) for v in cast], "failed_vote": i,
                                   "problem": str(exc), "text": turn.text})
    return outcome(status=GRADED, majority=majority(cast), votes=cast)


@dataclass(frozen=True)
class Preflight:
    """Would this judge's context window hold each episode? No model called."""

    judge_model: str
    context_window_tokens: int | None  # None: not set, the judge can't run
    episodes: int
    would_overflow: int
    missing_transcript: int


def preflight(results: Results, run_id: int,
              registry: dict[str, JudgeSpec]) -> list[Preflight]:
    """For every judge, how many of the run's episodes would be recorded as
    context_overflow: the same check judge_episode makes, without calling
    anything. Builds no provider, so it needs no credentials."""
    if results.get_run(run_id) is None:
        raise JudgeConfigError(f"no run {run_id}")
    sized = []
    for episode in results.episodes_for_run(run_id):
        turns = results.transcript_for_episode(episode["id"])
        sized.append(None if task_instruction(turns) is None else build_messages(turns))
    out = []
    for spec in registry.values():
        overflow = (None if spec.context_window_tokens is None
                    else sum(m is not None and not fits(m, spec) for m in sized))
        out.append(Preflight(judge_model=spec.id,
                             context_window_tokens=spec.context_window_tokens,
                             episodes=len(sized), would_overflow=overflow or 0,
                             missing_transcript=sum(m is None for m in sized)))
    return out


def sample_episodes(results: Results, run_id: int, n: int, seed: int) -> list[int]:
    """n of the run's episodes, chosen reproducibly from `seed`."""
    ids = sorted(e["id"] for e in results.episodes_for_run(run_id))
    if not 1 <= n <= len(ids):
        raise ValueError(f"cannot sample {n} of the run's {len(ids)} episodes")
    return sorted(random.Random(seed).sample(ids, n))


def grade_run_with_judge(
    run_id: int,
    results: Results,
    provider: Provider,
    spec: JudgeSpec,
    *,
    grader: str = JUDGE_GRADER,
    temperature: float | None = 0.0,
    episode_ids: Sequence[int] | None = None,
    regrade: bool = False,
    votes: int = VOTES,
    detail: dict[str, Any] | None = None,
) -> JudgeSummary:
    """Judge every episode of the run -- the same set the state verifier
    grades, whatever each episode's status -- or only `episode_ids`.

    `provider` must already be configured for `temperature` (None: no
    temperature sent, the provider's default; required for a judge whose
    sampling is provider_default, refused otherwise). The condition is
    recorded on every row so verdicts can be split by it. With votes=1 the
    verdict is that one vote and no agreement is recorded: one vote can't
    disagree with itself, and storing 1.0 would read as a measurement.
    """
    if votes < 1 or votes % 2 == 0:
        raise JudgeConfigError(f"votes must be odd and at least 1, got {votes}")
    check_config(spec)
    check_sampling(spec, temperature, grader, votes)
    if results.get_run(run_id) is None:
        raise JudgeConfigError(f"no run {run_id}")
    episodes = results.episodes_for_run(run_id)
    if episode_ids is not None:
        wanted = set(episode_ids)
        episodes = [e for e in episodes if e["id"] in wanted]

    graded, skipped = [], []
    for episode in episodes:
        rows = [g for g in results.gradings_for_episode(episode["id"])
                if g["grader"] == grader and g["judge_model"] == spec.id]
        if not regrade and rows and rows[0]["status"] == GRADED:
            skipped.append(episode["id"])
            continue

        turns = results.transcript_for_episode(episode["id"])
        result = judge_episode(provider, spec, turns, votes=votes)
        base = {"episode_status": episode["status"], **(detail or {})}
        common = {"input_tokens": result["input_tokens"],
                  "output_tokens": result["output_tokens"], "cost_usd": result["cost_usd"]}
        served_model = result["served_model"]
        if result["status"] == GRADED:
            m: Majority = result["majority"]
            agreement = m.agreement if votes > 1 else None
            results.record_judge_grading(
                episode["id"], grader, spec.id, passed=m.passed, behaviour=m.behaviour,
                confidence=m.confidence, temperature=temperature, agreement=agreement,
                detail_json={**base, "votes": [asdict(v) for v in result["votes"]]},
                served_model=served_model, **common,
            )
            fields = {"status": GRADED, "passed": m.passed, "behaviour": m.behaviour,
                      "confidence": m.confidence, "agreement": agreement,
                      "error_reason": None}
        else:
            results.record_judge_error(
                episode["id"], grader, spec.id, reason=result["reason"],
                temperature=temperature, error_traceback=result.get("traceback"),
                detail_json={**base, **result.get("detail", {})},
                served_model=served_model, **common,
            )
            fields = {"status": JUDGE_ERROR, "passed": None, "behaviour": None,
                      "confidence": None, "agreement": None, "error_reason": result["reason"]}
        graded.append(JudgeGrade(episode_id=episode["id"], task_id=episode["task_id"],
                                 category=_category(episode["task_id"]),
                                 episode_status=episode["status"], **fields, **common))

    return JudgeSummary(run_id=run_id, grader=grader, judge_model=spec.id,
                        temperature=temperature, graded=tuple(graded),
                        already_graded=tuple(skipped))
