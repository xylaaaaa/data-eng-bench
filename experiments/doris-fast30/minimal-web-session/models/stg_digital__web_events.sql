{{ config(materialized='view') }}

select * from main.WEB_EVENTS
