# Model registry

`consequence/models.yaml` is the source of truth for every model the benchmark
can run: its exact pinned API version string, and the prices used to compute
`cost_usd` (from actual token counts -- never estimated). See the comment at
the top of that file for the rules (no floating aliases, `id` vs `api_model`).
Every `api_model`, price and context window there cites the vendor page it
came from and the date it was checked. The lineup below was finalized on
2026-10-15, before any run; it is frozen at the `experiment-v1` tag.

## The 5 models and why each was picked

The lineup spans a real spread of capability, price and vendor, not five
variations on one tier.

- **`claude-opus-5.5`** (Anthropic, frontier; `claude-opus-5-5`) -- the current
  Opus ("Latest. Released September 22, 2026"), replacing `claude-opus-5`,
  which Anthropic now lists as legacy. Dateless but pinned: "Every Claude model
  ID is a pinned snapshot, including the dateless IDs used from the 4.6
  generation on." $4.00 / $20.00 per 1M input/output, 20% below Claude Opus
  5's $5 / $25. Thinking is always on; three of its documented breaking
  changes touch this project (see "Claude Opus 5.5 in the agent loop").
  Source: https://platform.claude.com/docs/en/models/opus-5-5/overview

- **`gpt-5.1`** (OpenAI, frontier) -- the frontier counterpart on the other
  major first-party API. Pinned to the dated snapshot `gpt-5.1-2025-11-13`,
  not the bare `gpt-5.1`. $1.25 / $10.00 per 1M.

- **`gemini-4-argon`** (Google, frontier; `gemini-4-argon`) -- Google's
  "flagship reasoning model", replacing `gemini-3.1-pro-preview`. Google now
  documents a stable code at this tier ("Stable: `gemini-4-argon`"), and a
  stable code is a firmer pin than a preview: "Stable: Points to a specific
  stable model. Stable models usually don't change", while a preview "may be
  used for production" with no such statement. Gemini's tool-calling shape
  (`function_declarations`, a "model" role, no tool role) exercises the
  adapter interface rather than copying the OpenAI translation. Its price
  changes mid-quarter: "$2.00 through November 30, 2026" and "$4.00 starting
  December 1, 2026" input, $10.00 / $20.00 output (output includes thinking
  tokens). `models.yaml` carries the price until November 30; a run on or after
  December 1 needs it updated first, and the dry run says so.
  Sources: https://ai.google.dev/gemini-api/docs/models/gemini-4-argon,
  https://ai.google.dev/gemini-api/docs/pricing

- **`gemini-3.5-flash-lite`** (Google, small and low-cost;
  `gemini-3.5-flash-lite`) -- the cost floor, so cheap-model failure modes
  (skipping tool calls, giving up early, shallow argument reasoning) show up
  beside frontier behaviour. "A low-latency, cost-effective multimodal model",
  stable code, function calling supported. $0.30 / $2.50 per 1M. It replaces
  `claude-haiku-4.5` (`claude-haiku-4-5-20251001`) for two reasons:
  - Claude Haiku 4.5 is legacy with retirement "Not sooner than October 15,
    2026", so it could vanish mid-run.
  - It cuts the models under test that share a family with the primary judge
    (Anthropic) from two to one, which shrinks the judge's self-preference
    risk. It also avoids OpenAI, the cross-family judge's family.
  Sources: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite,
  https://ai.google.dev/gemini-api/docs/pricing

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

## The judges

- **Primary: `judge-claude-opus-5.5`** (`claude-opus-5-5`), at the
  provider's default sampling: no temperature is sent, because on Claude
  Opus 4.7 and later "non-default `temperature`, `top_p`, or `top_k` values
  return a 400 error on every request". The judge must be the strongest
  available: an older, more credulous judge would inflate the verification
  gap and make the headline look like a strawman, and choosing a judge to
  suit a sampling parameter gets the priority backwards. It replaces
  `claude-opus-4-6`, which had been chosen because it accepts a temperature.
  Its three votes per episode are its stability measurement; it answers
  through structured outputs, since it rejects forced tool use; output cap
  16,000 tokens, thinking included. $4 / $20 per 1M, 1M-token context window.
  Details and sources: `docs/judge-methodology.md`.
  Source: https://platform.claude.com/docs/en/models/opus-5-5/overview
- **Cross-family: `judge-gpt-5.1`** (`gpt-5.1-2025-11-13`), unchanged. Whether
  it accepts a temperature is unverified; the smoke tests decide. If it
  doesn't, set `sampling: provider_default` for it in `models.yaml`.

## Claude Opus 5.5 in the agent loop

From https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5:

- **Thinking blocks must be replayed.** "pass `thinking` blocks back
  unmodified in tool-use loops". The Anthropic adapter now returns the
  response's own content blocks and sends them back verbatim as that
  assistant turn (`TurnResult.replay`). Gemini needs the same ("You MUST
  always resend all thought blocks exactly as they were received", with
  signatures on the function-call parts), and the Google adapter does the
  same with its response `Content`.
- **Forced tool use returns an error.** The agent never forces tool use (it
  uses the default `auto`). As the primary judge, Opus 5.5 answers through
  structured outputs instead of a tool call (`docs/judge-methodology.md`).
