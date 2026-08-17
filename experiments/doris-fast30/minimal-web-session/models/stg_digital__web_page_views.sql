{{ config(materialized='view') }}

select * from main.WEB_PAGE_VIEWS
