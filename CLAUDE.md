# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Standing rules for this repository

1. NEVER fabricate, estimate, or placeholder a measurement. Every number that
   appears in any README, report, chart, or docstring must be traceable to a row
   in the results database produced by an actual run. If a number is not yet
   measured, write TBD, not a guess.
2. Determinism is a hard requirement. The environment must restore to a
   byte-identical starting state before every episode. Any change that
   compromises this is a bug, not a tradeoff.
3. Verifiers assert on observable database state. They never grade the agent's
   text, its self-report, or the presence of a phrase in a transcript.
4. The agent's only write path to the database is the typed tool layer. The model
   never sees or emits SQL. This is enforced structurally, not by filtering.
5. Every new verifier must be accompanied by an adversarial note in
   docs/reward-hacking-log.md describing at least one way it could be cheated and
   whether that hole is closed.
6. Tests accompany logic in the same commit. CI must be green on every commit.
7. Keep provider-specific code behind the adapter interface. Adding a model is a
   config change, not a rewrite.
8. After every commit, update docs/resume-evidence.md. Add any new measured number,
   engineering decision, defect diagnosed, or artifact produced, each with its
   provenance. Flip NOT YET MEASURED rows to MEASURED when the measurement lands, and
   record the value with the command that regenerates it. Never add a number that is
   not traceable to a run, a test, or a count in the repository. Regenerate section 8
   whenever section 2 or 3 changes.

## What this project is

A benchmark that grades LLM agents on final database state rather than on their
own transcripts. Status: in development.

## Commands

```bash
pip install -e ".[dev]"          # install package + pytest, ruff, python-dotenv
cp .env.example .env             # then fill in values you need

ruff check .                     # lint (line length 100, default rule set)
pytest                           # run the DB-free test suite (default; see below)
pytest tests/test_diff.py::test_render_is_stable_and_sorted   # a single test

make up                          # start postgres:16 via docker-compose (host port 5433)
make down                        # stop it
make reset                       # drop/recreate schema + reload seed data
make test-live                   # pytest -m live, against the running postgres
make ledger-check                # docs/resume-evidence.md vs every source it cites
python -m consequence.planted_bugs --write-doc   # re-measure planted bugs (needs make up)
```

`docs/resume-evidence.md` is an aggregator, never a source of truth: each
number is followed by a hidden `fact:` comment naming a key in
`consequence/ledger.py`, which recomputes it from the repo or from the owning
document's marked block (`adversarial:summary` in the reward-hacking log,
`planted-bugs` in `docs/methodology.md`). Adding a test changes the test
counts the ledger cites, so `tests/test_ledger.py` fails until the ledger is
updated: that is intended.

Tests marked `@pytest.mark.live` require the docker-compose postgres and are
excluded by default (`addopts = "-m 'not live'"` in pyproject.toml), so plain
`pytest` and CI run with no database at all. `make test-live` (or `pytest -m
live`) is the only way to run them, and needs `make up` first.

CI (`.github/workflows/ci.yml`) runs two jobs on every push and PR: `check`
(`ruff check .` then `pytest`) and `live` (`pytest -m live` against a
postgres:16 service container). Every direct dependency in pyproject.toml is
pinned with `==`; bump a pin deliberately and run both suites when you do.

## Architecture

The core primitive this whole project rests on is **reset → act → snapshot →
diff**, split across three modules in `consequence/`:

- **`environment.py`** — `reset()` drops and recreates the `public` schema and
  reloads `env/seed.sql`, all in one transaction, so it's idempotent and every
  episode starts byte-identical (standing rule 2). `snapshot()` reads every
  table in `TABLES` order and returns a `Snapshot(structure, digest)`: a plain
  dict of table → rows plus a sha256 over it. NUMERIC columns come back from
  psycopg as `Decimal` and are run through `_normalize()` (`format(value.normalize(), "f")`)
  before hashing/comparison, because `Decimal("205.50") != Decimal("205.5")`
  even though the values are equal — without this, semantically identical
  databases would hash differently.
- **`diff.py`** — `diff(before, after) -> StateDiff` compares two `Snapshot`s
  table-by-table, row-by-row (keyed on the `id` column), column-by-column,
  applying the same numeric-normalization rule so trailing-zero differences
  never show up as changes. `StateDiff.touched()` is what verifiers and the
  collateral-damage check are expected to build on; it excludes the
  `audit_log` table by default (`include_audit=True` to include it) since an
  audit row is an expected side effect of any legitimate write, not evidence
  of what changed.
- **`db.py`** — thin psycopg3 wrapper: `connect()` is a context manager built
  from `DATABASE_URL` (via `config.py`), `run_sql_file()` executes a `.sql`
  file in one transaction.

`env/schema.sql` is the single source of truth for the DDL: every table uses
a plain integer primary key with **no sequence/identity/serial** (ids are
assigned explicitly by seed data, never generated), NUMERIC(12,2) for all
money (never float — see the comment in the file), and explicit `ON DELETE
RESTRICT` on every foreign key. There are no triggers; `audit_log` rows are
written by the typed tool layer (not yet built) rather than by the schema.

