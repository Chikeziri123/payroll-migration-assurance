-- The last basic pay segment must end where employment ends.
--
-- For a leaver that is the leaver date. For anyone still employed it is
-- the SAP high date. An employee whose pay record runs past their leaving
-- date is an employee SAP will keep paying.
--
-- This is expected to fail on the current build with exactly two rows,
-- and those two are already recorded as HIGH rejects against infotype
-- 0008 reading "tiling assertion failed". They are left failing rather
-- than thresholded away. A build that is green because somebody set a
-- tolerance to 2 is the precise failure mode this framework exists to
-- prevent.

with final_segment as (

    select distinct on (trim(pernr))
        trim(pernr) as pernr,
        begda,
        endda
    from {{ source('sap_target', 'pa0008') }}
    order by trim(pernr), begda desc

)

select
    f.pernr,
    f.endda     as final_segment_ends,
    s.leaver_date,
    case
        when s.leaver_date is not null then s.leaver_date - 1
        else date '9999-12-31'
    end as expected_end
from final_segment f
join {{ ref('int_person_spine') }} s on s.pernr = f.pernr
where f.endda <> case
                     when s.leaver_date is not null then s.leaver_date - 1
                     else date '9999-12-31'
                 end

