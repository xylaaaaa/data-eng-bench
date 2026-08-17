{{ config(materialized='view') }}

select * from {{ source('inventory', 'INVENTORY_LEVELS') }}
