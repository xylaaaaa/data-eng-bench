{{ config(materialized='view') }}

select
    ORDER_ID as order_id,
    CUSTOMER_KEY as customer_key,
    DATE_KEY as date_key,
    TOTAL_AMOUNT as total_amount
from {{ source('analytics', 'FACT_SALES') }}
