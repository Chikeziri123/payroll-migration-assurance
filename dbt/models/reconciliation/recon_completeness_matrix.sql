-- The completeness matrix. One row per person per infotype, carrying the
-- expectation, what is actually in the target, whether a blocking reject
-- exists, and the resulting reconciliation state.
--
-- Every other reconciliation model in this project aggregates or filters
-- this one. That is deliberate: the counts in the summary and the rows in
-- the exception report cannot disagree with each other, because they are
-- the same rows counted two ways. Reconciliations that build their
-- summary and their detail separately drift apart, usually quietly, and
-- usually just before go-live.
--
-- The six states are mutually exclusive and each names a distinct failure
-- mode:
--
--   LOADED                    eligible, present, no blocking reject
--   CORRECTLY_ABSENT          not eligible, not present
--   EXPLAINED_ABSENT          eligible, absent, blocking reject says why
--   UNEXPLAINED_ABSENT        eligible, absent, nothing accounts for it
--   REJECTED_BUT_PRESENT      a reject says excluded, the target says no
--   NOT_ELIGIBLE_BUT_PRESENT  a record exists that should not

with spine as (

    select * from {{ ref('int_person_spine') }}

),

-- Unpivot the eligibility flags into one row per person per infotype.
-- Postgres has no native unpivot, and a union all of six branches reads
-- more plainly here than a lateral over a values list.
expectation as (

    select pernr, '0000' as infotype, eligible_0000 as eligible from spine
    union all
    select pernr, '0001', eligible_0001 from spine
    union all
    select pernr, '0002', eligible_0002 from spine
    union all
    select pernr, '0006', eligible_0006 from spine
    union all
    select pernr, '0008', eligible_0008 from spine
    union all
    select pernr, '0009', eligible_0009 from spine

),

-- Row counts as well as presence. PA0006 and PA0009 carry a subtype and
-- PA0008 carries a validity segment per salary period, so rows per person
-- is not one for those and the summary needs both figures.
present as (

    select trim(pernr) as pernr, '0000' as infotype, count(*) as target_rows
    from {{ source('sap_target', 'pa0000') }} group by 1
    union all
    select trim(pernr), '0001', count(*)
    from {{ source('sap_target', 'pa0001') }} group by 1
    union all
    select trim(pernr), '0002', count(*)
    from {{ source('sap_target', 'pa0002') }} group by 1
    union all
    select trim(pernr), '0006', count(*)
    from {{ source('sap_target', 'pa0006') }} group by 1
    union all
    select trim(pernr), '0008', count(*)
    from {{ source('sap_target', 'pa0008') }} group by 1
    union all
    select trim(pernr), '0009', count(*)
    from {{ source('sap_target', 'pa0009') }} group by 1

),

blocking as (

    select
        pernr,
        infotype,
        count(*)                             as blocking_rejects,
        min(reject_severity)                 as reject_severity,
        min(reject_reason)                   as reject_reason
    from {{ ref('int_rejects_resolved') }}
    where reject_class = 'BLOCKING'
      and pernr is not null
    group by 1, 2

)

select
    e.pernr,
    e.infotype,
    e.eligible,
    (p.pernr is not null)                    as present,
    coalesce(p.target_rows, 0)               as target_rows,
    coalesce(b.blocking_rejects, 0)          as blocking_rejects,
    b.reject_severity,
    b.reject_reason,

    case
        when e.eligible and p.pernr is not null and b.pernr is null
            then 'LOADED'
        when e.eligible and p.pernr is not null and b.pernr is not null
            then 'REJECTED_BUT_PRESENT'
        when e.eligible and p.pernr is null and b.pernr is not null
            then 'EXPLAINED_ABSENT'
        when e.eligible and p.pernr is null and b.pernr is null
            then 'UNEXPLAINED_ABSENT'
        when not e.eligible and p.pernr is not null
            then 'NOT_ELIGIBLE_BUT_PRESENT'
        else 'CORRECTLY_ABSENT'
    end as recon_state

from expectation e
left join present  p on p.pernr = e.pernr and p.infotype = e.infotype
left join blocking b on b.pernr = e.pernr and b.infotype = e.infotype
