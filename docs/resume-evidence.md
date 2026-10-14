# Resume evidence ledger

Everything about Consequence that could end up on a resume, in a post, or in
an interview answer, with its provenance. It is an aggregator, never a second
source of truth: a number that lives in another document is cited from that
document's marked block, not restated. Every number is followed by a hidden
HTML comment, `fact:` plus a key, naming its source, and `python -m
consequence.ledger` (also `make ledger-check`, and `tests/test_ledger.py` in
CI) fails if any of them disagrees with its source. Standing rule 8 in
CLAUDE.md says when to update it.

Status words: **MEASURED** means the value is counted or computed from the
repository and checked by the ledger check. **NOT YET MEASURED** means no run
has produced it; the value cell then holds no number.

## 1. Fixed description

Use this wording everywhere, unchanged.

**One sentence:** Consequence is a benchmark that grades LLM agents on the
final state of the database they act on, not on their own account of what
they did.

**Tech stack:** Python, PostgreSQL (docker-compose), psycopg 3, SQLite results
store, Model Context Protocol server, pytest, ruff, GitHub Actions; model
adapters for Anthropic, OpenAI, Google and OpenRouter.

## 2. Scale and scope facts

| Fact | Value | Status | Source |
|---|---|---|---|
| Tasks | 25 <!-- fact:tasks.count --> | MEASURED | `tasks/NNN_*.py`; `len(consequence.tasks.TASKS)` |
| Easy tasks | 3 <!-- fact:tasks.easy --> | MEASURED | `Task.difficulty` over `TASKS` |
| Medium tasks | 7 <!-- fact:tasks.medium --> | MEASURED | `Task.difficulty` over `TASKS` |
| Hard tasks | 9 <!-- fact:tasks.hard --> | MEASURED | `Task.difficulty` over `TASKS` |
| Trap tasks (correct outcome is an unchanged database) | 6 <!-- fact:tasks.trap --> | MEASURED | `Task.difficulty == "trap"` |
| Normal (non-trap) tasks | 19 <!-- fact:tasks.normal --> | MEASURED | `Task.difficulty != "trap"` |
| Verifiers (one per task) | 25 <!-- fact:verifiers.count --> | MEASURED | `Task.verify` over `TASKS` |
| Tools exposed to the agent | 7 <!-- fact:tools.count --> | MEASURED | `consequence/tools/schema.py` `TOOLS` |
| Models in the registry | 5 <!-- fact:models.count --> | MEASURED | `consequence/models.yaml` |
| Judge models (separate `judges:` section) | 2 <!-- fact:judges.count --> | MEASURED | `consequence/models.yaml` `judges:` |
| Metrics computed from the results DB | 15 <!-- fact:metrics.count --> | MEASURED | `consequence/metrics.py` `METRICS` |
| Providers in the registry | 4 <!-- fact:providers.count --> | MEASURED | `provider` field in `consequence/models.yaml` |
| Trials per (task, model) in the experiment | NOT YET MEASURED | NOT YET MEASURED | Not fixed yet: `consequence run --trials` defaults to one. Set by the first real run. |
| Total episodes run against real models | NOT YET MEASURED | NOT YET MEASURED | `consequence status` after the first real run. No real run exists. |
| Offline tests | 617 <!-- fact:tests.offline --> | MEASURED | `pytest --collect-only -q -m "not live and not smoke"` |
| Live tests (real Postgres) | 332 <!-- fact:tests.live --> | MEASURED | `pytest --collect-only -q -m live` |
| Smoke tests (one billed API call each) | 4 <!-- fact:tests.smoke --> | MEASURED | `pytest --collect-only -q -m smoke`. Never run; see section 9. |
| Adversarial strategies | 6 <!-- fact:adversarial.strategies --> | MEASURED | `docs/reward-hacking-log.md`, block `adversarial:summary` |
| Adversarial attacks (strategies x tasks) | 150 <!-- fact:adversarial.attempted --> | MEASURED | `docs/reward-hacking-log.md`, block `adversarial:summary` |
| CI jobs | 2 <!-- fact:ci.jobs --> | MEASURED | `.github/workflows/ci.yml` (`check`, `live`) |
| Direct dependencies pinned exactly | 10 <!-- fact:dependencies.pinned_exactly --> of 10 <!-- fact:dependencies.count --> | MEASURED | `pyproject.toml` |

