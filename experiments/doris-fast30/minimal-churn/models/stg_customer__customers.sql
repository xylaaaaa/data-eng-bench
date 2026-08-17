{{ config(materialized='view') }}

select
    CUSTOMER_ID as customer_id,
    ACQUISITION_DATE as acquisition_date,
    CUSTOMER_SEGMENT_SNAPSHOT as customer_segment_snapshot,
    LEGACY_REGION_CODE as legacy_region_code
from {{ source('customer', 'CUSTOMERS') }}
