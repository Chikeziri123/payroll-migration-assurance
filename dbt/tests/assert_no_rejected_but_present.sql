-- A reject that did not reject is a lie in the audit trail.
--
-- If migration_rejects says a record was excluded from an infotype and
-- the infotype contains that record anyway, then the reject table cannot
-- be relied on to explain anything, and the reconciliation built on top
-- of it means nothing.
--
-- This is expected to fail on the current build. PA0009 holds 2209
-- records and carries 174 rejects against a population of 2334, so at
-- least 49 people are in both.

select *
from {{ ref('recon_exceptions') }}
where recon_state = 'REJECTED_BUT_PRESENT'