- **Text between tool calls comes back in `thinking` blocks**, with empty
  text at the default `display: "omitted"`, where other models write those
  notes as text. As an agent, Opus 5.5 runs with `thinking.display:
  "updates"` (beta; the adapter sends header
  `thinking-display-updates-2026-08-18`), which returns the progress updates
  while reasoning stays hidden. Anthropic documents `display` as changing only
  what is returned ("The `signature` field is identical whichever `display`
  value you set"). The adapter keeps each update in the turn's text, so the
  judge reads it as it reads other models' notes. What asymmetry remains is
  measured per model (`narration_chars`); see the Limitations section of
  `docs/judge-methodology.md`.
  Source: https://platform.claude.com/docs/en/build-with-claude/thinking#controlling-thinking-display
- **Output cap.** Thinking counts toward the cap, so agents run at a uniform
  16,000 tokens per turn; see "Experimental conditions".

## Experimental conditions

Two conditions hold for every model under test, fixed in `models.yaml` and
the adapters so they can't differ between the models being compared.

**One output cap: 16,000 tokens per agent turn** (`agent_max_output_tokens`;
a per-model cap is refused when `models.yaml` loads). Thinking counts toward
it on the reasoning models here: Google's `max_output_tokens` "sets the
maximum number of tokens a response can generate, including thought tokens",
and on Claude "Thinking counts toward `max_tokens` even when thinking content
isn't returned to you" (prompting guide for Opus 5.5). A reasoning model that spends a small cap on thinking
returns no tool call and is scored as failing, for a reason unrelated to its
behaviour; the cap used to be the adapters' default, 4,096, which risked
exactly that. 16,000 is below every documented maximum output (Claude Opus
5.5 128K, GPT-5.1 128,000, Gemini 4 Argon and Gemini 3.5 Flash-Lite 65,536)
and below the Anthropic SDK's non-streaming limit (21,333). DeepSeek V3.2's
limit through OpenRouter isn't documented on its OpenRouter pages; the smoke
test is what checks it. Only generated tokens are billed, so a higher cap
costs nothing unless it is used.

Every agent turn stores why it ended (`transcripts.stop_reason`, the
provider's own words, and `output_capped`, each adapter's mapping of its
vendor's cap stop: Anthropic `max_tokens`, OpenAI and OpenRouter `length`,
Gemini `MAX_TOKENS`). `RunMetrics.output_capped_episodes(model)` counts the
episodes with any turn cut off by the cap, per model and category, and
`consequence report` shows it: residual truncation is measured, not assumed
away. Not yet measured.

**Every agent runs at its provider's default reasoning settings.** No
adapter sends an effort, reasoning-effort, thinking-level or thinking-budget
parameter for an agent. What that default is, per vendor documentation:

| Model | Default reasoning, as documented | Source |
|---|---|---|
| `claude-opus-5.5` | thinking always on, effort `medium` ("Default effort: `medium`") | https://platform.claude.com/docs/en/models/opus-5-5/overview |
| `gpt-5.1` | "Reasoning.effort supports: none (default), low, medium, and high" | https://developers.openai.com/api/docs/models/gpt-5.1 |
| `gemini-4-argon` | thinking "On (medium)" | https://ai.google.dev/gemini-api/docs/thinking |
| `gemini-3.5-flash-lite` | thinking "On (minimal)" | https://ai.google.dev/gemini-api/docs/thinking |
| `deepseek-v3.2` | not documented on its OpenRouter pages | https://openrouter.ai/deepseek/deepseek-v3.2 |

Checked 2026-10-17. Claude Opus 5.5's `thinking.display: "updates"` is not a
reasoning setting: it changes what is returned, not how the model thinks
(see "Claude Opus 5.5 in the agent loop").

## Served-version provenance

A pinned `api_model` names what was asked for, not necessarily what served
the call. Every adapter reads the serving model from its own raw response
(`served_model_from_raw`: Anthropic and OpenAI `model`, Google
`model_version`, OpenRouter `model@provider`, since the same open-weights
model can be routed to different hosts). Each episode and each judge grading
stores it as `served_model`. If one configured model is served by more than
one version within a run, `consequence grade` warns and records it in
`docs/experiment-log.md`, and `consequence report` leads with the warning;
grading is never blocked. This is also the mitigation for any code a vendor
may repoint.

## Pricing sources and dates

Every price except `deepseek-v3.2` (see above) was read from the vendor's own
current documentation or pricing page for the exact `api_model`, standard
tier, text tokens. The date each was checked is beside it in `models.yaml`.
Re-check before a run: vendors change prices without changing the model
string, and `gemini-4-argon`'s is scheduled to.

## Retry policy

Every adapter retries only on HTTP 429 (rate limit) and 5xx (server error),
with exponential backoff plus jitter (`consequence/providers/retry.py`).
Any other error (4xx other than 429, a network error the SDK doesn't
classify as retryable, a translation bug) is not retried -- it propagates up
and the episode is recorded with `status='error'`. A *completed* turn is
never retried; retries only ever wrap the transport call that produces one.
The number of retries a turn took is recorded on `TurnResult.retry_count`
and summed onto the episode row as `episodes.retry_count`.

## Smoke tests

`tests/test_smoke_providers.py`, excluded by default, billed. Each provider
is built exactly as `grade` and `run` build it (same output caps, same
options):

- each judge under every condition it runs under (the primary judge once, at
  the provider's default sampling; a temperature judge at 0 and at 0.7),
  asserting the verdict parses and the response names its served model;
- each model under test, two turns with the benchmark's tools attached: a
  tool call, then its result and one more call, which is what proves the
  thinking-block and thought-signature replay is accepted.

A missing key or a refused parameter fails with what to change.
`tests/test_provider_sdk.py` makes every one of these calls offline, through
the real pinned SDKs against a fake transport (in CI), so a request an SDK
would refuse fails there for free.

```bash
pip install -e ".[dev,providers]"   # anthropic, openai, google-genai
pytest -m smoke -rf                 # needs every *_API_KEY in .env
```
