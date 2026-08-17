{{ config(materialized='view') }}
select * from main.stg_pos__coupon_usage
