{{ config(materialized='view') }}

select
    DATE_KEY as date_key,
    FULL_DATE as full_date
from {{ source('analytics', 'DIM_DATE') }}
