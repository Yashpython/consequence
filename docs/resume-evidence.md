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
| Metrics computed from the results DB | 22 <!-- fact:metrics.count --> | MEASURED | `consequence/metrics.py` `METRICS` |
| Providers in the registry | 4 <!-- fact:providers.count --> | MEASURED | `provider` field in `consequence/models.yaml` |
| Trials per (task, model) in the experiment | NOT YET MEASURED | NOT YET MEASURED | Not fixed yet: `consequence run --trials` defaults to one. Set by the first real run. |
| Total episodes run against real models | NOT YET MEASURED | NOT YET MEASURED | `consequence status` after the first real run. No real run exists. |
| Offline tests | 757 <!-- fact:tests.offline --> | MEASURED | `pytest --collect-only -q -m "not live and not smoke"` |
| Live tests (real Postgres) | 332 <!-- fact:tests.live --> | MEASURED | `pytest --collect-only -q -m live` |
| Smoke tests (billed API calls) | 8 <!-- fact:tests.smoke --> | MEASURED | `pytest --collect-only -q -m smoke`: each judge under each condition it runs under (the primary judge once, at the provider's default; the cross-family judge at 0 and 0.7), each model under test two tool-calling turns. All passed against the real APIs on 2026-10-19 (`docs/experiment-log.md`). |
| Adversarial strategies | 6 <!-- fact:adversarial.strategies --> | MEASURED | `docs/reward-hacking-log.md`, block `adversarial:summary` |
| Adversarial attacks (strategies x tasks) | 150 <!-- fact:adversarial.attempted --> | MEASURED | `docs/reward-hacking-log.md`, block `adversarial:summary` |
| CI jobs | 2 <!-- fact:ci.jobs --> | MEASURED | `.github/workflows/ci.yml` (`check`, `live`) |
| Direct dependencies pinned exactly | 10 <!-- fact:dependencies.pinned_exactly --> of 10 <!-- fact:dependencies.count --> | MEASURED | `pyproject.toml` |

## 3. Measured findings

None of these exist yet: no episode has been run against a real model. Each
entry fixes the definition now, so the number can't be redefined after it is
seen. Every value will be computed by `consequence/metrics.py`, from the
run's `results.db` and nowhere else; `RunMetrics(db, run)` below opens it.
`consequence report --run N [--format json] [--pass-k K ...]` lays all of
them out per model, computing nothing itself (`consequence/report.py`).
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
  temperature 0, for a judge that takes a temperature (the primary judge
  doesn't, so it has none). It measures API-level nondeterminism, NOT judge
  reliability:
  the three calls see identical input, so it is expected near 1 and says
  nothing about whether the judge is right. Never present it as
  self-consistency. See `docs/judge-methodology.md`.
- **Formula:** per episode, share of votes on the modal (passed, behaviour)
  pair; reported as its distribution (3/3, 2/3, 1/3), its mean, and the
  unanimous share with a Wilson interval. Temperature-0 rows only: the
  dedicated `llm_judge_api_agreement` sample when the primary verdicts were
  single votes (`--judge-votes 1 --api-agreement-subset N`), otherwise the
  primary triplicates. A single vote records no agreement.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `RunMetrics(db, run).judge_api_agreement(judge_model)`.

### 3.7 Judge stability

- **Definition:** whether the judge gives the same verdict across votes drawn
  with sampling on (sampling stability): the number that speaks to judge
  reliability. For the primary judge, which rejects a temperature, the three
  votes on every episode at the provider's default sampling; for a judge that
  takes one, a subset re-judged at temperature 0.7.
- **Formula:** as 3.6, over the primary judge's `llm_judge` rows sampled at
  `provider_default`, or a temperature judge's `llm_judge_stability` rows at
  0.7. The figure carries its `condition`.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `consequence grade --run N --grader llm_judge` (primary
  judge), or `--judge-model J --judge-stability-subset K` for a temperature
  judge, then `RunMetrics(db, run).judge_vote_agreement(judge_model)`.

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

### 3.13 Judging cost

- **Definition:** what one judge cost on a run, across its `llm_judge` and
  `llm_judge_stability` rows, judge errors included (a failed call still
  spends tokens). Judging spend only: never part of an episode's cost or of
  cost per solved task.
- **Formula:** sum of `gradings.judge_cost_usd` for that judge; unknown if any
  row's cost is unknown.
- **Value:** NOT YET MEASURED
- **Will come from:** the live judging commit.
- **Regenerate:** `RunMetrics(db, run).judge_cost(judge_model)`.

### 3.14 Episode outcomes

- **Definition:** episodes per status (completed, error, timeout, max_turns),
  per model and category.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).episode_status_counts(model)`.

### 3.15 Served-model drift

- **Definition:** for each configured model and judge, which model versions
  actually served its calls in a run (read from each response), and whether
  more than one did. A pinned model ID can still be served by something else:
  a provider alias, a routed host, a repointed code.
- **Formula:** distinct `served_model` values over the run's episodes (or
  the judge's gradings rows); drift when there is more than one.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).served_models(model)`,
  `.judge_served_models(judge_model)`; `consequence grade` and `consequence
  report` flag drift.

### 3.16 Visible agent narration

- **Definition:** how much the judge had to read beyond the tool results, per
  model: characters of the agent's own text per episode. Tool calls, their
  arguments and tool results are excluded. Models that narrate less give the
  judge less evidence, which confounds per-model comparisons of the
  verification gap; this measures that asymmetry instead of assuming it away
  (`docs/judge-methodology.md`, Limitations).
- **Formula:** per episode, the summed length of `transcripts.content` on
  assistant turns (0 if none); median and p90 over the model's episodes,
  whatever their status.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).narration_chars(model)`; `consequence
  report` shows it per model.

### 3.17 Episodes cut off by the output cap

- **Definition:** the share of a model's episodes containing any agent turn
  that stopped because it reached the output cap, per category. A reasoning
  model that spends the cap on thinking returns no tool call and fails for a
  reason unrelated to its behaviour; the cap is uniform (16,000 tokens per
  turn) and this measures the truncation that remains.
- **Formula:** episodes with any assistant turn where
  `transcripts.output_capped = 1`, over all the model's episodes in that
  category, whatever their status; Wilson interval.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).output_capped_episodes(model)`;
  `consequence report` shows it per model.

