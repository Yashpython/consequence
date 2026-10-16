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

- **Primary: `judge-claude-opus-4.6`** (`claude-opus-4-6`). The judge design
  needs a temperature (three votes at 0, a stability subset at 0.7), and
  Anthropic documents that on Claude Opus 4.7 and later, including Opus 5 and
  Opus 5.5, "non-default `temperature`, `top_p`, or `top_k` values return a 400
  error on every request, regardless of whether thinking is used". On older
  models "the restriction applies only while thinking is on", and Opus 4.6's
  thinking is off unless requested, which the adapter never does. So no
  current Claude model can be this judge; Opus 4.6 is the strongest that can.
  It is legacy, retirement "Not sooner than February 5, 2027". $5 / $25 per 1M,
  1M-token context window.
  Sources: https://platform.claude.com/docs/en/build-with-claude/thinking,
  https://platform.claude.com/docs/en/models/opus-4-6/overview
- **Cross-family: `judge-gpt-5.1`** (`gpt-5.1-2025-11-13`), unchanged. Whether
  it accepts a temperature is unverified; the smoke tests decide.

## Claude Opus 5.5 in the agent loop

From https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5:

- **Thinking blocks must be replayed.** "pass `thinking` blocks back
  unmodified in tool-use loops". The Anthropic adapter now returns the
  response's own content blocks and sends them back verbatim as that
  assistant turn (`TurnResult.replay`). Gemini needs the same ("You MUST
  always resend all thought blocks exactly as they were received", with
  signatures on the function-call parts), and the Google adapter does the
  same with its response `Content`.
- **Forced tool use returns an error.** Nothing here forces tool use; the
  agent and the judge both use the default `auto`.
- **Text between tool calls comes back in `thinking` blocks**, with empty
  text at the default `display: "omitted"`. So the transcript, and the judge,
  see Opus 5.5's final message but not its short notes between tool calls,
  which other models write as ordinary text. This is a known asymmetry in
  what the judge sees for this one model, not yet resolved: setting
  `thinking.display` would change the agent's request for one model only.

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

`tests/test_smoke_providers.py`, excluded by default, billed:

- each judge, once at temperature 0 and once at 0.7, asserting the
  `record_verdict` call parses and the response names its served model;
- each model under test, two turns with the benchmark's tools attached: a
  tool call, then its result and one more call, which is what proves the
  thinking-block and thought-signature replay is accepted.

```bash
pip install -e ".[dev,providers]"   # anthropic, openai, google-genai
pytest -m smoke -rf                 # needs every *_API_KEY in .env
```
