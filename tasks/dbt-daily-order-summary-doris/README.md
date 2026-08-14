# Apache Doris dbt tracer task

This directory is a self-contained, experimental Doris adaptation of
[`dbt-daily-order-summary`](../dbt-daily-order-summary). It demonstrates that a
Harbor coding-agent trial can create and verify a real dbt model using
`dbt-for-apache-doris==1.1.0` and Apache Doris.

It is deliberately **not** included in the canonical `dataset.toml`. Its tiny
fixture, Doris-only instruction, verifier, dependency versions, and task digest
differ from the official 103-task dataset, so its reward must not be compared
with existing leaderboard rows.

## What the harness does

For this task, “the harness” is the complete execution workflow, not just the
pytest file:

```text
Harbor reads task.toml
  -> Compose creates an isolated main container and Doris sidecar
  -> the sidecar healthcheck waits for FE and BE
  -> init_doris.py creates a deterministic seven-row ORDERS fixture
  -> Harbor asks the selected agent to create /app/dbt_project
  -> dbt-for-apache-doris compiles and materializes the model in Doris
  -> tests/test.sh runs 13 deterministic pytest checks
  -> the verifier writes reward.txt (1 for success, 0 for failure)
  -> Harbor stores logs/trajectory and removes both containers
```

The responsibilities stay separate:

| Part | Responsibility |
|---|---|
| Harbor | Trial scheduling, agent invocation, timeouts, artifacts, cleanup |
| Docker Compose | Per-trial network and process isolation |
| `init_doris.py` | Backend readiness plus non-overwriting fixture setup |
| Agent | Author and debug the standalone dbt project |
| dbt adapter | Translate dbt relations and materializations to Doris SQL |
| Doris | Execute SQL and store the source and output tables |
| pytest verifier | Judge physical structure, exact data, filtering, and reruns |

## Run the deterministic oracle

Prerequisites are Docker and [uv](https://docs.astral.sh/uv/). The first run
downloads the task image, the adapter from PyPI, and the pinned Doris all-in-one
image. Harbor reserves the `task.toml` budget of 2 CPU/4 GiB for the agent
container, while Compose additionally limits Doris to 4 CPU/12 GiB. Allow at
least 6 CPU and 16 GiB for the complete two-service trial, and run one trial at
a time.

From the repository root:

```bash
uvx harbor run \
  --path tasks/dbt-daily-order-summary-doris \
  --agent oracle \
  --n-concurrent 1
```

Success means the oracle's `dbt debug`, `dbt run`, and four dbt data tests pass,
then the external verifier reports `13 passed` and writes reward `1`.

If a development machine reaches PyPI only through a proxy bound to localhost,
opt in to host networking for the image build only; the portable default uses
Docker's normal build network:

```bash
DBT_DORIS_BUILD_NETWORK=host uvx harbor run \
  --path tasks/dbt-daily-order-summary-doris \
  --agent oracle \
  --n-concurrent 1
```

## Run a coding agent

After the oracle passes, use any Harbor-supported agent. For example:

```bash
uvx harbor run \
  --path tasks/dbt-daily-order-summary-doris \
  --agent codex \
  --model <provider/model> \
  --n-concurrent 1
```

Keep this first tracer at one concurrent trial because each attempt starts a
complete Doris process. The task exposes no host ports and uses no persistent
volume, static IP, or shared database, so later parallel runs remain logically
isolated.

## Why this task has its own main image

The canonical data-eng-bench image pins dbt Core below 1.11 for its DuckDB and
Snowflake adapters. `dbt-for-apache-doris==1.1.0` requires dbt Core 1.12, so
this task installs the Doris adapter in `/opt/dbt-doris`, an isolated virtual
environment layered over the canonical image. This preserves the image's
terminal-agent toolchain without silently upgrading its global dbt install.

The sidecar is pinned to the official
`apache/doris:4.0.3-all-slim` image digest because it can start one FE and one
BE inside a single disposable container. A release gate should also repeat the
demo against the current stable split FE/BE deployment; the all-in-one image is
the tracer's reproducible test fixture, not a production deployment template.
