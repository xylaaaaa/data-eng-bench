#!/usr/bin/env bash
set -euo pipefail

[[ "${DB_TYPE:-}" == "doris" ]] || { echo "This task requires DB_TYPE=doris" >&2; exit 1; }
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
  - name: orders_source
    schema: "{{ env_var('DORIS_SOURCE_DATABASE') }}"
    tables:
      - name: orders
        identifier: ORDERS
YAML

cat > "$project_dir/models/marts/production_sales.sql" <<'SQL'
{{
  config(
    materialized='table',
    duplicate_key=['order_id'],
    distributed_by=['order_id'],
    buckets=1,
    replication_num=1
  )
}}

select
    ORDER_ID as order_id,
    CUSTOMER_ID as customer_id,
    cast(ORDERED_AT as date) as order_date,
    round(GRAND_TOTAL, 2) as grand_total,
    STATUS as status
from {{ source('orders_source', 'orders') }}
where coalesce(TEST_ORDER_FLAG, false) = false
  and coalesce(SAMPLE_ORDER_FLAG, false) = false
  and coalesce(INTERNAL_ORDER_FLAG, false) = false
SQL

cat > "$project_dir/models/marts/schema.yml" <<'YAML'
version: 2
models:
  - name: production_sales
    columns:
      - name: order_id
        data_tests:
          - not_null
          - unique
      - name: customer_id
        data_tests:
          - not_null
      - name: order_date
        data_tests:
          - not_null
      - name: grand_total
        data_tests:
          - not_null
      - name: status
        data_tests:
          - not_null
YAML

cd "$project_dir"
dbt --version
dbt debug --profiles-dir "$project_dir"
dbt run --profiles-dir "$project_dir"
dbt test --profiles-dir "$project_dir"

echo "Doris production-sales filter solution complete"