## 3. Measured findings

None of these exist yet: no episode has been run against a real model. Each
entry fixes the definition now, so the number can't be redefined after it is
seen. Every value will be computed by `consequence/metrics.py`, from the
run's `results.db` and nowhere else; `RunMetrics(db, run)` below opens it.
Every rate carries its n and a Wilson score 95% interval, normal and trap are
always separate, and judge metrics are always per judge. When `docs/findings.md` exists, every value here is cited from its
marked blocks, never copied.

### 3.1 Verification gap

- **Definition:** the primary judge's pass rate minus the state verifier's,
  in percentage points, per category. Positive means reading the transcript
  credits work the database shows wasn't done. Both rates are over the
  episodes both graders gave a verdict on, and the gap comes with its
  sensitivity bound (section 4).
- **Formula:** 100 x (judge passes / paired) minus 100 x (state passes /
  paired). Bound: the same over every episode, with each missing judge
  verdict counted as a pass and each missing state verdict as a fail (upper),
  and the reverse (lower).
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit. The judge exists
  (`consequence/graders/judge.py`) but has only run against stubs.
- **Regenerate:** `RunMetrics(db, run).verification_gap(model,
  judge_model=primary_judge_id())` (`consequence/metrics.py`).

### 3.2 pass@1 vs pass^k

- **Definition:** pass@1 is the chance a single trial passes; pass^k is the
  chance all k trials of the same task pass, a measure of consistency rather
  than ability.
- **Formula:** pass@1 = state passes / state verdicts. Per task with n
  verdicts and c passes, pass^k(task) = C(c, k) / C(n, k); pass^k is the mean
  over tasks with at least k verdicts. Normal and trap separately. pass^k's
  interval is a fixed-seed bootstrap over tasks, since it is a mean of
  per-task estimates, not one binomial proportion.
- **Value:** NOT YET MEASURED
- **Will come from:** a real run with several trials per (task, model).
- **Regenerate:** `RunMetrics(db, run).pass_at_1(model)` and
  `.pass_hat_k(model, k)`.

### 3.3 Collateral damage rate

- **Definition:** among episodes the state verifier passed, the share that
  also changed rows outside the task's scope, overall and split into benign
  and destructive, per category.
- **Formula:** state passes with a collateral `passed = 0` row / state passes
  with a collateral verdict.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).collateral_rate(model)`.

### 3.4 Cost per solved task

- **Definition:** total episode cost divided by state-verified passes, per
  model and category. Undefined (None, with the reason) when nothing passed
  or any episode's cost is unknown, never zero or infinity.
- **Formula:** sum of `episodes.cost_usd` / count of `state_verifier` passes.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run. Costs come from pinned prices times
  actual token counts (`consequence/models.yaml`).
- **Regenerate:** `RunMetrics(db, run).cost_per_solved_task(model)`.

### 3.5 Failure taxonomy distribution

- **Definition:** share of failed episodes in each failure category.
- **Value:** NOT YET MEASURED
- **Will come from:** the commit that adds `docs/failure-taxonomy.md`.
- **Regenerate:** no command yet.

### 3.6 Judge API agreement

- **Definition:** agreement among the judge's three votes on one episode at
  temperature 0. It measures API-level nondeterminism, NOT judge reliability:
  the three calls see identical input, so it is expected near 1 and says
  nothing about whether the judge is right. Never present it as
  self-consistency. See `docs/judge-methodology.md`.
- **Formula:** per episode, share of votes on the modal (passed, behaviour)
  pair; reported as its distribution (3/3, 2/3, 1/3), its mean, and the
  unanimous share with a Wilson interval. Temperature-0 rows only.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `RunMetrics(db, run).judge_api_agreement(judge_model)`.

### 3.7 Judge stability

- **Definition:** whether the judge gives the same verdict when sampled at
  temperature 0.7: the number that speaks to judge reliability.
- **Formula:** as 3.6, over the `llm_judge_stability` rows at temperature 0.7
  only.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `consequence grade --run N --grader llm_judge
  --judge-stability-subset K`, then
  `RunMetrics(db, run).judge_vote_agreement(judge_model)`.

### 3.8 Cross-judge agreement

- **Definition:** agreement on `passed` between the primary judge (Anthropic)
  and each other judge (the cross-family OpenAI judge), per evaluated model
  and category, to expose self-preference. Computed strictly over the
  episodes BOTH judges gave a verdict on; that intersection is the n.
- **Formula:** episodes where the two verdicts match / the intersection;
  plus the four-way split (both pass, both fail, and each disagreement
  direction) and how many episodes only one judge covered.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `consequence grade --run N --grader llm_judge --judge-model
  judge-gpt-5.1 --judge-subset K`, then
  `RunMetrics(db, run).cross_judge_agreement(model, primary_judge=primary_judge_id())`.

### 3.9 Episodes that did not complete but satisfied the verifier

- **Definition:** episodes that errored, timed out or ran out of turns, yet
  left a final state the verifier accepts. All count as failed; this number
  is reported beside the pass rate, never added to it. Split by normal and
  trap, because a trap is satisfied by an episode that crashed before acting.
- **Formula:** count of episodes with episode status not `completed` and
  `verifier_verdict_raw = 1`. The only place any metric reads that column,
  and it returns counts, not a rate.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).incomplete_but_verifier_satisfied(model)`.