### 3.18 Reasoning state round trips

- **Definition:** per model, the agent turns whose reply carried reasoning
  state the vendor requires back verbatim (Claude thinking and
  redacted_thinking blocks, Gemini thought signatures), how many of those
  were sent back on a next call, and how many of those next calls were
  accepted. This is what proves the replay at scale; until it is measured,
  the round trip is shown only by the smoke test, once per model.
- **Formula:** over `transcripts` rows with `reasoning_state = 1`: accepted
  when a later agent turn exists in the episode; next call failed when the
  turn led to tool calls and the episode ended in `error` (any failure, not
  only a rejection of the replay); otherwise not sent back (final answer, or
  a limit ended the episode). The acceptance share is accepted over sent
  back, with a Wilson interval.
- **Value:** NOT YET MEASURED
- **Will come from:** the first real run.
- **Regenerate:** `RunMetrics(db, run).reasoning_round_trips(model)`;
  `consequence report` shows it per model.

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
| The report computes nothing | A report that does its own counting is a second definition of every number, and the old one pooled models together | `consequence/report.py` only calls `RunMetrics` and formats; `tests/test_report.py` parses it and rejects arithmetic operators, counting builtins and direct DB access, checks every figure equals the metrics' own, and checks it imports nothing but `consequence.metrics` | A report figure that disagrees with the metric it claims to show; results pooled across models |
| Vendor-documented model configuration | A floating alias or a remembered context window would make the run unrepeatable or let a transcript overflow silently | Every `api_model` passes a vendor-specific pinned-version rule in `tests/test_providers.py` (a planted alias fails it); every judge context window is cited to the vendor's page in `models.yaml` and must be set or the judge refuses to run | An evaluated model silently upgraded mid-study; an overflow check built on a guessed number |
| Provider-native replay in tool-use loops | Claude thinking blocks and Gemini thought signatures must go back exactly as received, which a provider-neutral conversation can't reconstruct | Each adapter returns its reply's own content as `TurnResult.replay` and sends it back verbatim; the harness passes it through opaquely (rule 7 holds) | A model that reasons worse, or rejects the call, because its own reasoning state was dropped mid-episode |
| Served-version provenance | A pinned model ID names what was asked for, not what answered | `served_model` on every episode and judge grading, read from the raw response by each adapter; drift flagged by `grade`, `report` and the experiment log, never blocking | Silently mixing two model versions in one model's results |
| The strongest available judge, whatever sampling it accepts | An older, more credulous judge would inflate the verification gap and make the headline look like a strawman; choosing a judge to suit a sampling parameter gets the priority backwards | `models.yaml`: primary judge `claude-opus-5-5`, `sampling: provider_default`; `judge.check_sampling` refuses any temperature, stability subset or API-agreement subset for it before a call; documented in `docs/judge-methodology.md` | A headline gap measured against a weak judge; a temperature sent to a model that rejects one |
| Agreement named by the condition it measures, enforced by the schema | Three votes at temperature 0 measure API nondeterminism; three at the provider's default measure sampling stability; the arithmetic is the same | `gradings.judge_sampling` plus CHECKs: `judge_api_agreement` only at temperature 0, `judge_vote_agreement` only above 0 or at the provider's default | Reporting provider determinism as judge reliability, or the reverse |
| A verdict format each judge can be held to | Claude Opus 5.5 rejects forced tool use, so an optional `record_verdict` call could come back as prose | The primary judge answers through structured outputs (`output_config.format`, the verdict schema); the adapter turns the JSON into the same call, checked by the same `parse_vote` | Verdicts lost to `malformed_output`, or guessed from prose |
| Equal evidence for the judge across models, then measured | Opus 5.5's notes between tool calls come back empty by default where other models write text | Opus 5.5 agents run with `thinking.display: "updates"`, documented as changing only what is returned; the adapter keeps the updates as turn text; `narration_chars` reports what asymmetry remains | A per-model gap that differs because the judge read less |
| One output cap for every model under test, set above what reasoning needs | Thinking counts toward the cap; a model that spends a small one on thinking returns no tool call and is scored as failing for a reason unrelated to its behaviour, which biases the benchmark against reasoning models | `models.yaml` `agent_max_output_tokens: 16000`; `load_model_registry` refuses a per-model cap; `tests/test_output_cap.py` and the SDK wire tests check every model's request carries it | Comparing models run under different caps; scoring a truncated turn as the model's choice |
| A reasoning round trip counts only if it was exercised | A second call that succeeds proves nothing about replay if the first reply carried no reasoning state, and adaptive thinking can skip reasoning | Every adapter reports `reasoning_state`, stored on every agent turn; the smoke two-turn test for Opus 5.5 and the Gemini models skips ("no reasoning produced; round trip unproven") instead of passing when there was none; `metrics.reasoning_round_trips` counts carried, sent back and accepted per model | Claiming the replay works from calls that never replayed anything |
| Truncation stored per turn and measured per model | A cap can still be hit; assuming it never is would hide the bias it was raised to remove | `transcripts.stop_reason` (verbatim) and `output_capped` (each adapter's mapping of its vendor's cap stop), required by the schema on every agent turn; `metrics.output_capped_episodes` | Unmeasured residual truncation in a per-model comparison |
| Every billed smoke call rehearsed offline through the real SDKs | A request the pinned SDK refuses only shows up when someone pays for a call | `tests/test_provider_sdk.py` runs each smoke-test call through the real SDK against a fake transport; CI installs the SDKs | Smoke-test failures from SDK argument handling (two found: 5.17, 5.18) |
| A planted-bug harness that refuses a red baseline | A count of "tests that caught the bug" is only meaningful if those tests pass without it | `consequence/planted_bugs.py` runs both suites unchanged first and stops without planting or writing if either has a failure | Publishing noise as validation, as happened once with the database down |
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
- **The report computes nothing.** The report only lays out what the metrics
  module returns; a test reads the report's own source and fails if it finds
  any arithmetic or counting. So there is exactly one definition of every
  number, and the report can't drift from it.
