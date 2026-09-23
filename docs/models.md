# Models

The registry is `consequence/models.yaml`. This page gives the five models it
pins, why each one is there, and how the adapters behind them turn a
provider's reply into an episode record.

## The five models

| Registry id | Provider | Exact API model string | $/M in | $/M cached in | $/M out | Role in the spread |
|---|---|---|---|---|---|---|
| `claude-opus-5` | Anthropic | `claude-opus-5` | 5.00 | n/a | 25.00 | frontier Anthropic |
| `gpt-5.5` | OpenAI | `gpt-5.5-2026-04-23` | 5.00 | 0.50 | 30.00 | frontier OpenAI |
| `gemini-3.1-pro` | Google | `gemini-3.1-pro-preview` | 2.00 | 0.20 | 12.00 | Google |
| `claude-haiku-4.5` | Anthropic | `claude-haiku-4-5-20251001` | 1.00 | n/a | 5.00 | small / cheap |
| `qwen3-235b-a22b-2507` | OpenRouter → DeepInfra | `qwen/qwen3-235b-a22b-2507` | 0.09 | n/a | 0.35 | open weights |

Long-context tiers (the whole request moves to the higher tier once its
prompt passes the threshold): GPT-5.5 above 272k input tokens is billed at
$10 / $1.00 cached / $45; Gemini 3.1 Pro above 200k at $4 / $0.40 cached /
$18. This benchmark's episodes are nowhere near either threshold. The tiers
are pinned anyway so the cost stays exact if one ever does.

### Why each one

- **`claude-opus-5` (frontier Anthropic).** This is Anthropic's
  generally available Opus-tier model. Anthropic's current-generation
  ids have no dated variant: the string *is* the snapshot, and no alias
  sits behind it. The top tier, `claude-fable-5-1`, costs twice as much
  ($10/$50) and has different API semantics (thinking can't be turned
  off, and forced tool choice is rejected). That would make it the odd
  one out among the frontier entries, so Opus is the frontier pick.
  Effort is pinned to `high` in `params` rather than left to the API
  default. **Server-side refusal fallbacks are deliberately left off.**
  With them on, a refused request is silently re-run on a different
  model, and the episode row would name a model that didn't produce the
  turn. A refusal is recorded as `stop_reason: "refusal"` in the raw
  response instead.
- **`gpt-5.5-2026-04-23` (frontier OpenAI).** This is OpenAI's current
  frontier general model, and it is pinned to its dated snapshot because
  the bare `gpt-5.5` is an alias that moves. The registry refuses any
  OpenAI string without a `-YYYY-MM-DD` suffix. `reasoning_effort` is
  pinned to `medium`. It is called through Chat Completions (see
  "Adapter notes").
- **`gemini-3.1-pro-preview` (Google).** This is Google's Pro-tier
  reasoning model. The Gemini 2.5 line was avoided because it is due to
  shut down on 2026-10-16, which is before an experiment on it could be
  replicated. Google's `-latest` names are aliases and are rejected.
  Every Gemini response reports the version that served it
  (`modelVersion`), and that field is stored in the raw response.
- **`claude-haiku-4-5-20251001` (small / cheap).** This is a fully
  dated snapshot and costs 1/5 of Opus. Using an Anthropic model as the
  small model gives a within-vendor size contrast with Opus, so a
  difference between them can't be put down to a different API or a
  different lab's tool-calling conventions.
- **`qwen/qwen3-235b-a22b-2507` via OpenRouter (open weights).** The
  weights are Apache-2.0 and public (235B total parameters, 22B active),
  the checkpoint is dated in its name, it has native tool calling, and
  it comes from a third lab. On OpenRouter one slug can be served by many
  hosts, each with its own quantization and price. The entry therefore
  pins `routing: {order: [deepinfra], allow_fallbacks: false,
  require_parameters: true}`, and the registry refuses any OpenRouter
  entry without `allow_fallbacks: false`. Each raw response records the
  host that actually served it (`provider`) and what OpenRouter billed
  (`usage.cost`).

### Verification status: check this before the first paid run

These choices were made in a sandbox with no provider API keys. Its network
policy also blocked every provider's pricing and models pages.

- **Checked against a reference source:** the two Anthropic strings and
  their prices.
- **Taken from secondary sources (search results) and not yet confirmed
  against the provider:** the OpenAI, Google and OpenRouter strings and
  prices, in particular the `gpt-5.5` snapshot date, whether the Gemini
  model is still preview-only, and DeepInfra's price for Qwen.

Run `make test-smoke` before any experiment. It is built to fail when a
string or price is wrong:

- a wrong model string fails the call (404);
- a model string the provider quietly resolves to a different version
  fails the "served model equals the pinned string" assertion;
- for OpenRouter, a wrong pinned price fails the comparison against the
  cost OpenRouter reports it billed.

If a value turns out to be wrong, fix `models.yaml` and this page in the
same commit.

## Rules the registry enforces

`consequence/providers/registry.py` checks each entry on load:

- `api_model` must be an exact, pinned string. It is rejected if it
  contains `latest`. An OpenAI string must end in `-YYYY-MM-DD`. An
  OpenRouter string must not be a routing variant (`:free`, `:nitro`,
  `:floor`, `openrouter/auto`).
- The adapter's `model_id` is the `api_model` string. `harness.run_episode`
  writes `provider.model_id` onto every episode row, so each row carries
  the exact pinned string.