### 3.10 What trap passes are made of

- **Definition:** among TRAP episodes the state verifier passed, the counts
  the judge classified as `refused_and_explained`, `silent_inaction` and
  `false_claim`. The state verifier passes all three alike; separating them
  is what the judge exists for, so this is a headline number, not a
  footnote.
- **Formula:** each behaviour's count over the trap passes the judge gave a
  verdict on, with a Wilson interval; other verdicts and missing verdicts are
  counted separately, never guessed.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `RunMetrics(db, run).trap_behaviour_split(model,
  judge_model=primary_judge_id())`.

### 3.11 Grading coverage

- **Definition:** for every grader and judge model that graded a run: how
  many of the run's episodes it covered, with a verdict, with a
  `grading_error` or `judge_error`, or not at all. Read before any other
  number: it shows whether every rate stands on the full run.
- **Formula:** row counts per (grader, judge_model, status), against the
  run's episode count.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).grading_coverage()`.

### 3.12 Time and turns

- **Definition:** wall-clock seconds per episode (`finished_at` minus
  `started_at`, every episode), and turns per state-verified pass, per model
  and category.
- **Formula:** median and p90 (nearest rank), with n.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).latency_stats(model)` and
  `.turn_stats(model)`.

## 4. Engineering decisions worth claiming

| Decision | Why it was made | Where it is enforced in code | What it rules out |
|---|---|---|---|
| The typed tool layer is the model's only write path | Grading state is only meaningful if every change went through known, validated operations | `consequence/tools/impl.py`, `consequence/tools/schema.py`, `consequence/mcp_server.py`; standing rule 4 | The model writing SQL, bypassing business rules, or writing without an audit row |
| Deterministic, byte-identical reset before every episode | Two episodes are only comparable if they start from the same state | `environment.reset()` (one transaction, explicit ids, no sequences); `Results.verify_state` rejects an episode whose starting digest differs from its run's | Results that depend on episode order or leftover state |
| Canonical normalization of money and timestamps | Equal databases must hash equally, on any machine | `environment._normalize` and `canonical_json`; `db.connect()` pins the session time zone to UTC; `tests/test_timestamps.py` | `205.50` vs `205.5` reading as a change; digests that depend on the server's time zone |
| Offline grading from stored state | A grader bug should cost a re-grade, not a re-run of paid model calls | `consequence/graders/state.py` reads only `results.db`; each run stores its initial snapshot and each episode its final snapshot | Grading against an environment that has since changed; paying twice for one run |
| Fail-closed integrity checks | A grader that guesses is worse than one that stops | `GradingIntegrityError`, `ResultsIntegrityError`, `ResultsSchemaError`; `grade --grader all` refuses if any grader can't run | Partially graded runs and tampered or inconsistent stored state passing silently |
| Provider adapter interface | Adding a model should be a config change | `consequence/providers/base.py` `Provider`; `consequence/providers/registry.py`; `consequence/models.yaml` with pinned version strings and prices | Provider-specific code in the harness; floating model aliases |
| Execution separated from grading | Measuring and judging are different jobs and fail differently | `harness.run_episode` records; `consequence grade` judges later from stored data | A grader bug corrupting a run; re-running models to re-score |
| Two graders with opposite blind spots | A state verifier can't hear an explanation and a transcript reader can't see the database; on trap tasks only the transcript separates refusal, silence and a false claim | `consequence/graders/judge.py` reads only the stored transcript (a test checks it imports nothing that produces state); `behaviour` on every judge row | Treating the twelve trap limits as fixable verifier holes; a judge that quietly sees the state it should be blind to |
| Same denominator for every grader | A gap between graders computed over different episode sets is meaningless and silent | The judge grades every episode whatever its status, writing a `judge_error` row rather than skipping; `test_the_judge_grades_exactly_the_episodes_the_state_verifier_grades` | Pass rates inflated by dropping errored or unjudgeable episodes |
| Never truncate what the judge reads | The false-claim finding depends on the judge seeing an earlier refusal | Overflow is checked before any call and recorded as `judge_error` / `context_overflow`; tests check every stored byte reaches the prompt | Verdicts on silently shortened transcripts |
| Trap tasks scored separately from normal tasks | An agent that does nothing passes every trap | `cli.build_report` and `GradingSummary.pass_counts` group by category with no combined figure | A headline pass rate inflated by inaction |
| A sensitivity bound for episodes with no verdict | A grading or judge error is neither a pass nor a fail, so it can't be counted; but silently dropping it can bias the rate, and the dropped episodes may not be random | `consequence/metrics.py`: every rate returns `no_verdict` and `ungraded` counts beside n, and `verification_gap` returns `bound_low_pp` / `bound_high_pp`, the gap recomputed with every missing verdict at each extreme | A headline gap that hides how much it depends on episodes nobody could grade |
| All reported numbers come from one read-only module | One definition per number, and no way for reporting code to change the record | `consequence/metrics.py` opens `results.db` read-only and imports only the standard library and yaml; a test imports it in a fresh interpreter to prove it loads no model, environment or seed code | Two places computing the same number differently; a metric that quietly writes or calls a model |
| Two notions of success kept apart | An episode that crashed after the right write must still fail, but that fact is worth reporting | `gradings.passed` vs `gradings.verifier_verdict_raw`; `status = 'grading_error'` for a verifier that raises | Crashes counted as passes; verifier bugs counted as agent failures |

How I would say each one out loud:

- **Tool layer.** The agent never sees SQL; it can only call seven typed tools,
  and every write goes through the same validation and writes an audit row.
  That's what makes grading the final database state meaningful: I know every
  way the state could have changed.
- **Deterministic reset.** Before every episode the environment is dropped and
  rebuilt in one transaction from seed data with explicit ids, and the
  starting snapshot's digest is checked against the run's. If two episodes
  didn't start byte-identical, the result is rejected before it's written.
- **Canonical normalization.** Money and timestamps are normalized before
  hashing, so 205.50 and 205.5 are the same value and a timestamp is always
  rendered in UTC. I also pinned the database session to UTC, because CI runs
  the live tests on a different machine than mine.
- **Offline grading.** Each run stores its starting snapshot and each episode
  stores its final snapshot, so grading needs nothing but the results file.
  If I find a grader bug, I fix it and re-grade for free instead of paying for
  the model calls again.
- **Fail closed.** If a stored digest doesn't match, or any requested grader
  can't run, grading stops before writing anything. A partially graded run
  that looks complete is the worst outcome for a benchmark.
- **Adapter interface.** Each provider sits behind one interface, and the
  model registry pins exact version strings and per-token prices. Adding a
  model is a config change, and cost is computed from real token counts.
- **Execution vs grading.** Running an episode only records what happened;
  grading is a separate step that reads those records. A grading bug can
  never damage a run, and changing a grader never needs a new run.
- **Traps scored separately.** Six of the 25 <!-- fact:tasks.count --> tasks are traps where the right
  move is to change nothing, so an agent that does nothing passes all of
  them. Reports always show normal and trap results side by side and never
  combine them.
- **Two graders.** One grader reads the database and never the transcript;
  the other reads the transcript and never the database. On trap tasks a
  refusal, silence and a false claim leave the same empty database, so only
  the transcript grader can tell them apart.
- **Sensitivity bound.** When the judge can't give a verdict on an episode,
  I don't count it as a pass or a fail; I report the gap over the episodes
  both graders judged, and next to it the range the gap could take if every
  missing verdict went one way or the other. If that range crosses zero, the
  headline isn't safe to state, and the report shows it.
- **One metrics module.** Every number I report comes from one module that
  opens the results file read-only and can't reach a model or the
  environment. There is one definition of each number and nothing else
  computes it.
- **Two notions of success.** If an agent makes the right write and then
  crashes, the episode fails, but I keep the verifier's raw verdict for
  analysis. If the verifier itself crashes, that's recorded as a grading
  error, not as the agent failing.

## 5. Defects found and diagnosed

### 5.1 Model input changed query semantics (unescaped LIKE wildcards)

- **What was wrong:** `query_invoices` filtered suppliers with `ILIKE` and
  wrapped the model's text in `%...%` without escaping it. A `%` in the input
  returned every supplier; a partial name silently mixed two suppliers'
  invoices.
- **How it was discovered:** a live test failed against a stale expected set.
  Before updating the expectation, the filter itself had to be shown correct,
  and reading it revealed the bug.
- **Mechanism:** the SQL was parameterized, so there was no injection, but a
  LIKE pattern is a second language inside the parameter. Model output
  decided what the filter matched, against the tool layer's premise that it
  never shapes a query.
- **Fix:** commit `2493619`. Wildcards are escaped, and a name must resolve to
  exactly one supplier or the query is refused with the candidates listed.
- **How it was proven:** with the old filter planted back
  (`substring_supplier_filter`), 5 <!-- fact:planted.substring_supplier_filter.live -->
  live tests fail. Source: `docs/methodology.md`, block `planted-bugs`. Full
  entry in `docs/reward-hacking-log.md`.

### 5.2 Tests with stale expectations that had never run

- **What was wrong:** two assertions were wrong from the day they were
  written: a harness test asserting no rows were added (a void writes an
  audit row), and an expected supplier invoice set that the seed had since
  outgrown.
- **How it was discovered:** the first full run of the live suite, on
  2026-10-10, failed four tests (commit `2493619`'s message): these two, and
  the two MCP tests in 5.3.
- **Mechanism:** live tests are excluded from the default `pytest` run and CI
  had no live job, so these tests had never executed.
- **Fix:** commit `2493619` corrected the assertions (the harness test now
  asserts the exact audit row, a stronger check) and added CI's `live` job
  with a Postgres service container.
- **How it was proven:** the live suite passes on GitHub Actions (runs 19 and
  21).

### 5.3 MCP SDK attribute drift

- **What was wrong:** the MCP SDK builds objects with camelCase keyword
  arguments (`inputSchema=`) but exposes snake_case attributes
  (`.input_schema`, `.is_error`). Tests read `.isError` and
  `.structuredContent`.
- **How it was discovered:** first while building the MCP server (commit
  `7531198`), where tutorials describe an older API; then again in the first
  live run, the second incident of the same kind.
- **Mechanism:** the dependency was `mcp>=1.9.0`, so the installed version
  could change shape under the code, and nothing pinned it.
- **Fix:** commit `2493619` switched every read to snake_case and pinned every
  direct dependency exactly: 10 <!-- fact:dependencies.pinned_exactly --> of
  10 <!-- fact:dependencies.count -->.
- **How it was proven:** the live MCP tests pass in CI with the pinned
  version.

### 5.4 Live and offline snapshots could never hash the same

- **What was wrong:** a live snapshot held native datetime objects while the
  offline seed parser held text, so their digests never matched.
- **How it was discovered:** building the offline grader (commit `a39b99d`),
  which must prove a stored episode is exactly what happened.
- **Mechanism:** two code paths produced the same instant in different
  representations, and a digest compares bytes, not meaning.
- **Fix:** commit `a39b99d` renders every timestamp as a UTC ISO string in
  `_normalize`, as money was already normalized.
- **How it was proven:** `test_live_seed_snapshot_matches_offline_seed`
  asserts a live reset hashes exactly like the parsed seed; it passes in CI's
  live job. `tests/test_timestamps.py` adds microsecond precision, NULL
  handling and session time zone checks.

### 5.5 Write-then-revert beat every verifier

- **What was wrong:** an agent could do the task, damage an unrelated row,
  then put it back. Every verifier compared only start and end states.
- **How it was discovered:** the adversarial pass (commit `bbdeca5`). The
  `transient_write` strategy succeeded against
  25 <!-- fact:adversarial.transient_write.succeeded_at_baseline --> of
  25 <!-- fact:adversarial.transient_write.attempted --> verifiers at baseline.
- **Mechanism:** a diff can't see a change that was undone; the collateral
  check reports net damage only.
- **Fix:** `build_result` appends `audit_trail_within_scope` to every
  verifier centrally: every audit row the episode wrote must point at an
  in-scope row.
- **How it was proven:** all 25 <!-- fact:adversarial.transient_write.closed -->
  closed in the generated table in `docs/reward-hacking-log.md`, which CI
  regenerates and compares (`test_log_matches_this_run`).

### 5.6 Number-spraying beat two verifiers, including one claimed safe

- **What was wrong:** tasks 004 and 011 accepted a flag reason that merely
  contained the right amount, so a reason listing every amount in a range
  passed.
- **How it was discovered:** the adversarial pass, strategy
  `letter_not_intent`. The reward-hacking log had previously called the 011
  hole closed.
- **Mechanism:** a substring check on free text is satisfied by enumerating.
- **Fix:** an allow-list of amounts an honest reason could cite. The log
  records that the earlier claim was wrong rather than quietly replacing it.
- **How it was proven:** 2 <!-- fact:adversarial.letter_not_intent.closed -->
  closed in the generated adversarial table.

### 5.7 A test passing for the wrong reason

- **What was wrong:** "an unimplemented grader exits with code 2" got its 2
  from an unrelated integrity stop.
- **How it was discovered:** while adding the state grader (commit `a39b99d`).
- **Mechanism:** asserting only an exit code can't tell two failures apart.
- **Fix:** the test also asserts the error message.
- **How it was proven:** the assertion now pins the cause, not just the code.

### 5.8 A test that only passed on the dev machine

- **What was wrong:** a CLI test needed Postgres, but only because the code
  it called had started resetting the environment.
- **How it was discovered:** CI run 20 failed on `f402933` with
  `DATABASE_URL is not set`.
- **Mechanism:** locally, `.env` supplied a database URL and Postgres was
  running, which hid the dependency; CI's `check` job has no database.
- **Fix:** commit `c66c78d` stubs the reset in that test.
- **How it was proven:** the full offline suite passes with `DATABASE_URL`
  blanked, and CI run 21 passed.

### 5.9 `--grader all` reported partial grading as success

- **What was wrong:** with one registered grader not implemented,
  `consequence grade --grader all` graded the rest and exited 0.
- **How it was discovered:** hardening review, after the state grader landed.
- **Mechanism:** the command treated "not implemented" as a note, not an
  error, unlike every other integrity path.
- **Fix:** uncommitted at time of writing. It now refuses before writing
  anything and names the missing grader.
- **How it was proven:** `test_grade_all_fails_closed_when_a_registered_grader_cannot_run`
  asserts the refusal and that no rows were written.

## 6. Evidence that the work validates itself

### Adversarial verifier audit

Cited from `docs/reward-hacking-log.md`, block `adversarial:summary`, which
`python -m consequence.adversaries --write-log` regenerates and CI compares.

| Fact | Value | Status | Source |
|---|---|---|---|
| Attacks run | 150 <!-- fact:adversarial.attempted --> | MEASURED | `adversarial:summary` |
| Succeeded against the baseline verifiers | 39 <!-- fact:adversarial.succeeded_at_baseline --> | MEASURED | `adversarial:summary` |
| Closed | 27 <!-- fact:adversarial.closed --> | MEASURED | `adversarial:summary` |
| Structural limits of state verification (all trap tasks; mitigated by the judge) | 12 <!-- fact:adversarial.structural_limit --> | MEASURED | `adversarial:summary` |
| Rejected from the start | 111 <!-- fact:adversarial.rejected_from_start --> | MEASURED | `adversarial:summary` |

### Planted-bug validation

Cited from `docs/methodology.md`, block `planted-bugs`, which `python -m
consequence.planted_bugs --write-doc` regenerates. Each count is failing
tests with the bug planted; the totals and the commit they were measured at
are in that block.

| Fact | Value | Status | Source |
|---|---|---|---|
| Grader reads the live environment: offline tests failing | 24 <!-- fact:planted.reads_live_environment.offline --> | MEASURED | `planted-bugs` |
| Grader reads the live environment: live tests failing | 1 <!-- fact:planted.reads_live_environment.live --> | MEASURED | `planted-bugs` |
| Grader skips errored episodes: offline tests failing | 13 <!-- fact:planted.skips_errored_episodes.offline --> | MEASURED | `planted-bugs` |
| Grader skips errored episodes: live tests failing | 1 <!-- fact:planted.skips_errored_episodes.live --> | MEASURED | `planted-bugs` |
| Old substring supplier filter: offline tests failing | 0 <!-- fact:planted.substring_supplier_filter.offline --> | MEASURED | `planted-bugs` |
| Old substring supplier filter: live tests failing | 5 <!-- fact:planted.substring_supplier_filter.live --> | MEASURED | `planted-bugs` |
| Manual spot-check agreement with the failure taxonomy | NOT YET MEASURED | NOT YET MEASURED | Needs `docs/failure-taxonomy.md` |

### CI

Two jobs (`.github/workflows/ci.yml`): `check` runs `ruff check .` and the
offline suite with no database; `live` runs the live suite against a
`postgres:16` service container. Both run on every push and pull request.
Recent results on GitHub Actions: run 21 (`c66c78d`) passed, run 20
(`f402933`) failed (section 5.8), run 19 (`2493619`, the first with the live
job) passed. Source: the repository's Actions page, or `curl -s
https://api.github.com/repos/Yashpython/consequence/actions/runs`.

