{{ config(materialized='view') }}
select * from main.stg_marketing__gift_cards
