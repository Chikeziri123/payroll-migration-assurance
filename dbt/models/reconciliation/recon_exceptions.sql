-- The exception report. Every person and infotype that is not cleanly
-- reconciled, one row each, with enough context to action it.
--
-- Scope note: CORRECTLY_ABSENT is excluded because it is not an
-- exception. An employee with no bank details in either source correctly
-- has no PA0009, and listing 125 of those would bury the one record that
-- actually matters. LOADED is excluded for the same reason.
--
-- What remains should, in a healthy migration, be entirely
-- EXPLAINED_ABSENT. Anything else is a finding.
--
-- The match method and review flags are carried through because the first
-- question anyone asks about an exception is whether this person was
-- already known to be difficult. A fuzzy matched record that then failed
-- to load is a different conversation from a cleanly matched one that
-- vanished.

with matrix as (

    select * from {{ ref('recon_completeness_matrix') }}

),

spine as (

    select * from {{ ref('int_person_spine') }}

)

select
    m.infotype,
    m.recon_state,
    m.pernr,

    s.hrnet_emp_id,
    s.payroll_ref,
    s.match_method,
    s.employment_status,
    s.leaver_date,
    s.has_critical_defect,
    s.review_required,

    m.eligible,
    m.present,
    m.target_rows,
    m.blocking_rejects,
    m.reject_severity,
    m.reject_reason,

    -- Severity ordering for triage. Unexplained loss outranks everything,
    -- because it is the only state where nobody knows what happened.
    case m.recon_state
        when 'UNEXPLAINED_ABSENT'       then 1
        when 'REJECTED_BUT_PRESENT'     then 2
        when 'NOT_ELIGIBLE_BUT_PRESENT' then 3
        when 'EXPLAINED_ABSENT'         then 4
        else 5
    end as triage_rank

from matrix m
left join spine s on s.pernr = m.pernr
where m.recon_state not in ('LOADED', 'CORRECTLY_ABSENT')
order by triage_rank, m.infotype, m.pernr