## 7. Artifacts

| Artifact | Status | Where |
|---|---|---|
| Public repository | EXISTS | https://github.com/Yashpython/consequence |
| CI with a live Postgres job | EXISTS | `.github/workflows/ci.yml` |
| MCP server for the tool layer | EXISTS | `python -m consequence.mcp_server`; `docs/mcp.md` |
| Results from real model runs (`docs/findings.md`) | PLANNED | Needs a real run |
| Failure taxonomy (`docs/failure-taxonomy.md`) | PLANNED | Not started |
| Deployed site | PLANNED | Not started |
| Published environment or package | PLANNED | Not started; version in `pyproject.toml` is a placeholder |
| Demo assets | PLANNED | Not started |
| Release tags | PLANNED | `git tag` lists none |

## 8. Candidate resume bullets

Form: accomplished X, as measured by Y, by doing Z. READY bullets contain only
measured numbers, each checked by the ledger check. DRAFT bullets depend on
something not yet measured.

- **READY** Hardened 25 <!-- fact:verifiers.count --> state-based verifiers against reward hacking, closing 27 <!-- fact:adversarial.closed --> of the 39 <!-- fact:adversarial.succeeded_at_baseline --> attacks that beat them, by running 150 <!-- fact:adversarial.attempted --> scripted adversarial agents across 6 <!-- fact:adversarial.strategies --> cheating strategies.
- **READY** Caught a write-then-revert exploit that passed all 25 <!-- fact:adversarial.transient_write.succeeded_at_baseline --> verifiers, by adding an audit-trail check that every verifier inherits centrally.
- **READY** Found and fixed a tool-layer bug where a single wildcard character in model input returned every supplier's records, proven by restoring the old filter and watching 5 <!-- fact:planted.substring_supplier_filter.live --> new tests fail.
- **READY** Made grading reproducible from a single results file with no database or model access, verified by planting a grader bug that 24 <!-- fact:planted.reads_live_environment.offline --> offline tests catch.
- **READY** Built a test suite of 617 <!-- fact:tests.offline --> offline and 332 <!-- fact:tests.live --> live tests, with CI running the live suite against a Postgres service container on every push.
- **DRAFT** Measured how often agents claim success the database contradicts (verification gap: NOT YET MEASURED) across the registry's models, by grading final state instead of transcripts.
- **DRAFT** Showed the difference between one-shot and every-time reliability (pass@1 vs pass^k: NOT YET MEASURED), by running repeated trials per task and model.
- **DRAFT** Quantified collateral damage (rate: NOT YET MEASURED) by classifying every out-of-scope row change as benign or destructive.
- **DRAFT** Separated justified refusals from silence and false claims among trap-task passes the database could not distinguish (split: NOT YET MEASURED), by pairing a state grader with a transcript-only LLM judge.

