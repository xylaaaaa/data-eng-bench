# Apache Doris fast-30 compatibility experiment

This directory records an unofficial golden-solution compatibility run of the
canonical `configs/fast-30.txt` task set against Apache Doris. It is not an
official data-eng-bench backend, a coding-agent score, or a leaderboard
submission.

## Result

The run used the canonical task solutions and verifier source at upstream
commit `53353547b9869d35d61b40fd6ee9397a7ac8ca80`.

| Measurement | Result |
| --- | ---: |
| Canonical solution model graphs completed on Doris | 30/30 tasks |
| Verifier cases using the canonical DuckDB oracle | 862/869 |
| Tasks passing the canonical verifier source and oracle | 29/30 |
| Verifier cases using the complete Doris-specific FIFO oracle | 869/869 |
| Tasks passing with backend-specific expected values | 30/30 |

The distinction matters. The canonical benchmark only supports DuckDB and
Snowflake. The experiment kept every canonical task file unchanged, but used a
runtime compatibility layer to connect dbt and the verifier to Doris. The
result measures adapter and SQL compatibility of reference solutions. It does
not measure whether Codex or another agent can solve the tasks.

Fixed runtime:

| Component | Version |
| --- | --- |
| Apache Doris | `4.0.3`, single FE/BE all-in-one container |
| dbt adapter | `dbt-for-apache-doris==1.1.0` |
| dbt Core | `1.12.2` |
| Python | `3.12.13` |
| Source fixture | `retail.duckdb`, LFS SHA-256 `bd2bb1b3...fc2ec2d` |

The tasks ran sequentially against one dedicated Doris sidecar. Source
relations were copied selectively from the DuckDB fixture, and minimal dbt
projects limited each parse graph to the task's actual dependency closure.
This is less isolated than a formal Harbor trial and must not be mixed into the
official leaderboard.

A post-run dependency audit found that the first cross-sell run inherited four
of its six `main.*` inputs from earlier tasks. That task was then rerun after
dropping both `main` and `analytics`, explicitly loading all six inputs, and
creating the target database. Its four dbt models and all 18 canonical verifier
cases passed again. This closes the known state-leak case, but it does not turn
the other sequential runs into independently isolated trials.

## Reproduction boundary

This directory preserves the compatibility image, loader, SQL/verifier
compatibility layer, minimal parse projects, and the backend-specific FIFO
oracle used during the run. The 30 tasks were migrated and executed one at a
time; this is not a one-command fast-30 backend and no such command is claimed.
The supported runnable demonstration in this fork is
[`tasks/dbt-daily-order-summary-doris`](../../tasks/dbt-daily-order-summary-doris),
which packages its own Doris sidecar, fixture, oracle, verifier, and cleanup in
a Harbor task.

The experiment tools are intentionally destructive inside their disposable
test environment. `load_duckdb.py` replaces a same-named Doris table or view,
and `dbt-wrapper.py` rewrites the selected dbt project's profile, project
configuration, and SQL before execution. Never point them at a persistent or
shared Doris database. A reproducible full fast-30 backend still requires an
explicit task manifest, per-trial database isolation, and a runner that records
the exact task digest and artifacts.

## Per-task evidence

| Task | Verifier result |
| --- | ---: |
| `cohort-retention-matrix` | 24/24 |
| `dbt-cart-abandonment-recovery` | 1/1 |
| `dbt-customer-churn-cohorts` | 3/3 |
| `dbt-customer-cltv-forecasting` | 5/5 |
| `dbt-customer-cross-sell-insights` | 18/18 |
| `dbt-daily-order-summary` | 13/13 |
| `dbt-fix-cac-payback-waterfall` | 14/14 |
| `dbt-fix-customer-snapshot-and-build-dimension` | 20/20 |
| `dbt-fix-daily-cohorts` | 11/11 |
| `dbt-fix-division-by-zero` | 1/1 |
| `dbt-fix-marketing-attribution` | 13/13 |
| `dbt-fix-refund-reconciliation` | 1/1 |
| `dbt-fraud-detection-model` | 13/13 |
| `dbt-inventory-turnover-analysis` | 10/10 |
| `dbt-multi-warehouse-stock-rebalance` | 1/1 |
| `dbt-price-elasticity` | 13/13 |
| `dbt-product-affinity` | 63/63 |
| `dbt-receivables-aging-buckets` | 4/4 |
| `dbt-rfm-customer-segmentation` | 13/13 |
| `dbt-session-attribution` | 1/1 |
| `dbt-supplier-payment-optimization` | 8/8 |
| `dbt-test-orders-filter` | 15/15 |
| `deferred-revenue-recognition` | 20/20 |
| `fifo-inventory-cogs` | 43/50 canonical; 50/50 Doris oracle |
| `marketing-campaigns-harbor` | 219/219 |
| `pos-operations` | 69/69 |
| `promotional-lift-analysis` | 16/16 |
| `tier-migration-analysis` | 27/27 |
| `web-session-quality-scoring` | 69/69 |
| `workforce-analytics` | 134/134 |

## FIFO oracle divergence

The seven canonical failures all come from the DuckDB expected-value file.
The allocation SQL applies `LEAST` and `GREATEST` to nullable columns from a
left join. DuckDB ignores null arguments, so 3,392 unmatched rows are counted
as 168,315 fulfilled units. Doris and Snowflake propagate null and correctly
allocate zero units for those rows.

The complete Doris oracle is in
`oracles/fifo-inventory-cogs/expected_results_doris.txt`. Unlike the upstream
Snowflake oracle, it includes ending inventory totals and total allocated
quantity, so all 50 verifier cases execute real assertions. The same values
were reproduced in DuckDB after adding an explicit
`case when receipt_id is null then 0` guard to the allocation expression.

This is a benchmark portability defect, not an adapter execution failure. An
upstream fix should make the null behavior explicit and regenerate every
backend oracle.

## Experiment components

- `Dockerfile` builds an isolated dbt 1.12 environment over the benchmark base
  image, avoiding its dbt Core `<1.11` constraint.
- `dbt-wrapper.py` rewrites generated profiles and the small set of SQL dialect
  constructs encountered by fast-30.
- `duckdb.py` preserves the verifier's DuckDB-shaped Python API while executing
  queries against Doris.
- `load_duckdb.py` copies only required fixture relations into Doris.
- `minimal-*` directories hold small dbt parse graphs for tasks that otherwise
  pull in the full 2,300-model project.

These are research artifacts, not a supported third-backend implementation.
A production variant still needs per-trial Doris isolation, task packages with
an explicit `DB_TYPE=doris`, a shared verifier abstraction, and a versioned
Doris dataset digest.
