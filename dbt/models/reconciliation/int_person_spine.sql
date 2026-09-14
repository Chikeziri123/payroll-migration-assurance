{{ config(materialized='view') }}

-- One row per migrated person, carrying that person's eligibility for
-- each infotype.
--
-- Eligibility is the crux of the whole reconciliation. A missing PA0006
-- for somebody who has no address in either source system is not a gap,
-- it is correct behaviour. Counting it as a gap produces a reconciliation
-- that can never balance, and a reconciliation that can never balance
-- gets ignored by the people it was built for.
--
-- PERNR is char(8) in the target. Every join in this project trims it to
-- text, consistently, so that a padding difference can never silently
-- fail a join and manufacture a fake exception.

with key_map as (

    select
        trim(pernr)        as pernr,
        hrnet_emp_id,
        payroll_ref,
        match_method,
        match_confidence
    from {{ source('sap_target', 'key_map') }}

),

golden as (

    select
        trim(pernr)        as pernr,
        hire_date,
        leaver_date,
        employment_status,
        address_line_1,
        postcode,
        sort_code,
        account_number,
        annual_salary,
        fte,
        has_critical_defect,
        review_required
    from {{ source('staging', 'golden_employee') }}

),

-- Payment history is the only evidence PA0008 can be built from. This is
-- read for existence only, never for values, so the 4 year window
-- limitation does not leak into the completeness logic.
has_payments as (

    select distinct payroll_ref
    from {{ source('legacy', 'miraclepay_payment_history') }}
    where payroll_ref is not null

)

select
    km.pernr,
    km.hrnet_emp_id,
    km.payroll_ref,
    km.match_method,
    km.match_confidence,

    g.hire_date,
    g.leaver_date,
    g.employment_status,
    g.annual_salary,
    g.fte,
    g.has_critical_defect,
    g.review_required,

    -- Actions, organisational assignment and personal data are
    -- unconditional. There is no data condition that excuses their
    -- absence, so any gap here is a defect by definition.
    true                                                as eligible_0000,
    true                                                as eligible_0001,
    true                                                as eligible_0002,

    -- An address record needs an address to have survived survivorship.
    (g.address_line_1 is not null or g.postcode is not null)
                                                        as eligible_0006,

    -- Basic pay is inferred from payment history. No history, no
    -- inference. The 151 rejects reading "cannot derive basic pay"
    -- confirm this is deliberate rather than accidental.
    (hp.payroll_ref is not null)                        as eligible_0008,

    -- Bank details need both halves of the payment instruction. A sort
    -- code with no account number cannot be paid to, so it is not a
    -- partial record, it is no record.
    (g.sort_code is not null and g.account_number is not null)
                                                        as eligible_0009

from key_map km
left join golden       g  on g.pernr        = km.pernr
left join has_payments hp on hp.payroll_ref = km.payroll_ref
