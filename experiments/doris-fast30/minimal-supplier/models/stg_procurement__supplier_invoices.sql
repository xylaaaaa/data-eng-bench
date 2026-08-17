{{ config(materialized='view') }}

select
    trim(invoice_id) as invoice_id,
    trim(invoice_number) as invoice_number,
    trim(supplier_id) as supplier_id,
    trim(po_id) as po_id,
    invoice_date,
    due_date,
    total_amount,
    trim(currency_code) as currency_code,
    trim(status) as status,
    created_at,
    updated_at
from {{ source('procurement', 'SUPPLIER_INVOICES') }}
