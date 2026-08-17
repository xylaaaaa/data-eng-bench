{{ config(materialized='view') }}
select * from main.stg_marketing__promotion_rules
