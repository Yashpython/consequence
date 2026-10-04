"""Suite-level properties of the task catalog, as opposed to any one task.

Run with -s to see the printed difficulty distribution:
    pytest tests/test_task_suite.py -s
"""

from __future__ import annotations

import re
from collections import Counter

import pytest
from task_scenarios import EXPECTED_TASKS, REFUSED, SCENARIOS

from consequence.collateral import check_collateral
from consequence.diff import diff
from consequence.task import DIFFICULTIES, MODIFIABLE_TABLES
from consequence.tasks import TASKS

EXPECTED_COUNT = 25
EXPECTED_DISTRIBUTION = {"easy": 3, "medium": 7, "hard": 9, "trap": 6}

# Anything whose meaning depends on when the instruction is read. Task dates
# must be literals ("before 2025-07-15"), so an episode's correct answer is
# fixed by the seed, not by the clock (standing rule 2).
TEMPORAL = re.compile(
    r"\b("
    r"today|tonight|tomorrow|yesterday|now|nowadays|currently|"
    r"current (?:date|time|day|week|month|quarter|year)|"
    r"(?:this|last|next|past|coming|previous) (?:week|month|quarter|year|weekend)|"
    r"(?:last|past|next|coming|previous) \d+ (?:days?|weeks?|months?|years?)|"
    r"ago|recent|recently|lately|so far|to date|year-to-date|ytd"
    r")\b",
    re.IGNORECASE,
)


def _scenarios_for(task_id):
    return [s for s in SCENARIOS if s[0] == task_id]


def _apply(env, actions):
    for tool, args, *marker in actions:
        if marker != [REFUSED]:
            getattr(env, tool)(**args)


# -- size and uniqueness -------------------------------------------------------


def test_exactly_25_tasks_with_unique_ids():
    ids = [t.id for t in TASKS]
    assert len(TASKS) == EXPECTED_COUNT
    assert len(set(ids)) == EXPECTED_COUNT


def test_no_duplicate_instruction_text():
    normalized = [" ".join(t.instruction.lower().split()) for t in TASKS]
    duplicates = [text for text, n in Counter(normalized).items() if n > 1]
    assert not duplicates


def test_manifest_matches_catalog():
    assert {t.id: t.difficulty for t in TASKS} == EXPECTED_TASKS


# -- difficulty distribution ----------------------------------------------------


def test_difficulty_distribution_is_recorded_and_printed(record_property):
    distribution = Counter(t.difficulty for t in TASKS)
    record_property("difficulty_distribution", dict(distribution))
    print("\ntask difficulty distribution:")
    for difficulty in DIFFICULTIES:
        ids = [t.id[:3] for t in TASKS if t.difficulty == difficulty]
        print(f"  {difficulty:7s} {len(ids):2d}  {' '.join(ids)}")
    print(f"  {'total':7s} {sum(distribution.values()):2d}")
    assert dict(distribution) == EXPECTED_DISTRIBUTION


# -- declarations: allowed_tables and in_scope ---------------------------------------


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_every_task_declares_allowed_tables_and_in_scope(task, offline_env):
    assert isinstance(task.allowed_tables, frozenset)
    assert task.allowed_tables <= MODIFIABLE_TABLES
    if task.difficulty == "trap":
        assert task.allowed_tables == frozenset()  # every trap's right answer changes nothing
    else:
        assert task.allowed_tables, "a normal task must be allowed to change something"

    golden = next(s for s in _scenarios_for(task.id) if s[3] is None)
    _apply(offline_env, golden[2])
    before, after = offline_env.snapshots()
    state_diff = diff(before, after)
    result = task.verify(before, after, state_diff)

    assert isinstance(result.in_scope, frozenset)
    assert {table for table, _ in result.in_scope} <= task.allowed_tables
    if task.difficulty != "trap":
        assert result.in_scope, "a normal task's golden path must declare what it touched"
    assert check_collateral(task, state_diff, result).clean


# -- coverage: golden path + at least two negatives, per task ------------------------


def test_every_task_has_a_golden_path_and_two_negative_tests():
    gaps = {}
    for task in TASKS:
        scenarios = _scenarios_for(task.id)
        golden = sum(1 for s in scenarios if s[3] is None)
        negative = sum(1 for s in scenarios if s[3] is not None)
        if golden < 1 or negative < 2:
            gaps[task.id] = {"golden": golden, "negative": negative}
    assert not gaps, gaps


def test_no_scenario_targets_an_unknown_task():
    known = {t.id for t in TASKS}
    assert {s[0] for s in SCENARIOS} <= known


def test_every_negative_scenario_names_the_checks_it_must_fail():
    for task_id, name, _, expect_failed in SCENARIOS:
        assert expect_failed is None or expect_failed, f"{task_id}:{name} names no checks"


# -- determinism: no clock-relative language in instructions ---------------------------


def test_no_task_instruction_has_a_nondeterministic_temporal_reference():
    offenders = {
        t.id: TEMPORAL.findall(t.instruction) for t in TASKS if TEMPORAL.search(t.instruction)
    }
    assert not offenders, offenders


@pytest.mark.parametrize("text", [
    "Today's date is 2025-06-30.", "flag anything from right now", "in the last 30 days",
    "invoices from two weeks ago", "this month's invoices", "due tomorrow",
    "recently added suppliers", "invoices currently pending", "year-to-date totals",
])
def test_temporal_pattern_catches_relative_phrasing(text):
    assert TEMPORAL.search(text)


@pytest.mark.parametrize("text", [
    "dated more than 60 days before 2025-07-15", "out in the next payment run",
    "whose status is 'active'", "the second quarter of 2025", "dated after 2025-06-30",
])
def test_temporal_pattern_allows_literal_dates(text):
    assert not TEMPORAL.search(text)
