-- No employee exists in the target who does not exist in the source.
--
-- The completeness matrix is built outward from key_map, so a target
-- record whose PERNR was never issued would be invisible to it. This
-- checks the other direction. A naive join on a field with duplicates
-- silently multiplies rows, which in a migration means creating
-- employees in SAP who do not exist, and this is the check that would
-- catch it.

with target_pernrs as (

    select trim(pernr) as pernr from {{ source('sap_target', 'pa0000') }}
    union
    select trim(pernr) from {{ source('sap_target', 'pa0001') }}
    union
    select trim(pernr) from {{ source('sap_target', 'pa0002') }}
    union
    select trim(pernr) from {{ source('sap_target', 'pa0006') }}
    union
    select trim(pernr) from {{ source('sap_target', 'pa0008') }}
    union
    select trim(pernr) from {{ source('sap_target', 'pa0009') }}

)

select t.pernr
from target_pernrs t
left join {{ ref('int_person_spine') }} s on s.pernr = t.pernr
where s.pernr is null
