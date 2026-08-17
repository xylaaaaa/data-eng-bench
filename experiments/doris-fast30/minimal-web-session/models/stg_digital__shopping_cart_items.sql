{{ config(materialized='view') }}

select * from main.SHOPPING_CART_ITEMS
