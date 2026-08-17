{{ config(materialized='view') }}

select
    CUSTOMER_KEY as customer_key,
    CUSTOMER_ID as customer_id
from {{ source('analytics', 'DIM_CUSTOMER') }}
