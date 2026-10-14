# Methodology

## Test validation

A green test suite only means something if it fails when the code is wrong.
To check that, the bugs this project most needs to rule out are planted back
into the source, one at a time, and both suites are run against each one:

- **offline:** `python -m pytest` with `DATABASE_URL` unset, exactly as CI's
  `check` job runs it;
- **live:** `python -m pytest -m live` against the docker-compose Postgres, as
  CI's `live` job runs it.

Each planted bug is an exact source edit defined in
`consequence/planted_bugs.py`. The harness applies it, runs both suites,
counts failing tests, and restores the file in a `finally` block. Two test
files are left out of these runs: `tests/test_planted_bugs.py`, which tests
the harness itself (its anchor check fails whenever its own bug is planted,
which would add one fake catch to every count), and `tests/test_ledger.py`,
which compares documents with each other. Neither tests the code a bug is
planted in.
`tests/test_planted_bugs.py` checks that every edit still applies to the
current source, so a refactor can't quietly turn a planted bug into a no-op.
The table below is written by the harness, never by hand. Regenerate it with:

```
make up
python -m consequence.planted_bugs --write-doc
```

<!-- planted-bugs:start -->
| Planted bug | What it does | Offline tests failing | Live tests failing |
|---|---|---|---|
| `reads_live_environment` | The grader takes `before` from the live environment instead of the run's stored initial snapshot. | 24 of 599 | 1 of 332 |
| `skips_errored_episodes` | The grader silently skips episodes that errored, timed out or ran out of turns. | 13 of 599 | 1 of 332 |
| `substring_supplier_filter` | query_invoices' supplier_name filter as of a39b99d: an unescaped substring ILIKE, so '%' matches every supplier. | 0 of 599 | 5 of 332 |

Measured on 2026-10-14, at commit `0be69bc` plus uncommitted changes (last commit 2026-10-12) by `python -m consequence.planted_bugs --write-doc`.
<!-- planted-bugs:end -->

### Reading the table

- **`reads_live_environment`** is caught almost entirely by the offline
  suite. The live suite resets the environment before every episode, so the
  live database usually *equals* the run's stored initial state, and a
  grader that wrongly reads it gets the right answer by coincidence. The live
  test that does catch it is
  `test_live_run_grades_offline_with_environment_unreachable`, which grades
  with the environment unreachable. So the guarantee that grading reads only
  `results.db` rests on the offline suite plus that one live test.
- **`skips_errored_episodes`** is caught by the grader tests whose recorded
  run deliberately includes an episode that crashed after making the right
  write, and one that ran out of turns. Dropping those episodes changes the
  pass counts and the set of graded episodes.
- **`substring_supplier_filter`** restores the `query_invoices` filter as it
  was at `a39b99d`. It is caught only by the live suite, because the tool
  layer's SQL runs only against Postgres. The offline unit tests in
  `tests/test_supplier_match.py` cover the matching logic (`pick_supplier`),
  which this bug bypasses rather than changes. See the incident entry in
  `docs/reward-hacking-log.md`.

### "Environment DB stopped" is fault injection

`test_grading_works_with_the_environment_db_unavailable` doesn't stop a
container. It simulates the outage: `DATABASE_URL` points at a port nothing
listens on (`127.0.0.1:1`), and `psycopg.connect`, `consequence.db.connect`,
the provider factory and the mock model's `run_turn` are all replaced with
functions that raise if touched. Grading must still complete.

The live variant, `test_live_run_grades_offline_with_environment_unreachable`,
covers the real case: it records a run against real Postgres (a real `reset()`
before every episode), then grades with `DATABASE_URL` pointed at the same
dead port. It doesn't stop the container either. What it adds over the
offline test is that the stored state came from a real database, not the
offline environment.

### Earlier measurements

When the state grader was first written (commit `a39b99d`), the same two
grader bugs were planted by hand. Those numbers aren't used here. That
grader rebuilt `before` from the seed rather than reading it from
`results.db`, so the bugs were planted in code that no longer exists, and
one count was cut off by `head` in the command that produced it. The table
above replaces them.
