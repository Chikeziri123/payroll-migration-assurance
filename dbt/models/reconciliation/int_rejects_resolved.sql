{{ config(materialized='view') }}

-- Classifies every reject and resolves it back to a PERNR.
--
-- Two things are going on in migration_rejects and they are not the same
-- thing at all:
--
--   Rows that name an infotype are genuine exclusions. The record was not
--   loaded into that infotype and the reason says why.
--
--   Rows with a NULL infotype are person-level observations. Fuzzy match
--   confirmations, HR records with no payroll counterpart, payroll
--   records with no HR counterpart. Every one of those people was
--   migrated. They are control concerns raised for a human, not
--   exclusions.
--
-- Severity does not separate them. 39 of the person-level observations
-- are CRITICAL and none of them stopped anything. Reconciling on severity
-- would therefore accept an advisory as the explanation for a missing
-- record, which is exactly the silent loss this phase exists to catch.
-- The infotype column is what separates them, so that is what is used.
--
-- The source_key also means different things depending on which system
-- wrote the row, so it has to be resolved rather than joined blindly:
--   SAP_TARGET        the key already is the PERNR
--   MiraclePay        a payroll reference, resolve through key_map
--   HR.net            an HR employee id, resolve through key_map
--   HR.net/MiraclePay an HR employee id, resolve through key_map

with rejects as (

    select
        reject_id,
        source_system,
        trim(source_key)            as source_key,
        nullif(trim(infotype), '')  as infotype,
        reject_reason,
        reject_severity,
        rejected_at
    from {{ source('sap_target', 'migration_rejects') }}

),

key_map as (

    select
        trim(pernr)  as pernr,
        hrnet_emp_id,
        payroll_ref
    from {{ source('sap_target', 'key_map') }}

)

select
    r.reject_id,
    r.source_system,
    r.source_key,
    r.infotype,
    r.reject_reason,
    r.reject_severity,
    r.rejected_at,

    case
        when r.infotype is not null then 'BLOCKING'
        else 'ADVISORY'
    end as reject_class,

    case
        when r.source_system = 'SAP_TARGET' then r.source_key
        when r.source_system = 'MiraclePay' then km_pay.pernr
        else km_hr.pernr
    end as pernr

from rejects r
left join key_map km_pay on km_pay.payroll_ref  = r.source_key
left join key_map km_hr  on km_hr.hrnet_emp_id  = r.source_key
