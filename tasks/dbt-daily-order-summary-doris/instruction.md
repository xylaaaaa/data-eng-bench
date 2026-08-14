# Daily Order Summary on Apache Doris

The operations team needs a daily view of order activity to plan staffing and
logistics. Build the view with dbt and materialize it in Apache Doris.

## Your task

Create a **standalone dbt project** at `/app/dbt_project` with a model named
`daily_order_summary`. Do not create or edit files outside that project.

The live backend is a disposable Doris instance created for this trial. Confirm
it first with:

```bash
echo "$DB_TYPE"
dbt --version
```

`DB_TYPE` must be `doris`, and `dbt-for-apache-doris` is already installed.

## Connection

Create `/app/dbt_project/profiles.yml` with profile name `dbt_project`. Configure
the Doris output from these preconfigured environment variables:

- `DORIS_HOST`
- `DORIS_PORT`
- `DORIS_USER`
- `DORIS_PASSWORD`
- `DORIS_TARGET_DATABASE`

Use adapter type `doris`. The adapter expects `username` (not `user`) and uses
the `schema` profile field for a Doris database. Do not set a different
`database` value. `DBT_PROFILES_DIR` already points at `/app/dbt_project`.

## Source data

Define a dbt source for table `ORDERS` in the database named by
`DORIS_SOURCE_DATABASE`. It contains these columns:

| Column | Meaning |
|---|---|
| `ORDER_ID` | Order identifier |
| `ORDERED_AT` | Timestamp when the order was placed |
| `GRAND_TOTAL` | Order amount (`DECIMAL(18,4)`) |
| `STATUS` | Order status |

The seven-row fixture includes completed, shipped, processing, cancelled,
returned, and failed orders, across three dates.

## Model requirements

The model must:

1. Cast `ORDERED_AT` to a date named `order_date`.
2. Exclude `CANCELLED`, `RETURNED`, and `FAILED` orders.
3. Group by `order_date`.
4. Produce `order_count` with `COUNT(*)`.
5. Produce `total_revenue` with the sum of `GRAND_TOTAL`, rounded to two decimal
   places.

Materialize `daily_order_summary` as a Doris table in the database named by
`DORIS_TARGET_DATABASE`. Configure the table for this single-BE demo with:

- `duplicate_key=['order_date']`
- `distributed_by=['order_date']`
- `buckets=1`
- `replication_num=1`

The result must contain exactly these columns:

| Column | Meaning |
|---|---|
| `order_date` | Order date |
| `order_count` | Valid orders on that date |
| `total_revenue` | Valid order revenue on that date |

Add these dbt data tests: `not_null` and `unique` on `order_date`, plus
`not_null` on `order_count` and `total_revenue`. Then run `dbt debug`,
`dbt run`, and `dbt test` before finishing. The verifier will recreate the
model through dbt, execute those four tests, and check its manifest, physical
relation, exact output rows, data quality, filtering, and idempotency against
the live Doris source.