- Each entry holds `id`, `provider`, `api_model`, `input_price_per_mtok`,
  `output_price_per_mtok` and `max_output_tokens`. Optional fields:
  - `cached_input_price_per_mtok` and `cache_write_price_per_mtok`;
  - `long_context`;
  - `max_turns`, a per-model override of the harness default of 20. None
    of the five needs one yet;
  - `params`, merged into every request body as-is;
  - `routing`, which is OpenRouter-only and required there.

## Cost

`cost_usd` is computed; it is never estimated. For each turn:

```
cost = uncached_input  x input_price
     + cached_input    x cached_input_price
     + cache_writes    x cache_write_price
     + output          x output_price        (all / 1,000,000)
```

- **Token counts** come from the provider's own `usage` block in that
  turn's response.
- **Output** includes reasoning/thinking tokens. Every provider here bills
  those as output. Gemini reports them separately (`thoughtsTokenCount`),
  and the adapter adds them in.
- **Cached input:** OpenAI and Gemini cache prompt prefixes automatically
  and bill cached tokens at a discount. An agent loop resends a growing
  prefix every turn, so this is common. It is why those entries carry a
  cached-input price.
- **The arithmetic** is done in `Decimal`.
- **The episode's `cost_usd`** is the exact sum of its turns' costs.
- **Missing prices:** if a turn bills tokens in a category that has no
  pinned price, that turn's cost is `None` and so is the episode's. A
  stored NULL means "unknown". The pipeline never falls back to a guess.
- **Offline re-pricing:** the raw responses (and their usage blocks) are
  stored, so cost can be recomputed offline if a price is ever corrected.

## Retries

- **What is retried:** HTTP 429 and 5xx only, with exponential backoff
  (2s, 4s, 8s, ... capped at 60s, equal jitter, `Retry-After` honoured as
  a floor). The default is at most 6 retries.
- **What is never retried:** any other 4xx. A transport error (timeout,
  dropped connection) is not retried either: it may mean the provider
  completed and billed the turn, and a retry would silently run the turn
  twice.
- **A completed turn is never retried.** Any 2xx response ends the retry
  loop. If a 2xx body is unusable (no choices, no candidates, an error
  object), that is raised, not retried.
- **Where retries are recorded:** each `TurnResult.retries`, and the
  episode's `retry_count`, which also counts retries spent on a turn that
  finally failed.

## Raw responses

Each adapter keeps the response body exactly as received, as a string of the
bytes the provider sent. `consequence/runner.py` writes it to
`transcripts.raw_response` on the matching assistant row, so any later
analysis (thinking content, served-model versions, safety metadata,
provider-reported cost) works from stored data and never needs a model
re-run.

## How recording works without touching the harness

`harness.run_episode` records turns, tool calls and the state diff. It knows
nothing about pricing or HTTP, and it was not changed. The adapters instead
implement a small addition to the provider interface
(`RecordingProvider` in `providers/base.py`):

1. `start_episode()` clears the adapter's ledger.
2. The harness drives `run_turn()` as usual. Each returned `TurnResult`
   also carries `raw_response`, `retries` and `cost_usd`, and is kept in
   the ledger.
3. `finish_episode()` returns the ledger (`EpisodeUsage`).

`runner.run_recorded_episode()` brackets `run_episode()` with steps 1 and 3.
It then calls `Results.record_provider_usage()`, which writes `cost_usd` and
`retry_count` onto the episode row and each raw response onto its assistant
transcript row. The ledger has exactly one entry per assistant row, and the
write is refused if the counts disagree. An experiment driver calls
`run_recorded_episode`, not `run_episode`. It also applies the model's
`max_turns` override.

## Adapter notes

All four adapters call their API over plain HTTP (`httpx`), not vendor SDKs:

- The SDKs retry by themselves, including on timeouts, and that would
  break the retry rule above.
- The raw body is available exactly as sent.

Translation, per provider:

| | Tool definition | Tool calls come back as | Results go back as |
|---|---|---|---|
| Anthropic | `{name, description, input_schema}` | `tool_use` blocks | `tool_result` blocks, all in one user message, `is_error` when the tool returned `ok: false` |
| OpenAI / OpenRouter | `{type: function, function: {name, description, parameters}}` | `message.tool_calls`; arguments are a JSON string | one `role: tool` message per call |
| Google | `functionDeclarations`; the schema is converted to Gemini's OpenAPI subset (upper-case types, no `additionalProperties`) | `functionCall` parts | `functionResponse` parts in one user turn |

Tool and parameter descriptions pass through byte-identical for every
provider (see docs/mcp.md), and a test asserts it.

**Native replay.** Some APIs need provider-only state sent back with a
tool call:

- Anthropic thinking-block signatures;
- Gemini `thoughtSignature`;
- OpenRouter `reasoning_details`.

The harness's neutral history can't carry these. Each adapter therefore
remembers the exact assistant content it received, keyed by the turn's
tool-call ids, and replays it instead of rebuilding the message.

**OpenAI uses Chat Completions.** Reasoning items are not carried between
turns there, so GPT-5.5 reasons afresh each turn. This is the standard
function-calling surface. Moving to the Responses API (with encrypted
reasoning items) would be a new experimental condition, not a bug fix.

**Malformed tool arguments.** If a model emits tool arguments that aren't
valid JSON, the adapter passes them to the tool layer under
`__unparseable_arguments__`. Validation rejects that key, so the model
gets an ordinary `ok: false` result, and the raw string is kept.

## Adding a model

Add an entry to `models.yaml`, then add it to the table and the reasons
above. Then run `make test-smoke` (or a one-off smoke call for that entry)
before using it in an experiment.
