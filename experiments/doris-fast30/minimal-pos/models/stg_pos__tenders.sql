{{ config(materialized='view') }}
select * from main.stg_pos__tenders
