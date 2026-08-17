{{ config(materialized='view') }}

select * from main.SHOPPING_CARTS
