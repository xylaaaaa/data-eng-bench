{{ config(materialized='view') }}

select * from {{ source('finance', 'CUSTOMER_INVOICES') }}
