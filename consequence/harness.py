"""The agent harness: reset -> act -> snapshot -> record. Nothing else.

run_episode() drives exactly one (task, model, trial) episode: it resets the
environment to the seed state, sends the system prompt and task
instructions, alternates model turns with tool execution until the model
stops, an error occurs, or a hard limit is hit, snapshots the environment
again, and records everything (episode row, full transcript, state diff) to
the results store.

The harness NEVER grades. Grading reads what's recorded here as a separate,
later, offline stage -- see consequence/results.py's design note on
state_diffs. Keeping the two apart is what makes a grader bug free to fix
and re-score: the raw evidence this module writes down never has to be
regenerated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from consequence.diff import StateDiff, diff
from consequence.environment import SEED_PATH, reset, snapshot
from consequence.providers.base import Provider
from consequence.providers.registry import compute_cost
from consequence.results import Results
from consequence.tools import call_tool
from consequence.tools.schema import TOOLS

DEFAULT_MAX_TURNS = 20
DEFAULT_TIMEOUT_S = 180.0

# This system prompt is a single fixed constant, sent verbatim to every
# model on every episode. Changing its wording -- even to fix a typo -- is a
# new experimental condition: episodes recorded before and after the change
# are not comparable, because the agent under test literally read different
# instructions. See docs/mcp.md, which states the same rule for tool
# descriptions.
SYSTEM_PROMPT = (
    "You are an operations agent for a supplier-invoice back office. You have "
    "access to a fixed set of tools for reading and updating invoices, "
    "suppliers, approvals, and review flags. You cannot execute SQL or modify "
    "the database in any other way -- every action must go through a tool "
    "call. Use tools to gather whatever information you need before acting. "
    "When you have finished the task, reply with a final message and do not "
    "call any more tools."
)


@dataclass(frozen=True)
class Task:
    task_id: str
    instructions: str


@dataclass(frozen=True)
class EpisodeRecord:
    episode_id: int
    run_id: int
    status: str
    error: str | None
    turn_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    retry_count: int
    cost_usd: float | None
    digest_before: str
    digest_after: str
    diff: StateDiff


def begin_run(results: Results, *, notes: str | None = None, git_sha: str | None = None) -> int:
    """Start a run: reset the environment and store its starting state (and
    the seed it came from) once, in the run row.

    reset() is deterministic, so every episode of the run starts from exactly
    this state -- and the results store enforces that on every episode write.
    """
    reset()
    return results.start_run(
        snapshot(), SEED_PATH.read_text(encoding="utf-8"), notes=notes, git_sha=git_sha
    )


def run_episode(
    task: Task,
    provider: Provider,
    results: Results,
    trial_index: int,
    *,
    run_id: int | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_total_tokens: int | None = None,
) -> EpisodeRecord:
    """Run one episode end to end and record it. Never raises: a provider
    error is caught and recorded as status='error', not propagated.

    If run_id is omitted, a new run is started for this one episode --
    convenient for tests and one-off calls. A caller running a whole
    experiment (many tasks/models/trials as one invocation) should call
    begin_run() once and pass that run_id to every episode, per the
    "runs = one row per experiment invocation" design in results.py.

    The one thing that does raise is ResultsIntegrityError, if the episode's
    state doesn't match its run (e.g. reset() wasn't deterministic). It is
    checked before any row is written, so a failing episode leaves nothing
    behind.
    """
    if run_id is None:
        run_id = begin_run(results)

    reset()
    before = snapshot()

    started_at = datetime.now(UTC).isoformat()
    transcript: list[dict[str, Any]] = []

    def _record_turn(role: str, **fields: Any) -> None:
        transcript.append({"turn_index": len(transcript), "role": role, **fields})

    _record_turn("system", content=SYSTEM_PROMPT)
    _record_turn("user", content=task.instructions)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task.instructions},
    ]

    status: str | None = None
    error: str | None = None
    input_tokens = 0
    output_tokens = 0
    latency_ms = 0
    turns_completed = 0
    retries_total = 0
    served: set[str] = set()
    start_time = time.monotonic()

    try:
        for _ in range(max_turns):
            if time.monotonic() - start_time > timeout_s:
                status = "timeout"
                break

            turn = provider.run_turn(messages, TOOLS)
            turns_completed += 1
            input_tokens += turn.input_tokens
            output_tokens += turn.output_tokens
            latency_ms += turn.latency_ms
            retries_total += turn.retry_count
            if turn.served_model:
                served.add(turn.served_model)

            _record_turn("assistant", content=turn.text, raw_response=turn.raw_response)
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in turn.tool_calls
                    ],
                    # Provider-native content the adapter sends back verbatim
                    # (thinking blocks, thought signatures); opaque here.
                    "replay": turn.replay,
                }
            )

            if max_total_tokens is not None and input_tokens + output_tokens > max_total_tokens:
                status = "max_turns"
                error = (
                    f"max_total_tokens exceeded: {input_tokens + output_tokens} "
                    f"> {max_total_tokens}"
                )
                break

            if not turn.tool_calls:
                status = "completed"
                break

            for call in turn.tool_calls:
                result = call_tool(call.name, call.arguments)
                _record_turn(
                    "tool", tool_name=call.name, tool_args=call.arguments, tool_result=result
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result}
                )
        else:
            status = "max_turns"
    except Exception as exc:  # noqa: BLE001 -- a provider error is data, not a crash
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    assert status is not None

    after = snapshot()
    state_diff = diff(before, after)
    finished_at = datetime.now(UTC).isoformat()
    # None (rather than a guess) when provider.model_id isn't in models.yaml --
    # cost_usd is computed from pinned prices, never estimated.
    cost_usd = compute_cost(provider.model_id, input_tokens, output_tokens)
    diff_json = state_diff.to_json()  # every table, audit_log included

    # Verify before writing anything, so an integrity failure can't leave an
    # episode row behind without its state.
    results.verify_state(run_id, diff_json, before.digest, after.digest, after)
    episode_id = results.record_episode(
        run_id=run_id,
        task_id=task.task_id,
        model_id=provider.model_id,
        trial_index=trial_index,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=int(latency_ms),
        turn_count=turns_completed,
        retry_count=retries_total,
        served_model=",".join(sorted(served)) or None,
    )
    results.record_transcript(episode_id, transcript)
    results.record_state_diff(
        episode_id,
        diff_json=diff_json,
        digest_before=before.digest,
        digest_after=after.digest,
        after_snapshot=after,
    )

    return EpisodeRecord(
        episode_id=episode_id,
        run_id=run_id,
        status=status,
        error=error,
        turn_count=turns_completed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=int(latency_ms),
        retry_count=retries_total,
        cost_usd=cost_usd,
        digest_before=before.digest,
        digest_after=after.digest,
        diff=state_diff,
    )
