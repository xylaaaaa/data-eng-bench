{{ config(materialized='view') }}

select * from {{ source('orders', 'ORDERS') }}
