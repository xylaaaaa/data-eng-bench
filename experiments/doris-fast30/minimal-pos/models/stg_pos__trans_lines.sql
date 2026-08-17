{{ config(materialized='view') }}
select * from main.stg_pos__trans_lines
