{{ config(materialized='view') }}

select
    trim(rate_id) as rate_id,
    trim(from_currency) as from_currency,
    trim(to_currency) as to_currency,
    exchange_rate,
    effective_date,
    trim(source) as source,
    created_at
from {{ source('finance', 'CURRENCY_EXCHANGE_RATES') }}
