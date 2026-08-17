{{ config(materialized='view') }}
select * from main.stg_marketing__coupon_redemptions
