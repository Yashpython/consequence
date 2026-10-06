"""The state grader: runs each task's verifier and the collateral check over a
recorded run, entirely from stored data.

What it reads: the results DB (episodes, state_diffs) and env/seed.sql. What it
never touches: the environment Postgres, and any model API. That's the point
of storing raw state (see results.py's note on state_diffs). A grader bug is
fixed and re-scored offline, at no cost, without replaying anything.

Reconstruction, per episode:
  before = the seed, parsed from env/seed.sql (consequence.offline). Its digest
           must equal the episode's recorded digest_before, which proves the
           episode ran against this seed and not an older one.
  after  = before with the stored diff applied. Its digest must equal the
           recorded digest_after, which proves the reconstruction is exactly
           the state the episode left.
If either digest disagrees -- or a diff is missing, or a task id isn't in the
catalog -- grading fails closed: GradingIntegrityError, raised before any row
is written. A grader that guessed would be worse than one that stops.

Episodes whose status isn't 'completed' (error, timeout, max_turns) are graded,
never skipped: skipping them would silently inflate pass rates. Their
state_verifier row is passed=False with the status and error recorded, and
the verdict the verifier would have given is kept alongside for analysis. The
collateral check runs on them like any other episode, because damage done
before a crash is still damage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from consequence.collateral import COLLATERAL_GRADER, check_collateral
from consequence.diff import diff
from consequence.environment import Snapshot
from consequence.offline import parse_seed, snapshot_of
from consequence.results import Results
from consequence.task import Task, VerifyResult

STATE_GRADER = "state_verifier"
COMPLETED = "completed"


class GradingIntegrityError(Exception):
    """Stored data can't be turned back into the exact states an episode saw."""


@dataclass(frozen=True)
class EpisodeGrade:
    episode_id: int
    task_id: str
    category: str  # 'normal' | 'trap' -- pass rates are never blended across these
    status: str
    passed: bool
    verifier_passed: bool  # the verifier's own verdict, before the status rule
    reason: str
    failed_checks: tuple[str, ...]
    collateral_clean: bool
    collateral_severity: str | None


@dataclass(frozen=True)
class GradingSummary:
    run_id: int
    graded: tuple[EpisodeGrade, ...] = field(default_factory=tuple)
    already_graded: tuple[int, ...] = field(default_factory=tuple)  # skipped: regrade=False

    def pass_counts(self) -> dict[str, dict[str, int]]:
        """state_verifier {category: {"passed", "total"}} over the episodes
        graded in this call. Split by category, never blended."""
        out: dict[str, dict[str, int]] = {}
        for g in self.graded:
            bucket = out.setdefault(g.category, {"passed": 0, "total": 0})
            bucket["total"] += 1
            bucket["passed"] += int(g.passed)
        return out


# -- reconstruction ----------------------------------------------------------------


def seed_snapshot() -> Snapshot:
    """The environment's starting state, from env/seed.sql alone."""
    return snapshot_of(parse_seed())


def apply_stored_diff(before: Snapshot, diff_json: str) -> Snapshot:
    """The state an episode left: `before` with its stored diff applied."""
    stored = json.loads(diff_json)
    rows = {table: {r["id"]: dict(r) for r in rs} for table, rs in before.structure.items()}
    for table, pk, row in stored["added"]:
        if pk in rows[table]:
            raise GradingIntegrityError(f"stored diff adds {table} {pk}, which already exists")
        rows[table][pk] = dict(row)
    for table, pk, _ in stored["removed"]:
        if rows[table].pop(pk, None) is None:
            raise GradingIntegrityError(f"stored diff removes {table} {pk}, which doesn't exist")
    for table, pk, columns in stored["changed"]:
        target = rows[table].get(pk)
        if target is None:
            raise GradingIntegrityError(f"stored diff changes {table} {pk}, which doesn't exist")
        for column, (old, new) in columns.items():
            if target.get(column) != old:
                raise GradingIntegrityError(
                    f"stored diff says {table} {pk}.{column} was {old!r}, "
                    f"but the seed has {target.get(column)!r}"
                )
            target[column] = new
    return snapshot_of(
        {table: [by_id[pk] for pk in sorted(by_id)] for table, by_id in rows.items()}
    )