- **Vendor-documented configuration.** Every model version is checked against
  the vendor's own rule for what counts as a pinned version, and every judge
  context window carries a link to the page it came from. That check found
  one of the models under test pinned to an alias.
- **Replay.** Some models keep part of their reasoning in opaque blocks that
  must be sent back exactly as received on the next call. Each adapter hands
  the harness its own reply to replay verbatim, so the harness stays
  provider-neutral and no model loses its reasoning mid-task.
- **Served versions.** Every call records the model that actually answered,
  because a pinned name is only what was asked for. If one model is served by
  two versions within a run, grading still goes ahead but it is flagged
  everywhere the results are read.
- **A harness that checks itself.** My bug-planting harness runs the suites
  clean first and refuses to measure if anything already fails, because a
  database outage once made every planted bug look caught by every test.
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
- **Fix:** `6d92c81`. It now refuses before writing
  anything and names the missing grader.
- **How it was proven:** `test_grade_all_fails_closed_when_a_registered_grader_cannot_run`
  asserts the refusal and that no rows were written.

### 5.10 The run report pooled every model together

- **What was wrong:** `consequence report` computed its own pass counts,
  costs and turn totals, and keyed them by grader only, so every model in a
  run was summed into one figure. A run comparing models reported one blended
  pass rate per grader, and the report's numbers had a second definition
  separate from the metrics.
- **How it was discovered:** while adding `consequence/metrics.py`, whose rule
  is that every reported number comes from it. Comparing the two showed the
  report never split by model.
- **Mechanism:** the report predated per-model reporting; it was written when
  runs held one model, and aggregated whatever rows a run contained.
- **Fix:** `60c72a6`. `consequence/report.py` builds every
  table per model from `RunMetrics` and computes nothing; `cli.py` only calls
  it.
- **How it was proven:** `tests/test_report.py` rejects any arithmetic in the
  report's source, checks every figure equals the metric's, and checks two
  models stay apart; planting the pooling back made report and CLI tests
  fail (checked by hand; the run's output isn't stored).

### 5.11 A model under test was pinned to a moving alias

- **What was wrong:** `models.yaml` pinned Claude Haiku 4.5 as
  `claude-haiku-4-5`, which Anthropic documents as "a convenience alias that
  resolves to the pinned snapshot claude-haiku-4-5-20251001". The test meant
  to catch floating versions only rejected the word "latest".
- **How it was discovered:** checking every `api_model` against vendor
  documentation before the real run. Anthropic's rule: dateless IDs are
  pinned only from the 4.6 generation on.
