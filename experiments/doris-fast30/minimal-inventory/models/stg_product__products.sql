{{ config(materialized='view') }}

select * from {{ source('product', 'PRODUCTS') }}
