-- Nothing appears from nowhere.
--
-- The mirror of unexplained absence, and the more dangerous half. A
-- PA0009 record for somebody with no surviving bank details means SAP
-- holds a payment instruction that no source system supports. In a real
-- payroll that is money moving to an account nobody authorised.

select *
from {{ ref('recon_exceptions') }}
where recon_state = 'NOT_ELIGIBLE_BUT_PRESENT'
