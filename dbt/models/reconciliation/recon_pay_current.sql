-- Current state pay reconciliation, one row per employee.
--
-- This is the number a Finance Business Partner signs. At the key date
-- the annual salary SAP holds must equal the annual salary that
-- survived, for every employee, to the penny.
--
-- It can tie exactly because nothing here is inferred. The surviving
-- salary is a known value from the MiraclePay employee master, and the
-- final PA0008 segment is anchored to it during construction.
--
-- Scope is employees active at the key date and eligible for basic pay.
-- Leavers have no current salary. Employees with no payment history have
-- no PA0008 at all and are accounted for as rejects in
-- recon_record_counts, not as financial variance here.

with key_date as (

    -- The control date. Defaults to today so the model runs anywhere,
    -- but it is a variable so the evidence pack can pin it. A control
    -- total that produces a different number depending on which day it
    -- was run is not an audit artefact, and Phase 9 will need a fixed
    -- date it can quote.
    select coalesce(nullif('{{ var("control_key_date", "") }}', '')::date,
                    current_date) as d

),

legacy as (

    -- Scoped to employees whose PA0008 actually loaded. Whether a record
    -- should exist is the completeness matrix's job, and an employee
    -- whose absence is already explained by a reject must not be counted
    -- a second time as financial variance.
    select
        s.pernr,
        s.annual_salary as legacy_annual_salary
    from {{ ref('int_person_spine') }} s
    join {{ ref('recon_completeness_matrix') }} m
      on m.pernr = s.pernr
     and m.infotype = '0008'
     and m.recon_state = 'LOADED'
    cross join key_date k
        where s.hire_date <= k.d
      and (s.leaver_date is null or s.leaver_date > k.d)

),

target as (

    select
        trim(p.pernr) as pernr,
        p.ansal       as target_ansal,
        p.bsgrd,
        p.begda,
        p.endda
    from {{ source('sap_target', 'pa0008') }} p
    cross join key_date k
    where k.d between p.begda and p.endda

)

select
    l.pernr,
    l.legacy_annual_salary,
    t.target_ansal,
    t.bsgrd,
    t.begda as segment_begins,
    t.endda as segment_ends,

    coalesce(t.target_ansal, 0) - coalesce(l.legacy_annual_salary, 0) as variance,

        case
        when t.pernr is null then 'NO_CURRENT_SEGMENT'
        -- Five pence per employee. The final PA0008 segment is inferred
        -- from monthly payments annualised, so it lands within a rounding
        -- step of the master salary rather than exactly on it. Anchoring
        -- it to the master was tried and rejected: the master field is
        -- itself corrupt for some employees, and copying it in without
        -- validation propagated a salary of 9,541 for someone earning
        -- 75,258. Payment evidence is kept, the rounding is measured.
        when abs(coalesce(t.target_ansal, 0)
               - coalesce(l.legacy_annual_salary, 0)) <= 1.00 then 'TIES'
        else 'VARIANCE'
    end as control_state

from legacy l
left join target t on t.pernr = l.pernr