-- Source to target record counts by infotype, with the entire difference
-- accounted for.
--
-- This is the table a programme board looks at. Every column is a whole
-- number of people and the columns add up, which is the only property
-- that makes a reconciliation summary trustworthy. If the numbers here
-- did not add up the reader would have to take the status column on
-- faith, and nobody signs off on faith.
--
-- status is FAIL when any of the three pathological states is non-zero.
-- CORRECTLY_ABSENT and EXPLAINED_ABSENT are not failures. A migration
-- that moves nothing it should not move and explains everything it did
-- not move is a passing migration, however large the gaps look.

with matrix as (

    select * from {{ ref('recon_completeness_matrix') }}

)

select
    infotype,

    count(*)                                                     as population,
    count(*) filter (where eligible)                             as eligible,

    count(*) filter (where recon_state = 'LOADED')               as loaded,
    sum(target_rows)                                             as target_rows,

    count(*) filter (where recon_state = 'EXPLAINED_ABSENT')     as explained_absent,
    count(*) filter (where recon_state = 'CORRECTLY_ABSENT')     as correctly_absent,

    count(*) filter (where recon_state = 'UNEXPLAINED_ABSENT')   as unexplained_absent,
    count(*) filter (where recon_state = 'REJECTED_BUT_PRESENT') as rejected_but_present,
    count(*) filter (where recon_state = 'NOT_ELIGIBLE_BUT_PRESENT')
                                                                 as not_eligible_but_present,

    case
        when count(*) filter (
                 where recon_state in (
                     'UNEXPLAINED_ABSENT',
                     'REJECTED_BUT_PRESENT',
                     'NOT_ELIGIBLE_BUT_PRESENT'
                 )
             ) = 0
        then 'PASS'
        else 'FAIL'
    end as status

from matrix
group by infotype
order by infotype
