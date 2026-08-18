# dbt-test-orders-filter on Apache Doris

This is an experimental Doris-native Harbor variant of the canonical
dbt-test-orders-filter task. It is intentionally outside the official
103-task dataset and leaderboard.

The trial workflow is:

    Harbor starts a main container and a disposable Doris sidecar
      -> the healthcheck creates seven deterministic ORDERS rows
      -> Codex (or another selected agent) creates /app/dbt_project
      -> dbt-for-apache-doris materializes production_sales
      -> Harbor uploads the hidden verifier after the agent exits
      -> pytest checks structure, flags, exact rows, dbt lineage, tests, and rerun stability
      -> reward.txt records 1 or 0 and the containers are removed

Run the deterministic oracle first:

    uvx harbor run \
      --path tasks/dbt-test-orders-filter-doris \
      --agent oracle \
      --n-concurrent 1

Then run a real coding agent:

    uvx harbor run \
      --path tasks/dbt-test-orders-filter-doris \
      --agent codex \
      --model provider/model \
      --n-concurrent 1

The task uses the same pinned Doris 4.0.3 sidecar and isolated dbt 1.12
environment as the daily-order tracer. The fixture contains four valid rows and
three rows marked test, sample, or internal. A passing verifier reports the
four exact production rows and validates the physical Duplicate Key layout.

The healthcheck waits for both an alive Doris backend and a registered storage
disk before creating the fixture. This avoids a startup race in which Doris
reports `Alive=true` for the BE before it can accept the first table DDL.

The task has been exercised end to end on Harbor 0.21.0. The deterministic
oracle passed 9/9 verifier tests with reward `1`. A separate Codex 0.144.0 /
GPT-5.5 trial independently created `/app/dbt_project`, ran `dbt debug`,
`dbt run`, `dbt test`, inspected Doris metadata, and reran the model; it also
passed 9/9 with reward `1` (one trial, no exception). This is a two-task Doris
smoke sample together with `dbt-daily-order-summary-doris`, not a fast-30 Agent
accuracy result.

This task is a research artifact. Its reward cannot be compared with the
canonical benchmark leaderboard until a Doris dataset revision, prompt policy,
backend-specific verifier contract, and reproducible Agent configuration are
published separately.
