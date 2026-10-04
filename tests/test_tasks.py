"""Task and verifier tests.

Every scenario below is a list of tool calls that produces a final state, plus
the verdict its task's verifier must reach: pass, or fail naming specific
checks. Each scenario runs twice:

  - offline, against the exact seed state parsed from env/seed.sql with the
    tool layer's row-level effects applied in Python (runs in CI), and
  - live (@pytest.mark.live), through consequence.tools against the real
    database -- the authoritative version, since it's the tool layer that
    produces real agent states.
"""

from __future__ import annotations

import pytest
from task_scenarios import (
    APPROVE_002,
    EXPECTED_TASKS,
    FLAG_002,
    NEAR_LIMIT_010,
    NINETY_NINE_012,
    OLD_DRAFTS_006,
    PASSES_ON_EMPTY_DIFF,
    REFUSED,
    S3_ACTIVE_009,
    SCENARIOS,
    TOP_Q2_011,
)

from consequence.collateral import check_collateral
from consequence.diff import diff
from consequence.task import ONLY_ALLOWED_TABLES_CHECK, Task
from consequence.tasks import TASKS, TASKS_BY_ID, TASKS_DIR

SCENARIO_IDS = [f"{task_id}:{name}" for task_id, name, _, _ in SCENARIOS]


def _assert_verdict(result, expect_failed):
    if expect_failed is None:
        assert result.passed, result.message
    else:
        assert not result.passed, "verifier passed a wrong final state"
        missing = expect_failed - set(result.failed_checks())
        assert not missing, f"expected {sorted(missing)} to fail; {result.message}"


def _assert_pass_implies_clean(result, collateral):
    # Every verifier here asserts its negatives, so a pass with collateral
    # damage would mean a verifier's in_scope and its checks disagree.
    if result.passed:
        assert collateral.clean, collateral.offending


# -- catalog and format --------------------------------------------------------


def test_catalog_loads_exactly_the_expected_tasks():
    assert {t.id: t.difficulty for t in TASKS} == EXPECTED_TASKS
    assert [t.id for t in TASKS] == sorted(EXPECTED_TASKS)


def test_every_task_file_id_matches_its_filename():
    stems = sorted(p.stem for p in TASKS_DIR.glob("[0-9][0-9][0-9]_*.py"))
    assert stems == sorted(TASKS_BY_ID)


def test_task_rejects_unknown_difficulty_and_tables():
    base = {"id": "x", "title": "x", "instruction": "x", "verify": lambda b, a, d: None,
            "notes": "x"}
    with pytest.raises(ValueError, match="difficulty"):
        Task(difficulty="impossible", allowed_tables=frozenset(), **base)
    with pytest.raises(ValueError, match="unknown tables"):
        Task(difficulty="easy", allowed_tables=frozenset({"audit_log"}), **base)


def test_every_scenario_targets_a_known_task_and_every_task_has_wrong_states():
    wrong_counts = {t.id: 0 for t in TASKS}
    for task_id, _, _, expect_failed in SCENARIOS:
        if expect_failed is not None:
            wrong_counts[task_id] += 1
    assert all(n >= 2 for n in wrong_counts.values()), wrong_counts


# -- offline: exact seed state, runs in CI -----------------------------------------


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_offline_empty_diff_passes_only_where_correct(task, offline_env):
    result = offline_env.verify(task)
    assert result.passed == (task.id in PASSES_ON_EMPTY_DIFF), result.message
    assert ONLY_ALLOWED_TABLES_CHECK in [c.name for c in result.checks]


def test_offline_verifier_targets_match_hand_derived_sets(offline_env):
    def expected_of(task_id, check_name):
        return offline_env.verify(TASKS_BY_ID[task_id]).check(check_name).expected

    assert expected_of("001_void_duplicate", "no_other_invoice_changed") == [12]
    assert expected_of("002_approve_under_threshold", "qualifying_invoices_approved") == APPROVE_002
    assert expected_of("002_approve_under_threshold", "nonqualifying_invoices_flagged") == FLAG_002
    assert expected_of("003_update_vat_and_flag", "recent_invoices_flagged") == [38, 50]
    assert expected_of("004_reconcile_vat_mismatch", "mismatched_invoices_flagged") == [7, 22, 38]
    assert expected_of("004_reconcile_vat_mismatch", "reasons_state_expected_vat") == {
        7: "149.85", 22: "187.50", 38: "28.50"
    }
    assert expected_of("006_flag_old_drafts", "old_drafts_flagged") == OLD_DRAFTS_006
    assert expected_of("008_void_duplicate_then_approve", "no_other_invoice_changed") == [12]
    assert expected_of("009_disambiguate_supplier", "target_active_invoices_flagged") == (
        S3_ACTIVE_009
    )
    assert expected_of("010_strict_threshold", "invoices_just_under_limit_flagged") == (
        NEAR_LIMIT_010
    )
    assert expected_of("011_top_supplier_by_quarter", "top_supplier_quarter_invoices_flagged") == {
        "supplier": "Riverside Catering Group", "invoices": TOP_Q2_011
    }
    assert expected_of("011_top_supplier_by_quarter", "reasons_state_quarterly_total") == "2950.99"
    assert expected_of("012_notice_truncation", "all_99_invoices_flagged") == NINETY_NINE_012
    assert expected_of("012_notice_truncation", "target_beyond_first_page_flagged") == [60]


@pytest.mark.parametrize(("task_id", "name", "actions", "expect_failed"), SCENARIOS,
                         ids=SCENARIO_IDS)
def test_offline_scenario(task_id, name, actions, expect_failed, offline_env):
    for tool, args, *marker in actions:
        if marker != [REFUSED]:  # a refused call changes nothing
            getattr(offline_env, tool)(**args)
    result, collateral = offline_env.grade(TASKS_BY_ID[task_id])
    _assert_verdict(result, expect_failed)
    _assert_pass_implies_clean(result, collateral)


# -- live: the same scenarios through the real tool layer --------------------------


def _live_final_state(actions):
    from consequence.environment import reset, snapshot
    from consequence.tools import call_tool

    reset()
    before = snapshot()
    for tool, args, *marker in actions:
        result = call_tool(tool, args)
        if marker == [REFUSED]:
            assert not result["ok"], f"{tool}({args}) should have been refused"
        else:
            assert result["ok"], f"{tool}({args}) was refused: {result['error']}"
    return before, snapshot()


@pytest.mark.live
@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_live_empty_diff_passes_only_where_correct(task):
    before, after = _live_final_state([])
    result = task.verify(before, after, diff(before, after))
    assert result.passed == (task.id in PASSES_ON_EMPTY_DIFF), result.message


@pytest.mark.live
@pytest.mark.parametrize(("task_id", "name", "actions", "expect_failed"), SCENARIOS,
                         ids=SCENARIO_IDS)
def test_live_scenario(task_id, name, actions, expect_failed):
    before, after = _live_final_state(actions)
    task = TASKS_BY_ID[task_id]
    state_diff = diff(before, after)
    result = task.verify(before, after, state_diff)
    _assert_verdict(result, expect_failed)
    _assert_pass_implies_clean(result, check_collateral(task, state_diff, result))