def reconstruct(stored: dict[str, Any], seed: Snapshot) -> tuple[Snapshot, Snapshot]:
    """(before, after) for one episode's state_diffs row, digest-verified."""
    if stored["digest_before"] != seed.digest:
        raise GradingIntegrityError(
            f"episode {stored['episode_id']} started from digest {stored['digest_before']}, "
            f"but env/seed.sql now hashes to {seed.digest} -- the seed changed since this "
            "run, so its starting state can't be reconstructed"
        )
    after = apply_stored_diff(seed, stored["diff_json"])
    if after.digest != stored["digest_after"]:
        raise GradingIntegrityError(
            f"episode {stored['episode_id']}: seed + stored diff hashes to {after.digest}, "
            f"not the recorded digest_after {stored['digest_after']}"
        )
    return seed, after


# -- grading -------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _detail(episode: dict[str, Any], result: VerifyResult, passed: bool, reason: str) -> dict:
    return {
        "status": episode["status"],
        "error": episode["error"],
        "passed": passed,
        "verifier_passed": result.passed,
        "reason": reason,
        "message": result.message,
        "failed_checks": result.failed_checks(),
        "checks": [
            {"name": c.name, "passed": c.passed, "expected": _jsonable(c.expected),
             "actual": _jsonable(c.actual)}
            for c in result.checks
        ],
    }


def _prepare(results: Results, episode: dict[str, Any], seed: Snapshot,
             tasks_by_id: dict[str, Task]):
    """Everything needed to grade one episode, or GradingIntegrityError."""
    task = tasks_by_id.get(episode["task_id"])
    if task is None:
        raise GradingIntegrityError(
            f"episode {episode['id']}: task {episode['task_id']!r} is not in the catalog"
        )
    stored = results.state_diff_for_episode(episode["id"])
    if stored is None:
        raise GradingIntegrityError(f"episode {episode['id']} has no stored state diff")
    before, after = reconstruct(stored, seed)
    state_diff = diff(before, after)
    result = task.verify(before, after, state_diff)
    return task, state_diff, result


def grade_run(run_id: int, results: Results, regrade: bool = False) -> GradingSummary:
    """Grade every episode of a run with state_verifier and collateral.

    Reads only the results DB and env/seed.sql. With regrade=False, an episode
    that already has both grading rows is skipped. With regrade=True every
    episode is re-graded, and its rows are replaced (record_grading upserts
    per (episode_id, grader)), never duplicated.
    """
    from consequence.tasks import TASKS_BY_ID

    if results.get_run(run_id) is None:
        raise GradingIntegrityError(f"no run {run_id}")
    seed = seed_snapshot()

    todo, skipped = [], []
    for episode in results.episodes_for_run(run_id):
        graders_done = {g["grader"] for g in results.gradings_for_episode(episode["id"])}
        if not regrade and {STATE_GRADER, COLLATERAL_GRADER} <= graders_done:
            skipped.append(episode["id"])
        else:
            todo.append(episode)

    # Reconstruct and verify everything first, so an integrity failure in any
    # episode stops the run before a single row is written.
    prepared = [(episode, *_prepare(results, episode, seed, TASKS_BY_ID)) for episode in todo]

    graded = []
    for episode, task, state_diff, result in prepared:
        collateral = check_collateral(task, state_diff, result)
        completed = episode["status"] == COMPLETED
        passed = completed and result.passed
        if not completed:
            reason = (f"episode ended with status {episode['status']!r}"
                      + (f": {episode['error']}" if episode["error"] else "")
                      + " -- graded as failed regardless of final state")
        else:
            reason = result.message

        results.record_grading(episode["id"], STATE_GRADER, passed=passed,
                               detail_json=_detail(episode, result, passed, reason))
        results.record_grading(episode["id"], COLLATERAL_GRADER, passed=collateral.clean,
                               detail_json=collateral.to_detail())
        graded.append(EpisodeGrade(
            episode_id=episode["id"],
            task_id=task.id,
            category="trap" if task.difficulty == "trap" else "normal",
            status=episode["status"],
            passed=passed,
            verifier_passed=result.passed,
            reason=reason,
            failed_checks=tuple(result.failed_checks()),
            collateral_clean=collateral.clean,
            collateral_severity=collateral.severity,
        ))

    return GradingSummary(run_id=run_id, graded=tuple(graded), already_graded=tuple(skipped))
