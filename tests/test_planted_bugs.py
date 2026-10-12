"""The planted-bug harness itself. Running the planted bugs means running the
whole suite several times, so it isn't part of CI; these tests keep the
harness honest without running it."""

from __future__ import annotations

import pytest

import consequence.planted_bugs as pb
from consequence.planted_bugs import (
    PLANTED_BUGS,
    PlantedBug,
    SuiteResult,
    parse_block,
    render_block,
)


@pytest.mark.parametrize("bug", PLANTED_BUGS, ids=[b.key for b in PLANTED_BUGS])
def test_every_anchor_matches_the_source_exactly_once(bug):
    """If a refactor moves the code a bug plants into, this fails, instead of
    the harness reporting a planted bug that was never actually planted."""
    source = (pb.ROOT / bug.path).read_text(encoding="utf-8")
    assert source.count(bug.anchor) == 1
    assert "PLANTED" in bug.replacement and bug.replacement != bug.anchor


def test_measure_restores_the_file_even_if_the_suite_run_crashes(tmp_path, monkeypatch):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(pb, "ROOT", tmp_path)
    seen = []

    def crashing_run(suite):
        seen.append(target.read_text(encoding="utf-8"))
        raise KeyboardInterrupt

    monkeypatch.setattr(pb, "run_suite", crashing_run)
    bug = PlantedBug("k", "d", "mod.py", "x = 1\n", "x = 2  # PLANTED\n")
    with pytest.raises(KeyboardInterrupt):
        pb.measure(bug, ("offline",))
    assert seen == ["x = 2  # PLANTED\n"]  # it really was planted while the suite ran
    assert target.read_text(encoding="utf-8") == "x = 1\n"  # and is gone afterwards


def test_measure_refuses_an_anchor_that_does_not_match(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("y = 1\n", encoding="utf-8")
    monkeypatch.setattr(pb, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="anchor"):
        pb.measure(PlantedBug("k", "d", "mod.py", "x = 1\n", "x = 2\n"), ("offline",))


def test_the_doc_block_round_trips():
    results = {
        bug.key: {"offline": SuiteResult(failed=("a", "b"), passed=8),
                  "live": SuiteResult(failed=(), passed=5)}
        for bug in PLANTED_BUGS
    }
    parsed = parse_block(render_block(results))
    assert parsed == {bug.key: {"offline": (2, 10), "live": (0, 5)} for bug in PLANTED_BUGS}


def test_the_methodology_doc_has_a_complete_block():
    text = (pb.ROOT / pb.DOC_PATH).read_text(encoding="utf-8")
    assert set(parse_block(text)) == {bug.key for bug in PLANTED_BUGS}