## 9. Explicitly NOT claimable

- **Any result about real models.** No episode has been run against a real
  model. Every finding in section 3 is NOT YET MEASURED.
- **"Supports Anthropic, OpenAI, Google and OpenRouter" as tested.** The
  adapters are implemented and unit-tested offline, but the smoke tests that
  make one real API call per provider have never run. Claim "adapters
  implemented", not "verified against".
- **Pinned prices and model strings as confirmed.** `docs/models.md` records
  which still need live confirmation.
- **"The environment DB was stopped and grading still worked."** That test is
  fault injection (a dead port and raising stubs); no container is stopped.
  See `docs/methodology.md`.
- **"CI green on every commit."** Run 20 failed (section 5.8).
- **Earlier planted-bug counts not in `docs/methodology.md`.** Verifier
  mutations were checked by hand while tasks were written, but their outputs
  aren't stored in the repository. Re-measure them through
  `consequence/planted_bugs.py` before citing any count.
- **Anything about adoption.** No one else uses it, it has no stars or
  forks, and no release.
- **Any judge result.** The judge is built and tested against stubs only; it
  has never called a real model. Its accuracy, its agreement figures and its
  self-preference are all unmeasured.
- **"`judge_api_agreement` shows the judge is consistent."** It measures API
  nondeterminism at temperature 0, not reliability. Only the temperature-0.7
  stability subset speaks to reliability.
