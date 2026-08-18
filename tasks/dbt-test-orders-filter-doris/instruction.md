# Production Sales Order Filter on Apache Doris

Build a standalone dbt project at /app/dbt_project that removes test, sample,
and internal orders from a production sales table.

The live backend is a disposable Apache Doris instance. First run:

    echo "$DB_TYPE"
    dbt --version

DB_TYPE must be doris; dbt-for-apache-doris is already installed in the active
environment.

## Connection

Create /app/dbt_project/profiles.yml with profile name dbt_project. Configure
the Doris output using DORIS_HOST, DORIS_PORT, DORIS_USER, DORIS_PASSWORD, and
DORIS_TARGET_DATABASE. Use adapter type doris, username (not user), and the
target database as the profile schema.

## Source

Define a dbt source for physical table ORDERS in the database named by
DORIS_SOURCE_DATABASE. It contains:

| Column | Type / meaning |
|---|---|
| ORDER_ID | unique order identifier |
| CUSTOMER_ID | customer identifier |
| ORDERED_AT | order timestamp |
| GRAND_TOTAL | DECIMAL(18,4) order amount |
| STATUS | order status |
| TEST_ORDER_FLAG | nullable boolean |
| SAMPLE_ORDER_FLAG | nullable boolean |
| INTERNAL_ORDER_FLAG | nullable boolean |

## Model

Create a table model named production_sales in DORIS_TARGET_DATABASE. Preserve
these columns (additional columns are okay): order_id, customer_id, order_date,
grand_total, and status.

Requirements:

1. Cast ORDERED_AT to a DATE column named order_date.
2. Exclude every row where any of the three order flags is true; null flags
   count as false.
3. Round grand_total to two decimal places.
4. Materialize an Apache Doris table with duplicate_key=['order_id'],
   distributed_by=['order_id'], buckets=1, and replication_num=1.
5. Add dbt data tests: not_null and unique on order_id, and not_null on
   customer_id, order_date, grand_total, and status.

Run dbt debug, dbt run, and dbt test. The verifier will recreate the model,
check its manifest and source lineage, inspect the physical Doris table, compare
the exact filtered rows, and run the model a second time for idempotency.
