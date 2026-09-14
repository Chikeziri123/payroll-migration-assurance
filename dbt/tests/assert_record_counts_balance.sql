-- A failing dbt test is a failed reconciliation.
--
-- This is the top level assertion: every infotype must reconcile. It
-- returns the summary rows that did not, so the failure message points
-- straight at which infotype to look at rather than at a row count.

select *
from {{ ref('recon_record_counts') }}
where status = 'FAIL'
