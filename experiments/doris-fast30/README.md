# Apache Doris fast-30 compatibility experiment

This directory is an unofficial compatibility runner for the canonical
`configs/fast-30.txt` task set. It answers one narrow question:

> Can the published golden dbt solutions and their deterministic verifiers
> execute against Apache Doris through `dbt-for-apache-doris`?

It does **not** measure Codex accuracy, database performance, or an official
third backend for the Snowflake Labs leaderboard. The runner executes the
benchmark's `solution/solve.sh`; a real Agent run is a separate Harbor step.

## Expected release result

The release gate is one clean-commit run with 30 fresh Doris instances:

| Measurement | Gate |
| --- | ---: |
| Tasks selected | 30/30 |
| Canonical dbt solution exit status | 30/30 |
| Verifier cases | 869/869 passed, 0 skipped, 0 failed |
| Host reward | 30/30 equal to `1` |
| Task cleanup | 30/30 containers and networks removed |

`fifo-inventory-cogs` uses the explicit Doris compatibility oracle in
`oracles/fifo-inventory-cogs/expected_results_doris.txt`. The upstream DuckDB
oracle has a documented `LEAST`/`GREATEST` NULL semantic difference; its seven
numeric assertions are not silently counted as Doris failures.

## Reproduce

Prerequisites:

- Docker with permission to create bridge networks and containers;
- Python 3 and PyYAML for the host runner;
- enough headroom for one Doris sidecar (4 CPU/12 GiB plus 4 CPU/8 GiB runner);
- at least 25 GiB free Docker storage. Doris state is bounded by 4 GiB BE and
  2 GiB FE tmpfs mounts, and is deleted after every task.

The runner image must be built once. The build needs access to the pinned base
image and PyPI; use the normal Docker build network unless the local proxy
requires host networking:

```bash
docker build \
  --file experiments/doris-fast30/Dockerfile \
  --tag data-eng-bench-doris-fast30:local \
  experiments/doris-fast30
```

Validate the manifest without changing Docker state:

```bash
python3 experiments/doris-fast30/run.py --validate-only
```

Run one task:

```bash
python3 experiments/doris-fast30/run.py \
  --task dbt-daily-order-summary \
  --require-clean \
  --output-dir /tmp/doris-fast30-daily
```

Run the complete release gate sequentially:

```bash
python3 experiments/doris-fast30/run.py \
  --require-clean \
  --output-dir /tmp/doris-fast30-full
```

`--require-clean` makes the recorded Git commit meaningful. The runner also
records the manifest digest, shared compatibility-layer digest, actual project
tree digest, pinned image IDs, verifier case counts, per-stage exit codes, and
task-scoped cleanup evidence. A non-`1` host reward, missing verifier summary,
artifact-copy error, or cleanup error makes the task fail.

The output directory contains `run.json` and one directory per task with
`runtime.json`, fixture/solution/verifier logs, dbt artifacts, and container
evidence. Do not publish raw `containers.json` without reviewing host paths.
No task uses a persistent volume. The bridge network is
task-scoped; it is not an air-gapped network, so do not point the runner at a
shared Doris deployment.

## What one task does

```text
validate manifest and pinned fixture
  -> create a fresh network and Doris FE/BE container
  -> wait for FE/BE health, compute query, and one-table storage probe
  -> create only the task's target databases
  -> load the task's source closure from retail.duckdb
  -> start a fresh runner container and seed its minimal dbt project
  -> run the canonical solution
  -> run the original pytest verifier and read reward.txt
  -> copy evidence, then remove runner, Doris, and network
```

The Doris image is the pinned all-in-one `4.0.3` image. The adapter image uses
`dbt-for-apache-doris==1.1.0`, dbt Core `1.12.2`, Python `3.12.13`, and a
separate virtual environment because the canonical benchmark image constrains
dbt Core below `1.11`.

## Fixture provenance

The repository's original LFS object is `retail.duckdb`, OID
`bd2bb1b3a7cf62aa94c191527aa30db5bf415aea97db718a1e0c11c59fc2ec2d`, size
`488,910,848` bytes. The release runner does not read the LFS pointer from the
worktree. It verifies the materialized file baked into the pinned base image:

| Runtime input | Value |
| --- | --- |
| Image | `ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0@sha256:29bcb90579ecc8fc0dbb0d658b2a938374ac057ff816c78746520d48ed1edb09` |
| Container path | `/app/database/retail.duckdb` |
| Size | `491,008,000` bytes |
| SHA-256 | `cddd207c9bdf2c0e2a85440038a95c8262bb8327f8849bcfee29be4fb24da5ba` |

The image copy includes the upstream base-image preparation/fix step, so the
runtime digest is the authoritative input for this experiment.

## Scope and limitations

- The 30 task files and original verifier logic remain the benchmark source of
  truth. Minimal projects and the compatibility wrapper are experiment inputs.
- The SQL wrapper handles only constructs observed in fast-30. It is not a
  general DuckDB-to-Doris transpiler.
- The FIFO Doris oracle makes backend NULL behavior explicit; its result must
  be reported separately from a native DuckDB oracle comparison.
- A green golden-solution run is an adapter/backend compatibility baseline. It
  says nothing about whether a coding Agent can discover the solution.
- The experiment is not an official `DB_TYPE=doris` data-eng-bench dataset and
  must not be mixed into the canonical leaderboard or used as a performance
  comparison with DuckDB.

## Real Agent follow-up

The Doris-native tracers at
`tasks/dbt-daily-order-summary-doris` and
`tasks/dbt-test-orders-filter-doris` are the first two real Harbor tasks. Each
has a passing `oracle` run and one passing Codex trial; the Agent phase does not
receive `solution/` or `tests/`, and its result is reported separately from the
golden-solution table above. Extending this distinction to all 30 tasks
requires generated Doris task variants and is intentionally a separate
deliverable.
