# Experiment log

What happened during each real run, beyond what the results database holds:
when it ran, which configuration, and anything that could confound it. No
experiment run has happened yet; the smoke runs below are wiring checks, not
measurements.

## Runs

None yet. Each entry: date, run id, git commit, `models.yaml` at that commit,
judging options, and anything unusual.

## Smoke runs

Billed, minimal calls (`pytest -m smoke -rf`, `tests/test_smoke_providers.py`)
proving that each request is accepted and its reply parses. They store
nothing in a results database and measure nothing about model behaviour.

- **2026-10-19, at `12ad4bb`.** 6 of 8 passed: every judge condition
  (`judge-claude-opus-5.5` at the provider's default with a structured-output
  verdict; `judge-gpt-5.1` at temperature 0 and 0.7) and the two-turn tool
  test for `claude-opus-5.5`, `gpt-5.1` and `deepseek-v3.2`. Both Gemini
  models failed: 400 `Unknown name "additional_properties"` on every tool
  declaration (defect 5.20 in `docs/resume-evidence.md`).
- **2026-10-19, at `12ad4bb` plus the Google adapter fix (tool schemas sent
  as `parameters_json_schema`).** `pytest -m smoke -rf -k gemini`: both
  Gemini two-turn tests passed. The fix changes only the Google adapter, so
  the six earlier passes stand.

Outcome per smoke test, the source the evidence ledger cites (`smoke.*`
facts in `consequence/ledger.py`); `tests/test_ledger.py` checks it names
every smoke test the suite collects, so it can't go stale silently:

<!-- smoke-runs:start -->
| fact | smoke test | outcome | when |
|---|---|---|---|
| judge_claude_opus_5_5_provider_default | `test_judge_returns_a_parseable_verdict[judge-claude-opus-5.5-provider_default]` | passed | 2026-10-22, `348a2ac` |
| judge_gpt_5_1_t0 | `test_judge_returns_a_parseable_verdict[judge-gpt-5.1-t0]` | passed | 2026-10-22, `348a2ac` |
| judge_gpt_5_1_t0_7 | `test_judge_returns_a_parseable_verdict[judge-gpt-5.1-t0.7]` | passed | 2026-10-22, `348a2ac` |
| model_claude_opus_5_5 | `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[claude-opus-5.5]` | passed | 2026-10-22, `348a2ac` |
| model_deepseek_v3_2 | `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[deepseek-v3.2]` | passed | 2026-10-22, `348a2ac` |
| model_gemini_3_5_flash_lite | `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[gemini-3.5-flash-lite]` | passed | 2026-10-22, `348a2ac` |
| model_gemini_4_argon | `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[gemini-4-argon]` | passed | 2026-10-22, `348a2ac` |
| model_gpt_5_1 | `test_evaluated_model_makes_a_parseable_tool_call_and_takes_its_result[gpt-5.1]` | passed | 2026-10-22, `348a2ac` |
<!-- smoke-runs:end -->

Together, every smoke test has passed once against the real APIs, each with
the request exactly as `run` and `grade` build it: the 16,000-token agent
cap, Opus 5.5's `thinking.display: "updates"` beta header, and the second
call carrying the first reply back verbatim.

What it established: `gpt-5.1` accepts temperature 0 and 0.7 as a judge;
`deepseek-v3.2` accepts a 16,000-token cap (on the OpenRouter host it was
routed to); the primary judge's structured-output verdict parses; and the
second call, carrying the first reply back verbatim, was accepted for
Claude Opus 5.5 and both Gemini models. Not established: whether those first
replies contained thinking blocks or thought signatures, which the test
doesn't check. Since then the two-turn test asserts it for Claude Opus 5.5 and
the Gemini models, and skips with "no reasoning produced; round trip
unproven" when a first reply carries none.

- **2026-10-22, at `348a2ac`.** `pytest -m smoke -rf`: 8 passed, 0 skipped
  (summary line "8 passed, 1089 deselected in 70.16s"; pytest reports a
  skipped count whenever any test skips). This is the first run with the
  round-trip assertion, so for `claude-opus-5.5`, `gemini-4-argon` and
  `gemini-3.5-flash-lite` the first reply carried reasoning state (thinking
  blocks; thought signatures) and the second call, carrying it back
  verbatim, was accepted: the reasoning round trip was exercised, once per
  model. The other five passes repeat the earlier results on the committed
  code. The outcomes in the block above are this run's.

## Served-model drift

A pinned `api_model` can still be served by a different model version: a
provider alias, a routed host (OpenRouter records `model@provider`), or a
preview code repointed by its vendor. Every episode and judge grading stores
the model that actually served it (`served_model`, from each response). When
one configured model is served by more than one version within a run,
`consequence grade` warns and records it below, and `consequence report`
leads with the warning. Grading is not blocked; a drifting model's results
must be read with the drift in mind.

<!-- served-model-drift:start -->
None recorded.
<!-- served-model-drift:end -->
