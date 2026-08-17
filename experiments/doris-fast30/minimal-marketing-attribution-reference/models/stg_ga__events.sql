{{ config(materialized='view') }}

select * from main.stg_ga__events
