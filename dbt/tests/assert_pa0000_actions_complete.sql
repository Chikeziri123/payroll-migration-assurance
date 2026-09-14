-- Actions must agree with employment status.
--
-- Every person the builder processed gets a hire action, and every
-- leaver among them gets a leaving action as well. If those counts
-- drift apart, SAP's view of who still works here has drifted from the
-- surviving record, which drives everything from payroll runs to leaver
-- reporting.
--
-- Scoped to employees who reached PA0000 at all. Whether somebody
-- should have reached it is the completeness matrix's job, and having
-- two tests answer the same question means fixing a defect twice.

with employees_with_actions as (

    select
        s.pernr,
        s.leaver_date
    from {{ ref('int_person_spine') }} s
    where exists (
        select 1
        from {{ source('sap_target', 'pa0000') }} p
        where trim(p.pernr) = s.pernr
    )

),

expected as (

    select
        count(*)                                          as hires,
        count(*) filter (where leaver_date is not null)   as leavers
    from employees_with_actions

),

actual as (

    select
        count(*) filter (where trim(massn) = '01')        as hire_actions,
        count(*) filter (where trim(massn) = '10')        as leaving_actions
    from {{ source('sap_target', 'pa0000') }}

)

select
    e.hires,
    a.hire_actions,
    e.leavers,
    a.leaving_actions
from expected e
cross join actual a
where e.hires <> a.hire_actions
   or e.leavers <> a.leaving_actions