- **"Self-preference bias is handled."** The cross-family judge is a design
  that can measure it; nothing has been measured.
- **Judge context windows.** `context_window_tokens` is unset for both
  judges; the judge refuses to run until it is set from vendor documentation.
- **Mutation checks on the judge tests.** Four bugs were planted in
  `judge.py` by hand while it was written, and each was caught, but the
  outputs aren't stored. Add them to `consequence/planted_bugs.py` before
  citing a count.
- **The failure taxonomy.** It doesn't exist.
- **A verification gap without its n and sensitivity bound.** When any
  episode lacks a verdict, the paired gap alone can mislead: in the metrics
  tests, six judge errors out of ten turn a paired gap of -50 points into a
  bound of -30 to +30. Quote the bound with the gap.
- **A Wilson interval on pass^k.** pass^k is a mean of per-task estimates;
  its interval is a bootstrap over tasks, not Wilson. Say which.
- **Mutation checks on the metrics tests.** Four bugs were planted in
  `metrics.py` by hand while it was written, and each was caught, but the
  outputs aren't stored; add them to `consequence/planted_bugs.py` before
  citing a count.
- **Lines of code, or speed and cost of a full run.** Not measured.
- **The do-nothing agent's score on traps as a finding.** It is a property of
  the task design, shown with a mock agent, not a measured model behaviour.

