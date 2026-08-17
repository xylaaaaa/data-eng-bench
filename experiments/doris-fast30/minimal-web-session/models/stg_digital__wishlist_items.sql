{{ config(materialized='view') }}

select * from main.WISHLIST_ITEMS
