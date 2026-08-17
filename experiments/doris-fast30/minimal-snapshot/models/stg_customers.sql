{{ config(materialized='view') }}

select
    trim(CUSTOMER_ID) as customer_id,
    trim(CUSTOMER_NUMBER) as customer_number,
    trim(CUSTOMER_TYPE) as customer_type,
    trim(EMAIL) as email,
    EMAIL_VERIFIED as email_verified,
    trim(PHONE_PRIMARY) as phone_primary,
    PHONE_VERIFIED as phone_verified,
    trim(FIRST_NAME) as first_name,
    trim(LAST_NAME) as last_name,
    trim(COMPANY_NAME) as company_name,
    trim(ACQUISITION_SOURCE) as acquisition_source,
    trim(ACQUISITION_CAMPAIGN) as acquisition_campaign
from {{ source('customer', 'CUSTOMERS') }}
