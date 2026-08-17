{{ config(materialized='view') }}

select * from main.int_sessions_events_joined
