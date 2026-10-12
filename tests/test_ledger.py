"""docs/resume-evidence.md must agree with every source it cites, and the
checker that enforces that must actually catch disagreements."""

from __future__ import annotations

from consequence.ledger import FACTS, LEDGER_PATH, ROOT, check

FAKE = {"tasks.count": lambda: "25", "tasks.trap": lambda: "6"}


def test_the_ledger_agrees_with_every_source_it_cites():
    problems = check((ROOT / LEDGER_PATH).read_text(encoding="utf-8"))
    assert problems == []


def test_the_ledger_cites_the_generated_blocks_not_copies():
    """The amendment's rule: numbers that live in another document are cited
    from its marked block, so these keys must actually be used."""
    text = (ROOT / LEDGER_PATH).read_text(encoding="utf-8")
    for key in ("adversarial.attempted", "adversarial.structural_limit",
                "planted.substring_supplier_filter.live", "tests.offline", "tests.live"):
        assert f"<!-- fact:{key} -->" in text, key


def test_every_fact_resolves_against_the_current_repo():
    for key, get in FACTS.items():
        assert get().strip(), key


def test_a_disagreement_is_reported():
    assert check("| Tasks | 24 <!-- fact:tasks.count --> | MEASURED | x |", FAKE) == [
        "tasks.count: ledger says '24', source says '25'"]


def test_an_unknown_fact_is_reported():
    assert check("| X | 3 <!-- fact:nope --> | MEASURED | x |", FAKE) == [
        "unknown fact 'nope' (cited with value '3')"]


def test_an_unreadable_source_is_a_reported_problem_not_a_crash():
    def missing():
        raise KeyError("planted-bugs block has no row 'x'")

    [problem] = check("| X | 3 <!-- fact:gone --> | MEASURED | x |", {"gone": missing})
    assert problem.startswith("gone: could not read its source (KeyError")


def test_a_measured_row_must_cite_a_fact():
    [problem] = check("| Tasks | 25 | MEASURED | tasks/ |", FAKE)
    assert problem.startswith("MEASURED row cites no fact")


def test_a_not_yet_measured_row_carries_no_number():
    [problem] = check("| Episodes | about 600 | NOT YET MEASURED | run |", FAKE)
    assert problem.startswith("NOT YET MEASURED row has a number")
    assert check("| Episodes | NOT YET MEASURED | NOT YET MEASURED | run |", FAKE) == []


def test_em_dashes_are_rejected():
    assert check("A claim — with a dash.", FAKE) == ["the ledger contains an em-dash"]


def test_ready_bullets_may_only_contain_cited_numbers():
    ok = "## 8. Bullets\n- **READY** Built 25 <!-- fact:tasks.count --> tasks.\n## 9. X\n"
    assert check(ok, FAKE) == []
    stray = "## 8. Bullets\n- **READY** Built 25 <!-- fact:tasks.count --> tasks in 3 weeks.\n"
    [problem] = check(stray + "## 9. X\n", FAKE)
    assert "uncited number(s) ['3']" in problem


def test_draft_bullets_may_hold_unmeasured_claims_but_unmarked_bullets_may_not():
    draft = "## 8. Bullets\n- **DRAFT** Cut errors by NOT YET MEASURED.\n## 9. X\n"
    assert check(draft, FAKE) == []
    [problem] = check("## 8. Bullets\n- Shipped it.\n## 9. X\n", FAKE)
    assert "neither READY nor DRAFT" in problem
