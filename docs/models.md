# Model registry

`consequence/models.yaml` is the source of truth for every model the benchmark
can run: its exact pinned API version string, and the prices used to compute
`cost_usd` (from actual token counts -- never estimated). See the comment at
the top of that file for the rules (no floating aliases, `id` vs `api_model`).

## The 5 models and why each was picked

The lineup is chosen to span a real spread of capability, price, and vendor,
not five variations on one tier:

- **`claude-opus-5`** (Anthropic, frontier) -- the reference point this whole
  project is built closest to; Anthropic's current top-tier model, used
  elsewhere in this codebase's own tooling. Pinned to the bare model ID
  `claude-opus-5` -- Anthropic does not use dated snapshots for current-gen
  models; that bare string *is* the pin (never append a date suffix to it).
  $5.00 / $25.00 per 1M input/output tokens.

- **`gpt-5.1`** (OpenAI, frontier) -- the natural frontier counterpart on the
  other major first-party API, so the benchmark isn't Anthropic-only at the
  top end. Pinned to the dated snapshot `gpt-5.1-2025-11-13` rather than the
  bare `gpt-5.1` alias, since OpenAI does publish dated snapshots for this
  model and a snapshot is a harder pin than a family name. $1.25 / $10.00
  per 1M.

- **`gemini-3.1-pro`** (Google, frontier-tier) -- the third first-party lab,
  and Gemini's tool-calling shape (`function_declarations`, "model" role
  instead of "assistant", no separate tool role) is different enough from
  the other two that it genuinely exercises the adapter interface rather
  than being a copy of the OpenAI translation. Pinned to
  `gemini-3.1-pro-preview` -- Google's own docs classify this as "preview",
  not the unstable "experimental" tier, and it's still an exact model
  string, not a `-latest` alias. $2.00 / $12.00 per 1M (standard tier,
  prompts <=200k tokens).

- **`claude-haiku-4.5`** (Anthropic, small/cheap) -- the cost floor of the
  lineup, so cheap-model failure modes (skipping tool calls, giving up
  early, shallower tool-argument reasoning) show up in the results
  alongside frontier behavior. Reusing Anthropic here (rather than a fourth
  vendor) was deliberate: it isolates the small/cheap effect from a
  vendor-family effect, since `claude-opus-5` is also Anthropic. $1.00 /
  $5.00 per 1M.

- **`deepseek-v3.2`** (open-weights, via OpenRouter) -- the open-weights
  representative, so the lineup isn't all closed frontier labs. Routed
  through OpenRouter's OpenAI-wire-compatible endpoint as `deepseek/deepseek-v3.2`.

  **This one entry needs a live check before it's used for real spend.**
  Every other price in `models.yaml` was read from that vendor's own current
  pricing page; OpenRouter's live pricing endpoint
  (`GET https://openrouter.ai/api/v1/models`) was not queried while writing
  this registry, so `deepseek-v3.2`'s prices are a best-effort placeholder,
  flagged as such in `models.yaml` itself. Confirm the model slug still
  resolves and pull its current per-token prices from that endpoint (or
  OpenRouter's `/models` page) before running anything that spends money on
  it.

## Pricing sources and dates

All prices except `deepseek-v3.2` (see above) were read from each vendor's
own current API documentation/pricing page in September 2026, for the exact
`api_model` string in the registry (standard tier, text tokens, prompts at or
under any tiering threshold the model has). Re-check `models.yaml`'s prices
periodically -- vendors change prices without changing the model string.

## Retry policy

Every adapter retries only on HTTP 429 (rate limit) and 5xx (server error),
with exponential backoff plus jitter (`consequence/providers/retry.py`).
Any other error (4xx other than 429, a network error the SDK doesn't
classify as retryable, a translation bug) is not retried -- it propagates up
and the episode is recorded with `status='error'`. A *completed* turn is
never retried; retries only ever wrap the transport call that produces one.
The number of retries a turn took is recorded on `TurnResult.retry_count`
and summed onto the episode row as `episodes.retry_count`.

## Running a smoke test

Each adapter has exactly one opt-in `@pytest.mark.smoke` test in
`tests/test_providers.py` -- a single trivial, billed call with no tools, to
confirm credentials and wiring work. These are excluded by default
(`pytest`'s default `addopts` excludes both `live` and `smoke`). To run one:

```bash
pip install -e ".[providers]"   # anthropic, openai, google-genai
export ANTHROPIC_API_KEY=...    # or OPENAI_API_KEY / GOOGLE_API_KEY / OPENROUTER_API_KEY
pytest -m smoke -k anthropic    # or openai / google / openrouter
```

Each smoke test call is real and billed at that model's own rates -- expect
a fraction of a cent per run, but it is not free. No experiment (a full set
of tasks x models x trials) is run in this commit; that's a separate,
later stage that reuses `consequence.harness.run_episode` with a real
provider from `consequence.providers.registry.make_provider`.