- **Mechanism:** the naming convention changed across generations, so a
  dateless ID is a pin for newer models and an alias for older ones; a
  string check can't tell them apart without the vendor's rule.
- **Fix:** `7c19937`. Pinned to
  `claude-haiku-4-5-20251001`, with the source cited in `models.yaml`; the
  test now encodes each vendor's documented rule and covers judges too.
- **How it was proven:** the rule has its own tests, including the old alias,
  which it flags.

### 5.12 The OpenRouter adapter would have sent the OpenAI key

- **What was wrong:** given no key, the OpenRouter adapter let the `openai`
  SDK pick one, and that SDK reads `OPENAI_API_KEY`. The open-weights run
  would have sent the OpenAI secret to openrouter.ai, and failed there.
- **How it was discovered:** tracing how each smoke test would find its
  credentials.
- **Mechanism:** OpenRouter is reached through the OpenAI SDK pointed at a
  different base URL; the SDK's credential default belongs to the original
  vendor.
- **Fix:** `7c19937`. The adapter resolves
  `OPENROUTER_API_KEY` itself and refuses to run without it.
- **How it was proven:** `test_openrouter_never_sends_the_openai_key` sets
  only `OPENAI_API_KEY` and asserts the adapter refuses.

### 5.13 The planted-bug harness measured noise with the database down

- **What was wrong:** a measurement run with the Postgres container stopped
  reported every planted bug as failing every live test.
- **How it was discovered:** the result was implausible: a bug in the judge,
  which no live test touches, can't fail every live test.
- **Mechanism:** the harness counted failures with the bug planted but never
  checked the suites passed without it; every live test failed for lack of a
  database.
- **Fix:** `7c19937`. The harness runs both suites
  unchanged first and stops, planting and writing nothing, if either fails.
- **How it was proven:** `test_a_red_baseline_plants_nothing_and_writes_nothing`;
  the table was then regenerated with the database up.

### 5.14 The primary judge could never have run

- **What was wrong:** the judge design sends temperature 0 and 0.7, and the
  primary judge was Claude Opus 5. Anthropic documents that on Opus 4.7 and
  later, Opus 5 included, "non-default `temperature`, `top_p`, or `top_k`
  values return a 400 error on every request, regardless of whether thinking
  is used".
- **How it was discovered:** reading the vendor documentation while moving
  the lineup to the current Opus, before any smoke test had run.
- **Mechanism:** newer Claude models removed sampling controls; the design
  assumed every judge has them.
- **Fix:** `f399150` made the primary judge `claude-opus-4-6`, the strongest
  Claude model that accepts a temperature. Since superseded: the primary
  judge is `claude-opus-5-5` at the provider's default sampling, with no
  temperature sent (section 4), because the judge's strength matters more
  than its sampling controls.
- **How it was proven:** the judge smoke tests, 2026-10-19
  (`docs/experiment-log.md`): `judge-claude-opus-5.5` returned a verdict with
  no temperature sent, and `judge-gpt-5.1` at 0 and 0.7.

### 5.15 Gemini adapter: thinking left out of cost, and a storage crash

- **What was wrong:** output tokens were `candidates_token_count`, which
  excludes thinking tokens that Google bills as output ("Output (includes
  thinking tokens)"); and the stored raw response kept thought signatures as
  bytes, which JSON can't encode, so recording a thinking model's transcript
  could fail.
- **How it was discovered:** reading the Google adapter while checking how
  Gemini thought signatures must be replayed.
- **Mechanism:** both followed from Gemini 3+ models thinking by default;
  the adapter was written for responses without thinking.
- **Fix:** `f399150`. Output tokens add `thoughts_token_count`; the raw
  response is stored in JSON mode.
- **How it was proven:** `test_google_replays_its_content_and_counts_thinking_as_output`
  with a fake response. The real Gemini calls in the 2026-10-19 smoke run
  were recorded without error; their token counts were not checked against
  Google's billing.

### 5.16 Reasoning state was dropped between agent turns

- **What was wrong:** adapters rebuilt each assistant turn from its text and
  tool calls, discarding Claude thinking blocks and Gemini thought
  signatures, both of which the vendors require back unmodified in tool-use
  loops.
