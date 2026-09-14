-- 00_introspect_schema.sql
--
-- Run each statement separately in the pgAdmin Query Tool and paste the
-- results back. The handover gives row counts and design intent but not
-- column names, and I am not going to guess at the field names in
-- migration_rejects, key_map or the infotype tables. Reconciliation SQL
-- written against invented columns fails at compile time at best and
-- silently reconciles the wrong thing at worst.


-- 1. Every column in the three schemas the reconciliation reads,
--    collapsed to one row per table so the paste stays manageable.

SELECT table_schema || '.' || table_name AS tbl,
       string_agg(column_name || ' ' || data_type, ', '
                  ORDER BY ordinal_position) AS columns
FROM information_schema.columns
WHERE table_schema IN ('legacy', 'staging', 'sap_target')
GROUP BY 1
ORDER BY 1;


-- 2. Sample rows. These tell me the shape of the values, not just the
--    names, which matters most for the reject and audit tables.

SELECT * FROM sap_target.migration_rejects LIMIT 5;


-- 3.

SELECT * FROM sap_target.key_map LIMIT 3;


-- 4.

SELECT * FROM sap_target.migration_audit LIMIT 3;


-- 5. The distinct action types in PA0000. Record count reconciliation for
--    PA0000 is not one row per employee, it is one hire action per
--    employee plus one leaving action per leaver, so I need to know how
--    those are coded before I can assert the count.
--    Adjust the column name if the introspection above shows it is not
--    called massn.

SELECT massn, COUNT(*) AS rows
FROM sap_target.pa0000
GROUP BY massn
ORDER BY 2 DESC;


-- 6. Which columns on PA0008 carry the pay amount and any period
--    indicator. Needed to define the financial control total correctly.

SELECT * FROM sap_target.pa0008 LIMIT 3;


-- 7. Payment history shape, for the historical cost control total.

SELECT * FROM legacy.miraclepay_payment_history LIMIT 3;
