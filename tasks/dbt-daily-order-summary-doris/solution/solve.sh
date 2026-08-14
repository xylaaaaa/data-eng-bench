#!/usr/bin/env bash
set -euo pipefail

if [[ "${DB_TYPE:-}" != "doris" ]]; then
    echo "This demo requires DB_TYPE=doris" >&2
    exit 1
fi

: "${DORIS_HOST:?DORIS_HOST is required}"
: "${DORIS_PORT:?DORIS_PORT is required}"
: "${DORIS_USER:?DORIS_USER is required}"
: "${DORIS_SOURCE_DATABASE:?DORIS_SOURCE_DATABASE is required}"
: "${DORIS_TARGET_DATABASE:?DORIS_TARGET_DATABASE is required}"

project_dir=/app/dbt_project
mkdir -p "$project_dir/models/marts" "$project_dir/models/staging"

cat > "$project_dir/profiles.yml" <<'YAML'
dbt_project:
  target: dev
  outputs:
    dev:
      type: doris
      host: "{{ env_var('DORIS_HOST') }}"
      port: "{{ env_var('DORIS_PORT') | int }}"
      username: "{{ env_var('DORIS_USER') }}"
      password: "{{ env_var('DORIS_PASSWORD', '') }}"
      schema: "{{ env_var('DORIS_TARGET_DATABASE') }}"
      threads: 4
YAML

cat > "$project_dir/dbt_project.yml" <<'YAML'
name: dbt_project
version: 1.0.0
config-version: 2
profile: dbt_project
model-paths: ["models"]
YAML

cat > "$project_dir/models/staging/sources.yml" <<'YAML'
version: 2

sources:
  - name: orders
    schema: "{{ env_var('DORIS_SOURCE_DATABASE') }}"
    tables:
      - name: orders
        identifier: ORDERS
YAML

cat > "$project_dir/models/marts/daily_order_summary.sql" <<'SQL'
{{
  config(
    materialized='table',
    duplicate_key=['order_date'],
    distributed_by=['order_date'],
    buckets=1,
    replication_num=1
  )
}}

with valid_orders as (
    select
        cast(ORDERED_AT as date) as order_date,
        GRAND_TOTAL
    from {{ source('orders', 'orders') }}
    where STATUS not in ('CANCELLED', 'RETURNED', 'FAILED')
),

daily_aggregates as (
    select
        order_date,
        count(*) as order_count,
        round(sum(GRAND_TOTAL), 2) as total_revenue
    from valid_orders
    group by order_date
)

select
    order_date,
    order_count,
    total_revenue
from daily_aggregates
SQL

cat > "$project_dir/models/marts/schema.yml" <<'YAML'
version: 2

models:
  - name: daily_order_summary
    description: Daily valid-order counts and revenue in Apache Doris.
    columns:
      - name: order_date
        data_tests:
          - not_null
          - unique
      - name: order_count
        data_tests:
          - not_null
      - name: total_revenue
        data_tests:
          - not_null
YAML

cd "$project_dir"
dbt --version
dbt debug --profiles-dir "$project_dir"
dbt run --profiles-dir "$project_dir"
dbt test --profiles-dir "$project_dir"

echo "Doris dbt demo solution complete"