- **How it was discovered:** the Claude Opus 5.5 "what's new" page ("pass
  `thinking` blocks back unmodified in tool-use loops") and Google's thinking
  guide ("You MUST always resend all thought blocks exactly as they were
  received").
- **Mechanism:** the provider-neutral conversation format had no place for
  provider-only state.
- **Fix:** `f399150`. `TurnResult.replay`, sent back verbatim by the adapter
  that produced it.
- **How it was proven:** offline tests check the replay goes back verbatim
  (`test_anthropic_replays_its_own_blocks_verbatim`,
  `test_the_harness_replays_and_records_the_served_model`); every model's
  two-turn smoke test, whose second call carries the first reply back
  verbatim, passed against the real APIs on 2026-10-19
  (`docs/experiment-log.md`).

### 5.17 The Anthropic SDK has no temperature parameter

- **What was wrong:** the Anthropic adapter passed `temperature` to
  `client.messages.create()`, which in the pinned `anthropic==1.13.0` has no
  such parameter. Every temperature-0 judge call to `claude-opus-4-6`, the
  primary judge in `f399150`, would have raised `TypeError: Messages.create()
  got an unexpected keyword argument 'temperature'` before any request.
- **How it was discovered:** installing the pinned SDKs and calling each
  adapter through its real SDK against a fake transport, before the smoke
  tests were run.
- **Mechanism:** the offline tests checked the adapter's request dict, never
  the SDK call that consumes it.
- **Fix:** the `final prep` commit. A temperature goes in
  `extra_body`; `tests/test_provider_sdk.py` makes every smoke-test call
  through the real SDKs, and CI now installs them.
- **How it was proven:** `test_an_anthropic_temperature_reaches_the_request_body`.

### 5.18 A judge output cap the SDK refuses without streaming

- **What was wrong:** the new primary judge's cap was first set to 32,000
  output tokens. The pinned Anthropic SDK refuses a non-streaming request
  whose `max_tokens` could take over 10 minutes (3,600 s x max_tokens /
  128,000 > 600 s, so above 21,333), so every judge call would have raised
  `ValueError` before any request.
- **How it was discovered:** `test_each_judge_condition_goes_through_its_sdk`
  failed on the first run of the new offline SDK tests.
- **Mechanism:** an SDK-side limit, not an API one: nothing in the vendor's
  model documentation states it.
- **Fix:** the `final prep` commit. The cap is 16,000, with the limit
  cited in `models.yaml`.
- **How it was proven:** the same test passes.

### 5.19 Agents capped at 4,096 tokens, and Gemini cut-offs hidden

- **What was wrong:** every agent turn ran at the adapters' default output
  cap, 4,096 tokens, and thinking counts toward it on Claude Opus 5.5 and
  both Gemini models. A turn that spent it on thinking would return no tool
  call and be scored as the model failing the task. Nothing would have shown
  it: no agent turn stored its stop reason, and the Google adapter replaced
  Gemini's finish reason with `tool_use` or `end_turn`, so a `MAX_TOKENS`
  turn looked like a normal end.
- **How it was discovered:** reviewing the agent configuration after the
  judge's own cap had to be sized for thinking (5.18).
- **Mechanism:** the cap was never set, so the adapters' default applied,
  sized for models without thinking; the stop reason was treated as
  adapter-internal.
- **Fix:** the `final prep` commit. One cap of 16,000 for every model
  under test; every agent turn stores `stop_reason` and `output_capped`;
  Gemini's finish reason is kept verbatim; `output_capped_episodes` (3.17)
  reports what remains.
- **How it was proven:** `tests/test_output_cap.py` (the cap on every model,
  each adapter's mapping, storage on every turn, the schema's refusal, the
  metric) and `test_a_gemini_turn_cut_off_by_the_cap_is_flagged_through_the_real_sdk`.

### 5.20 Gemini rejected every tool the benchmark gives an agent

- **What was wrong:** the Google adapter sent each tool's JSON Schema as a
  function declaration's `parameters`, which "Represents a select subset of
  an OpenAPI 3.0 schema object" with no `additionalProperties`. Every tool
  schema sets `additionalProperties: false`, so the API rejected each agent
  request with 400 `Unknown name "additional_properties"`: neither Gemini
  model could have run a single episode.
- **How it was discovered:** the first smoke run, 2026-10-19 (both Gemini
  model tests failed with that error).
- **Why the offline SDK tests couldn't catch it:** `tests/test_provider_sdk.py`
  sends each request through the real Google SDK, but to a fake server that
  accepts any payload. The SDK didn't object: it serialized
  `additionalProperties` as `additional_properties` without complaint. The
  check that refused it is Google's, on Google's servers, so only a real
  call could reach it. The offline test now pins the fixed shape (the schema
  in `parameters_json_schema`, no `additional_properties` anywhere), which
  guards the fix but could not have found the defect.
- **Mechanism:** the adapter used the OpenAPI-subset field for a full JSON
  Schema; the SDK serialized the unsupported keyword instead of refusing it.
- **Fix:** the `smoke-run fixes` commit. The schema goes, unchanged, in
  `parameters_json_schema`, which "Describes the parameters to the function
  in JSON Schema format" (https://ai.google.dev/api/generate-content), so
  Gemini gets the same tool schema as every other model, with
  `additionalProperties: false` kept, not stripped. Argument enforcement
  never depended on it: the tool layer refuses undeclared arguments from
  every adapter alike (`tests/test_tool_arguments.py`).
- **How it was proven:** offline,
  `test_gemini_gets_the_tool_schema_unchanged_as_json_schema`; then both
  Gemini smoke tests passed against the real API (`pytest -m smoke -rf -k
  gemini`, 2026-10-19, `docs/experiment-log.md`).

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
consequence.planted_bugs --write-doc` regenerates, only after both suites
pass unchanged. Each count is failing tests with the bug planted; the totals
and the commit they were measured at are in that block. The judge is tested
offline only (stubbed judge providers), so its bugs fail no live test by
design.

| Fact | Value | Status | Source |
|---|---|---|---|
| Grader reads the live environment: offline tests failing | 27 <!-- fact:planted.reads_live_environment.offline --> | MEASURED | `planted-bugs` |
| Grader reads the live environment: live tests failing | 1 <!-- fact:planted.reads_live_environment.live --> | MEASURED | `planted-bugs` |
| Grader skips errored episodes: offline tests failing | 14 <!-- fact:planted.skips_errored_episodes.offline --> | MEASURED | `planted-bugs` |
| Grader skips errored episodes: live tests failing | 1 <!-- fact:planted.skips_errored_episodes.live --> | MEASURED | `planted-bugs` |
| Old substring supplier filter: offline tests failing | 0 <!-- fact:planted.substring_supplier_filter.offline --> | MEASURED | `planted-bugs` |
| Old substring supplier filter: live tests failing | 5 <!-- fact:planted.substring_supplier_filter.live --> | MEASURED | `planted-bugs` |
| Judge skips errored episodes: offline tests failing | 11 <!-- fact:planted.judge_skips_errored_episodes.offline --> | MEASURED | `planted-bugs` |
| Judge truncates tool results: offline tests failing | 3 <!-- fact:planted.judge_truncates_tool_results.offline --> | MEASURED | `planted-bugs` |
| Judge skips the overflow check: offline tests failing | 2 <!-- fact:planted.judge_skips_overflow_check.offline --> | MEASURED | `planted-bugs` |
| Judge imports the state diff: offline tests failing | 1 <!-- fact:planted.judge_imports_state_diff.offline --> | MEASURED | `planted-bugs` |
| Manual spot-check agreement with the failure taxonomy | NOT YET MEASURED | NOT YET MEASURED | Needs `docs/failure-taxonomy.md` |

### Smoke-run facts

From the 2026-10-19 smoke runs (`pytest -m smoke -rf`), cited from the
`smoke-runs` block of `docs/experiment-log.md`; each is one billed call per
condition, "passed" meaning the smoke test passed. They show a
request is accepted and its reply parses; they measure no model behaviour.

| Fact | Value | Status | Source |
|---|---|---|---|
| `judge-gpt-5.1` (`gpt-5.1-2025-11-13`) accepts temperature 0 | passed <!-- fact:smoke.judge_gpt_5_1_t0 --> | MEASURED | smoke `test_judge_returns_a_parseable_verdict[judge-gpt-5.1-t0]` |
| `judge-gpt-5.1` accepts temperature 0.7 | passed <!-- fact:smoke.judge_gpt_5_1_t0_7 --> | MEASURED | smoke `...[judge-gpt-5.1-t0.7]` |
| `deepseek-v3.2` via OpenRouter accepts the 16,000-token agent cap | passed <!-- fact:smoke.model_deepseek_v3_2 --> | MEASURED | smoke `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[deepseek-v3.2]` (one host, as routed that day) |
| A second call carrying the first reply back verbatim is accepted: `claude-opus-5.5` | passed <!-- fact:smoke.model_claude_opus_5_5 --> | MEASURED | smoke two-turn test `[claude-opus-5.5]` |
| The same: `gemini-4-argon` | passed <!-- fact:smoke.model_gemini_4_argon --> | MEASURED | smoke two-turn test `[gemini-4-argon]`, after the 5.20 fix |
| The same: `gemini-3.5-flash-lite` | passed <!-- fact:smoke.model_gemini_3_5_flash_lite --> | MEASURED | smoke two-turn test `[gemini-3.5-flash-lite]`, after the 5.20 fix |
| Structured-output verdict from the primary judge (`judge-claude-opus-5.5`, no temperature sent) parses | passed <!-- fact:smoke.judge_claude_opus_5_5_provider_default --> | MEASURED | smoke `...[judge-claude-opus-5.5-provider_default]` |

What the replay rows don't show: the run that produced them didn't assert
that the first reply contained thinking blocks or thought signatures
(adaptive thinking can skip thinking on a simple request), so "accepted" is
proven, "reasoning state round-tripped" is not. The smoke test now asserts
it, and skips rather than passes when a reply carries none; it hasn't been
run in that form. The at-scale figure is 3.18.

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
- **READY** Made grading reproducible from a single results file with no database or model access, verified by planting a grader bug that 27 <!-- fact:planted.reads_live_environment.offline --> offline tests catch.
- **READY** Built a test suite of 757 <!-- fact:tests.offline --> offline and 332 <!-- fact:tests.live --> live tests, with CI running the live suite against a Postgres service container on every push.
- **DRAFT** Measured how often agents claim success the database contradicts (verification gap: NOT YET MEASURED) across the registry's models, by grading final state instead of transcripts.
- **DRAFT** Showed the difference between one-shot and every-time reliability (pass@1 vs pass^k: NOT YET MEASURED), by running repeated trials per task and model.
- **DRAFT** Quantified collateral damage (rate: NOT YET MEASURED) by classifying every out-of-scope row change as benign or destructive.
- **DRAFT** Separated justified refusals from silence and false claims among trap-task passes the database could not distinguish (split: NOT YET MEASURED), by pairing a state grader with a transcript-only LLM judge.

## 9. Explicitly NOT claimable

- **Any result about real models.** No episode has been run against a real
  model. Every finding in section 3 is NOT YET MEASURED.
- **"Supports Anthropic, OpenAI, Google and OpenRouter" as more than
  wiring.** Every smoke test has passed once against the real APIs
  (2026-10-19, `docs/experiment-log.md`): each request is accepted and its
  reply parses. That is a minimal call per condition, not a run: nothing
  about reliability over hundreds of episodes, rate limits or behaviour.
  Claim "smoke-tested against each provider", not "validated at scale".
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
- **Any judge result.** Each judge has answered one smoke-test transcript,
  but no real episode has been judged. Its accuracy, its agreement figures
  and its self-preference are all unmeasured.
- **"`judge_api_agreement` shows the judge is consistent."** It measures API
  nondeterminism at temperature 0, not reliability. Only the temperature-0.7
  stability subset speaks to reliability.
- **"Self-preference bias is handled."** The cross-family judge is a design
  that can measure it; nothing has been measured.
- **"Every context window is verified."** They are taken from vendor
  documentation, checked 2026-10-14 and 2026-10-15 and cited in
  `models.yaml`; no call has tested them. Every pinned model ID has answered a
  real smoke-test call (2026-10-19).
- **"Reasoning state round-trips" for any model.** Not yet shown: the smoke
  run accepted a second call carrying the first reply back, but didn't check
  the first reply held any reasoning state. The claim waits for 3.18
  (carried, sent back and accepted turns per model, from the full run), and
  for the smoke test in its new form, which skips when there is nothing to
  round-trip.
- **The judge saw the same agent text for every model.** Opus 5.5's notes
  between tool calls are now returned (`display: "updates"`), but they are
  summaries and it may skip any of them, and every model narrates as much as
  it chooses. How much each model gave the judge is `narration_chars`, not
  yet measured; quote it beside any per-model gap.
- **"No model was truncated."** The cap is 16,000 tokens per agent turn for
  every model, but a turn can still reach it; how many episodes did is
  `output_capped_episodes`, not yet measured. DeepSeek V3.2's maximum output
  through OpenRouter isn't documented on its OpenRouter pages; it accepted
  16,000 in the smoke run, but OpenRouter routes between hosts, so that
  shows one host's limit, not every host's.
- **"Every model ran at the same reasoning effort."** Each runs at its
  provider's default, and the defaults differ (Opus 5.5 `medium`, GPT-5.1
  `none`, Gemini 4 Argon `medium`, Gemini 3.5 Flash-Lite `minimal`,
  DeepSeek undocumented; `docs/models.md`).
- **"Showing Opus 5.5's progress updates doesn't change its behaviour" as
  tested.** It rests on Anthropic's documentation (`display` "controls how
  thinking content is returned"; the signature is identical whichever value
  is set). Nothing here measures it.
- **One price for `gemini-4-argon` across the whole run.** Google doubles it
  on December 1, 2026; a run on or after that date needs `models.yaml`
  updated first.
- **"The primary judge's verdicts are temperature 0" or "its agreement is API
  determinism".** It runs at the provider's default sampling; its vote
  agreement is sampling stability.
- **"GPT-5.1's temperature is documented."** OpenAI's model page is silent
  on sampling parameters; that `judge-gpt-5.1` accepts 0 and 0.7 rests on the
  2026-10-19 smoke run, not on documentation.
- **"Self-preference is ruled out."** The primary judge is the same model as
  one model under test (`claude-opus-5.5`), the strongest form of the risk;
  only the cross-family comparison can show its size.
- **The failure taxonomy.** It doesn't exist.
- **A verification gap without its n and sensitivity bound.** When any
  episode lacks a verdict, the paired gap alone can mislead: in the metrics
  tests, six judge errors out of ten turn a paired gap of -50 points into a
  bound of -30 to +30. Quote the bound with the gap.
- **A Wilson interval on pass^k.** pass^k is a mean of per-task estimates;
  its interval is a bootstrap over tasks, not Wilson. Say which.
- **Mutation checks on the metrics and report tests.** Four bugs were planted
  in `metrics.py` and four in `report.py` by hand while they were written, and
  each was caught, but the outputs aren't stored; add them to
  `consequence/planted_bugs.py` before citing a count.
- **Lines of code, or speed and cost of a full run.** Not measured.
- **The do-nothing agent's score on traps as a finding.** It is a property of
  the task design, shown with a mock agent, not a measured model behaviour.

## 10. Ledger changelog

| Date | Commit | Change |
|---|---|---|
| 2026-10-22 | `smoke-run fixes` | Every adapter reports and every agent turn stores `reasoning_state`; the smoke two-turn test now asserts reasoning state for Opus 5.5 and the Gemini models and skips ("no reasoning produced; round trip unproven") when there is none; added 3.18 (reasoning round trips, metric 22), an engineering decision, and replaced the replay guard in section 9: the round-trip claim waits for 3.18. |
| 2026-10-19 | `smoke-run fixes` | First smoke run: added defect 5.20 (Gemini rejected every tool schema, fixed by sending it unchanged as `parameters_json_schema`; why the offline SDK tests couldn't catch it), smoke-run facts in section 6, `tests/test_tool_arguments.py` (undeclared arguments refused for every vendor), marked 5.14, 5.16 and 5.20 proven against the real APIs and 5.15 partly, recorded both smoke runs in `docs/experiment-log.md`, and rewrote five section 9 guards that said the smoke suite had never run. |
| 2026-10-17 | `final prep` | One agent output cap of 16,000 tokens for every model under test, and every agent at its provider's default reasoning settings, both recorded as experimental conditions. Added 3.17 (episodes cut off by the cap, metric 21), two engineering decisions, defect 5.19 (a 4,096 cap with thinking, and Gemini cut-offs hidden by the Google adapter) and two section 9 guards. |
| 2026-10-15 | `final prep` | Primary judge `claude-opus-5-5` at the provider's default sampling, chosen as the strongest available (supersedes `claude-opus-4-6`): rewrote 3.6 and 3.7 for sampling stability, added 3.16 (visible narration, metric 20) and five engineering decisions, replaced the "chosen for its controls" decision, updated 5.14 to 5.16 to their commit, added defects 5.17 and 5.18 (found by the new offline SDK tests), and rewrote four section 9 guards. Smoke tests 9 to 8: the primary judge runs under one condition. Planted bugs re-measured: the judge-skips-errored-episodes bug is now caught by 11 offline tests, up from 10 (the new provider-default test). |
| 2026-10-15 | `f399150` | Final lineup before `experiment-v1`: `claude-opus-5.5` replaces Opus 5, `gemini-4-argon` (stable) replaces the 3.1 Pro preview, `gemini-3.5-flash-lite` replaces Claude Haiku 4.5; primary judge `claude-opus-4-6`, because current Claude models reject any non-default temperature. Added served-version provenance (3.15, two metrics), provider-native replay, defects 5.14 to 5.16, three engineering decisions, and replaced the Haiku and Gemini-preview guards in section 9 with four current ones. |
| 2026-10-14 | `7c19937` | Pinned Claude Haiku 4.5 to its dated snapshot and set both judge context windows from cited vendor pages; the smoke suite now covers each judge at both temperatures and each model with tools; the four judge mutations are stored in the planted-bug table; single-vote judging with an API-agreement sample, dry-run judging estimates and an overflow preflight. Added defects 5.11 (a floating alias), 5.12 (the OpenRouter key) and 5.13 (planted bugs measured against a red baseline), two engineering decisions, the 3.6 source rule, and replaced two section 9 guards with three current ones. |
| 2026-10-13 | `60c72a6` | `consequence/report.py` now lays out `consequence/metrics.py` per model and computes nothing; added the judging cost (3.13) and episode outcome (3.14) definitions, two metrics behind them, the "report computes nothing" engineering decision, and defect 5.10 (the old report pooled every model). Metric count 15 to 17. |
| 2026-10-13 | `0d1d696` | Added `consequence/metrics.py` as the only source of reported numbers. Rewrote section 3 so every definition matches the code and names its `RunMetrics` call: new definitions for the trap behaviour split (3.10), cross-judge agreement over the intersection (3.8) and grading coverage (3.11), plus time and turns (3.12). Corrected three earlier definitions to match the metrics as specified: the verification gap is a difference in percentage points, not a share of judge passes; collateral is over state passes only; cost per solved task is per category, traps included. Added the no-verdict sensitivity bound and the single metrics module to section 4, a scale fact, and section 9 guards. |
| 2026-10-11 | `0be69bc` | Added the LLM judge: scale facts (judge models), section 3 definitions for the verification gap, judge API agreement, stability, cross-family comparison and the new 3.10 trap behaviour split, three engineering decisions, the reframed adversarial row (structural limits), a DRAFT bullet, and section 9 guards. Corrected 3.6, which wrongly defined judge API agreement as judge-vs-state agreement. |
| 2026-10-11 | `6d92c81` | Created. Backfilled from every commit, `a033e8e` through `c66c78d`, and the hardening work in progress. Added the ledger check (`consequence/ledger.py`) and the planted-bug harness whose output section 6 cites. |
