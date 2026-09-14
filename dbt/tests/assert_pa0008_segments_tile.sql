-- Basic pay history must tile without gaps.
--
-- The exclusion constraint on the table already makes overlaps physically
-- impossible, so this is the other half: consecutive segments must be
-- contiguous. A gap means a period where SAP believes the employee was
-- paid nothing, which would be invisible in a total and obvious in a
-- payslip.

with segments as (

    select
        trim(pernr) as pernr,
        begda,
        endda,
        lead(begda) over (partition by trim(pernr) order by begda) as next_begda
    from {{ source('sap_target', 'pa0008') }}

)

select
    pernr,
    endda       as segment_ends,
    next_begda  as next_segment_starts
from segments
where next_begda is not null
  and next_begda <> endda + 1
