-- Nothing disappears silently.
--
-- An eligible person with no record in an infotype and no reject saying
-- why is the single worst outcome in a data migration, because nobody
-- knows it happened. Everything else on this list is a known problem.
--
-- This is expected to fail on the current build. There is one PERNR
-- absent from PA0000, PA0001, PA0002 and PA0008 with no reject anywhere
-- accounting for it.

select *
from {{ ref('recon_exceptions') }}
where recon_state = 'UNEXPLAINED_ABSENT'
