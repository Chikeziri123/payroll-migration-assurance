-- An advisory is not an exclusion.
--
-- The 292 person level rejects are control observations about people who
-- were migrated: fuzzy matches to confirm, HR records with no payroll
-- counterpart, payroll records with no HR counterpart. If one of them
-- turns out to name somebody who is not in the migrated population, then
-- the advisory quietly excluded a record, which is worse than a blocking
-- reject because it was never meant to block anything.
--
-- Together with assert_no_rejected_but_present this proves the reject
-- table means what it says in both directions.

select r.*
from {{ ref('int_rejects_resolved') }} r
left join {{ ref('int_person_spine') }} s on s.pernr = r.pernr
where r.reject_class = 'ADVISORY'
  and s.pernr is null
