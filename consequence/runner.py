"""Run one episode against a real provider and record everything it cost.

harness.run_episode() records what the harness knows about: the turns, the
tool calls, the state diff. It deliberately knows nothing about pricing or
HTTP retries. This wrapper brackets it with the provider's per-episode
ledger (start_episode / finish_episode) and writes the ledger onto the rows
the harness just recorded: cost_usd and retry_count on the episode, and each
turn's raw response on its assistant transcript row.

The episode row's model_id is the provider's model_id, which for every real
adapter is the exact pinned API model string from models.yaml.
"""

from __future__ import annotations

from typing import Any

from consequence.harness import DEFAULT_MAX_TURNS, EpisodeRecord, Task, run_episode
from consequence.providers.base import Provider
from consequence.providers.common import EpisodeUsage
from consequence.results import Results


def run_recorded_episode(
    task: Task,
    provider: Provider,
    results: Results,
    trial_index: int,
    *,
    max_turns: int | None = None,
    **kwargs: Any,
) -> tuple[EpisodeRecord, EpisodeUsage | None]:
    """run_episode(), plus the provider ledger written to the results DB.

    max_turns defaults to the model's registry override, else the harness
    default. A provider without a ledger (e.g. MockProvider) is run as-is
    and returns None for the usage.
    """
    spec = getattr(provider, "spec", None)
    if max_turns is None:
        max_turns = getattr(spec, "max_turns", None) or DEFAULT_MAX_TURNS

    start = getattr(provider, "start_episode", None)
    if start is not None:
        start()
    record = run_episode(task, provider, results, trial_index, max_turns=max_turns, **kwargs)
    finish = getattr(provider, "finish_episode", None)
    if finish is None:
        return record, None

    usage: EpisodeUsage = finish()
    results.record_provider_usage(
        record.episode_id,
        cost_usd=usage.cost_usd,
        retry_count=usage.retry_count,
        raw_responses=usage.raw_responses,
    )
    return record, usage