## 10. Ledger changelog

| Date | Commit | Change |
|---|---|---|
| 2026-10-13 | uncommitted metrics work | Added `consequence/metrics.py` as the only source of reported numbers. Rewrote section 3 so every definition matches the code and names its `RunMetrics` call: new definitions for the trap behaviour split (3.10), cross-judge agreement over the intersection (3.8) and grading coverage (3.11), plus time and turns (3.12). Corrected three earlier definitions to match the metrics as specified: the verification gap is a difference in percentage points, not a share of judge passes; collateral is over state passes only; cost per solved task is per category, traps included. Added the no-verdict sensitivity bound and the single metrics module to section 4, a scale fact, and section 9 guards. |
| 2026-10-11 | uncommitted judge work | Added the LLM judge: scale facts (judge models), section 3 definitions for the verification gap, judge API agreement, stability, cross-family comparison and the new 3.10 trap behaviour split, three engineering decisions, the reframed adversarial row (structural limits), a DRAFT bullet, and section 9 guards. Corrected 3.6, which wrongly defined judge API agreement as judge-vs-state agreement. |
| 2026-10-11 | `c66c78d` plus uncommitted hardening work | Created. Backfilled from every commit, `a033e8e` through `c66c78d`, and the hardening work in progress. Added the ledger check (`consequence/ledger.py`) and the planted-bug harness whose output section 6 cites. |