`env/seed.sql` is hand/script-generated, not random, and deliberately encodes
edge cases that downstream tasks/verifiers exercise: an exact duplicate
invoice pair (same supplier + number, different `created_at`), invoices whose
`vat` doesn't match the expected rate for their total, totals straddling the
500.00 threshold, a suspended supplier, suppliers with NULL/malformed `brn`,
and confusingly similar supplier names. `audit_log` starts empty.

**Tasks** live in `tasks/NNN_name.py` at the repo root, one per file, each
defining `TASK = consequence.task.Task(...)` whose `id` equals the file stem;
`consequence/tasks.py` loads them by path. A task's `verify(before, after,
diff)` never sees the transcript, computes its targets from `before`, asserts
both the intended change and the absence of other changes (every verifier
includes `only_allowed_tables()`), and returns named `Check`s plus
`in_scope` — the (table, pk) rows it may legitimately touch, including rows
inserted by the episode. `consequence/collateral.py` reports everything
touched outside `in_scope` as benign/destructive collateral, recorded as
`grader='collateral'`, independently of the task verdict. `build_result`
also appends `audit_trail_within_scope` to every verifier: every `audit_log`
row the episode wrote must point at an in-scope row, which catches
write-then-revert tampering that no diff can see.
`consequence/offline.py` is the seed state plus the tool layer's write
semantics in pure Python (it reuses `tools/impl.py`'s validation), used by
tests and by `consequence/adversaries.py`. Six adversarial agents per task
try to pass each verifier without doing the task. `tests/test_adversarial.py`
requires that only the ledger's `KNOWN_OPEN` ones pass. If an adversary
passes, fix the verifier, never the adversary. Regenerate the log's tables
with `python -m consequence.adversaries --write-log`.
`consequence/graders/state.py` (`consequence grade --grader state_verifier`)
grades a run from `results.db` alone -- no `env/seed.sql`, no environment
Postgres, no model. `harness.begin_run` stores the run's starting snapshot once
(`runs.initial_snapshot_json`; `reset()` is deterministic, so every episode
starts there), and each episode stores `state_diffs.after_snapshot_json`. Both
are the exact canonical JSON that was hashed (`environment.canonical_json`),
so verification is sha256(stored) == digest. `Results.record_state_diff`
rejects, before writing, any episode whose digests or `diff_json` don't match.
`results.SCHEMA_VERSION` is checked on open; on mismatch delete results.db
(no migrations). Snapshot cells must stay JSON-native (`environment._normalize`).
A `gradings` row has two notions of success: `passed` is authoritative and
the only thing any metric reads; `verifier_verdict_raw` (the verifier's
verdict before the completed-episodes-only rule) is analysis-only and never
aggregated into a pass rate. A verifier that raises is recorded as
`status='grading_error'` with `passed` NULL and the traceback stored: excluded
from pass counts, reported separately, retried by the next `grade`.
`grade --grader all` fails closed if any registered grader can't run.
`consequence/graders/judge.py` is the LLM judge (`grader='llm_judge'`): it
reads ONLY the stored transcript (it must never import diff, environment,
state, collateral, task or offline code; a test enforces this), votes three
times, and grades exactly the state verifier's episodes, whatever their
status, writing a `judge_error` row rather than skipping. It never truncates
(oversized input is `judge_error`/`context_overflow`). Judges live in
`models.yaml` under `judges:`; `JUDGE_PROMPT` is fixed apparatus like
`SYSTEM_PROMPT`. Judging cost goes in `gradings.judge_cost_usd`, never in
`episodes.cost_usd`. See `docs/judge-methodology.md`.
`consequence/metrics.py` (`RunMetrics`) computes every number that is reported
anywhere, from `results.db` opened read-only; it imports only the stdlib and
yaml (a test imports it in a fresh interpreter). Rates come per category
(normal/trap, never blended), with n, a Wilson interval and no-verdict counts;
judge metrics always take `judge_model`. `consequence/report.py` lays those out
per model and computes nothing: `tests/test_report.py` rejects any arithmetic
in its source. Add a new reported number to metrics.py, never to the report.
`db.connect()` pins the Postgres session time zone to UTC; every timestamp
column is `timestamptz` and every seed timestamp literal carries an explicit
offset (`tests/test_timestamps.py` enforces both).
Verifier
helpers in `consequence/task.py` accept both native and JSON-round-tripped
cell values. Adding a verifier requires an entry in
`docs/reward-hacking-log.md` (standing rule 5) and scenarios in
`tests/test_tasks.py`, which runs each scenario offline against the parsed
seed (`tests/conftest.py`, CI) and live through the tool layer.

Tests mirror this split: `tests/test_schema.py` parses `env/schema.sql`
statically (regex, no DB) to assert every expected table/column exists;
`tests/test_diff.py` is pure unit tests against hand-built `Snapshot` objects
(no DB); `tests/test_determinism.py` is the only suite that touches a real
database and is marked `live` throughout.
