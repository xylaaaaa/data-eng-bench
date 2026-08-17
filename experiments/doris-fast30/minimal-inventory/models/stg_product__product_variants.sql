{{ config(materialized='view') }}

select * from {{ source('product', 'PRODUCT_VARIANTS') }}
