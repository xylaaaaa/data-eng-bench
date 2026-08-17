{{ config(materialized='view') }}
select * from main.stg_marketing__marketing_campaigns
