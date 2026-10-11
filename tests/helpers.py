"""Test helpers for building results-store runs without Postgres.

A run now stores its initial snapshot and every episode stores its full after
snapshot, verified on write, so tests can't hand-write a state diff any more.
These build real ones from the parsed seed and the offline tool layer.
"""

from __future__ import annotations

from consequence.environment import SEED_PATH
from consequence.offline import OfflineEnvironment, parse_seed, snapshot_of
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
    **episode_fields,
) -> int:
    """Record an episode whose stored state is what `actions` really produce."""
    from consequence.diff import diff

    env = OfflineEnvironment()
    for tool, args in actions:
        assert env.call_tool(tool, args)["ok"], (tool, args)
    before, after = env.snapshots()
    state_diff = diff(before, after)
    episode_id = results.record_episode(
        run_id=run_id, task_id=task_id, model_id=model_id, trial_index=trial_index,
        status=status, **episode_fields,
    )
    results.record_state_diff(
        episode_id, diff_json=state_diff.to_json(), digest_before=before.digest,
        digest_after=after.digest, after_snapshot=after,
    )
    return episode_id
