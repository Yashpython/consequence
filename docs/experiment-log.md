# Experiment log

What happened during each real run, beyond what the results database holds:
when it ran, which configuration, and anything that could confound it. No
run has happened yet.

## Runs

None yet. Each entry: date, run id, git commit, `models.yaml` at that commit,
judging options, and anything unusual.

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
