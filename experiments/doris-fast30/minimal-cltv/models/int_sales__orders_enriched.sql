{{ config(materialized='view') }}

select
    ORDER_ID as order_id,
    CUSTOMER_ID as customer_id,
    ORDERED_AT as ordered_at,
    GRAND_TOTAL as grand_total,
    (STATUS = 'CANCELLED') as is_cancelled
from {{ source('orders', 'ORDERS') }}
