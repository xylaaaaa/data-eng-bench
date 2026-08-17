{{ config(materialized='view') }}

select * from {{ source('orders', 'ORDER_LINES') }}
