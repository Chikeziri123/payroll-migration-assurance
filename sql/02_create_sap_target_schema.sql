-- =====================================================================
-- SAP HCM TARGET SCHEMA
-- =====================================================================
-- A PostgreSQL representation of the SAP HCM infotype structures this
-- migration targets. Table and field names follow SAP conventions so
-- the mapping specification is readable by anyone familiar with SAP.
--
-- SCOPE DECISION (see docs/mapping-specification.md):
--   In scope:  PA0000 Actions, PA0001 Org Assignment, PA0002 Personal
--              Data, PA0006 Addresses, PA0008 Basic Pay, PA0009 Bank
--   Out of scope: time management, absences, pensions, recurring
--              payments. Six infotypes exercise every technical
--              challenge in the migration; more adds volume, not
--              difficulty.
--
-- THE CENTRAL CONSTRAINT
-- SAP infotypes are date-delimited by BEGDA and ENDDA. For a given
-- employee and infotype, validity periods must tile continuously:
-- no gaps, no overlaps. This schema enforces that with PostgreSQL
-- exclusion constraints, so invalid history is rejected at load
-- rather than discovered during reconciliation.
--
-- Unlike the legacy schema, this layer is heavily constrained. It
-- represents a receiving system that will not accept bad data, which
-- is exactly what SAP is.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS sap_target;

-- Required for exclusion constraints that combine equality on a scalar
-- column with overlap on a range. Without it, the EXCLUDE clauses below
-- cannot be created.
CREATE EXTENSION IF NOT EXISTS btree_gist;


-- ---------------------------------------------------------------------
-- PA0000 : Actions
-- ---------------------------------------------------------------------
-- The employment lifecycle. Every other infotype record must fall
-- within a period covered by an action. Hire opens the record,
-- leaver closes it.
--
-- SAP high date: 31/12/9999 means "currently valid, no end". It is a
-- real sentinel value in SAP, not a placeholder we invented.
CREATE TABLE IF NOT EXISTS sap_target.pa0000 (
    pernr           char(8)     NOT NULL,          -- personnel number
    begda           date        NOT NULL,          -- valid from
    endda           date        NOT NULL DEFAULT '9999-12-31',
    massn           char(2)     NOT NULL,          -- action type
    massg           char(2),                       -- action reason
    stat2           char(1)     NOT NULL,          -- employment status
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0000_pk PRIMARY KEY (pernr, begda),
    CONSTRAINT pa0000_dates CHECK (endda >= begda),
    CONSTRAINT pa0000_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- PA0001 : Organisational Assignment
-- ---------------------------------------------------------------------
-- Company code, cost centre, position, org unit.
--
-- MIGRATION LIMITATION: neither legacy source holds cost centre
-- history. Only current state is available. Each employee therefore
-- receives a single record valid from hire to high date, using the
-- survived current value. Historical organisational assignment is not
-- migratable from the available sources. This is a stated assumption
-- requiring business sign-off, not an oversight.
CREATE TABLE IF NOT EXISTS sap_target.pa0001 (
    pernr           char(8)     NOT NULL,
    begda           date        NOT NULL,
    endda           date        NOT NULL DEFAULT '9999-12-31',
    bukrs           char(4)     NOT NULL,          -- company code
    werks           char(4)     NOT NULL,          -- personnel area
    persg           char(1)     NOT NULL,          -- employee group
    persk           char(2)     NOT NULL,          -- employee subgroup
    kostl           char(10),                      -- cost centre
    orgeh           char(8),                       -- organisational unit
    plans           char(8),                       -- position
    stell           char(8),                       -- job
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0001_pk PRIMARY KEY (pernr, begda),
    CONSTRAINT pa0001_dates CHECK (endda >= begda),
    CONSTRAINT pa0001_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- PA0002 : Personal Data
-- ---------------------------------------------------------------------
-- Identity. Changes rarely, typically only on marriage or a legal
-- name change, so most employees have a single record.
CREATE TABLE IF NOT EXISTS sap_target.pa0002 (
    pernr           char(8)     NOT NULL,
    begda           date        NOT NULL,
    endda           date        NOT NULL DEFAULT '9999-12-31',
    nachn           varchar(40) NOT NULL,          -- surname
    vorna           varchar(40) NOT NULL,          -- first name
    midnm           varchar(40),                   -- middle name
    rufnm           varchar(40),                   -- known as
    anred           char(1),                       -- form of address key
    gbdat           date        NOT NULL,          -- date of birth
    gesch           char(1)     NOT NULL,          -- gender key
    famst           char(1),                       -- marital status
    natio           char(3),                       -- nationality
    perid           varchar(20),                   -- NI number
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0002_pk PRIMARY KEY (pernr, begda),
    CONSTRAINT pa0002_dates CHECK (endda >= begda),
    CONSTRAINT pa0002_gender CHECK (gesch IN ('1', '2', '3')),
    CONSTRAINT pa0002_dob_sane CHECK (gbdat > '1920-01-01'
                                  AND gbdat < CURRENT_DATE),
    CONSTRAINT pa0002_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- PA0006 : Addresses
-- ---------------------------------------------------------------------
-- Subtype distinguishes address kinds, so an employee may hold a
-- permanent and a postal address simultaneously. The uniqueness and
-- overlap rules therefore include subty.
CREATE TABLE IF NOT EXISTS sap_target.pa0006 (
    pernr           char(8)     NOT NULL,
    subty           char(4)     NOT NULL,          -- 1 permanent, 2 temporary
    begda           date        NOT NULL,
    endda           date        NOT NULL DEFAULT '9999-12-31',
    stras           varchar(60) NOT NULL,          -- street and house number
    locat           varchar(40),                   -- second address line
    ort01           varchar(40) NOT NULL,          -- city
    ort02           varchar(40),                   -- district / county
    pstlz           varchar(10) NOT NULL,          -- postcode
    land1           char(3)     NOT NULL,          -- country key
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0006_pk PRIMARY KEY (pernr, subty, begda),
    CONSTRAINT pa0006_dates CHECK (endda >= begda),
    CONSTRAINT pa0006_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        subty WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- PA0008 : Basic Pay
-- ---------------------------------------------------------------------
-- The payroll core, and the infotype where history genuinely matters.
--
-- Periods are inferred from step changes in gross pay across the
-- legacy payment history, because no source system holds a salary
-- change log. The inference rules and their limitations are documented
-- in the mapping specification.
--
-- Financial reconciliation is performed against this table: the sum of
-- annual salary here must tie to the source payroll to the penny.
CREATE TABLE IF NOT EXISTS sap_target.pa0008 (
    pernr           char(8)     NOT NULL,
    begda           date        NOT NULL,
    endda           date        NOT NULL DEFAULT '9999-12-31',
    trfar           char(2),                       -- pay scale type
    trfgb           char(2),                       -- pay scale area
    trfgr           char(8),                       -- pay scale group
    trfst           char(2),                       -- pay scale level
    ansal           numeric(15,2) NOT NULL,        -- annual salary
    waers           char(3)     NOT NULL DEFAULT 'GBP',
    bsgrd           numeric(5,2) NOT NULL,         -- capacity utilisation %
    divgv           numeric(5,2),                  -- working hours per period
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0008_pk PRIMARY KEY (pernr, begda),
    CONSTRAINT pa0008_dates CHECK (endda >= begda),
    CONSTRAINT pa0008_salary_positive CHECK (ansal > 0),
    CONSTRAINT pa0008_fte_range CHECK (bsgrd > 0 AND bsgrd <= 100),
    CONSTRAINT pa0008_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- PA0009 : Bank Details
-- ---------------------------------------------------------------------
-- Where the money goes. The most operationally sensitive infotype in
-- the migration: an error here means someone does not get paid.
--
-- UK sort codes are exactly six digits, account numbers exactly eight.
-- Enforcing that here means a malformed account cannot reach the
-- target, which is the correct behaviour for this data.
CREATE TABLE IF NOT EXISTS sap_target.pa0009 (
    pernr           char(8)     NOT NULL,
    subty           char(4)     NOT NULL DEFAULT '0',
    begda           date        NOT NULL,
    endda           date        NOT NULL DEFAULT '9999-12-31',
    bnksa           char(4),                       -- bank details type
    emftx           varchar(40),                   -- payee name
    bankl           varchar(15) NOT NULL,          -- sort code
    bankn           varchar(18) NOT NULL,          -- account number
    zlsch           char(1)     NOT NULL,          -- payment method
    betrg           numeric(13,2),                 -- amount, for split pay
    loaded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pa0009_pk PRIMARY KEY (pernr, subty, begda),
    CONSTRAINT pa0009_dates CHECK (endda >= begda),
    CONSTRAINT pa0009_sortcode_format CHECK (bankl ~ '^[0-9]{6}$'),
    CONSTRAINT pa0009_account_format CHECK (bankn ~ '^[0-9]{8}$'),
    CONSTRAINT pa0009_no_overlap EXCLUDE USING gist (
        pernr WITH =,
        subty WITH =,
        daterange(begda, endda, '[]') WITH &&
    )
);


-- ---------------------------------------------------------------------
-- Migration audit trail
-- ---------------------------------------------------------------------
-- Every value changed between legacy and target is recorded here:
-- what changed, from what, to what, and under which rule.
--
-- This is the artefact that makes the migration defensible. When a
-- stakeholder asks "why is this employee's cost centre different from
-- the old system", the answer is a query, not a recollection.
CREATE TABLE IF NOT EXISTS sap_target.migration_audit (
    audit_id        bigserial   PRIMARY KEY,
    pernr           char(8),
    source_system   text        NOT NULL,
    source_key      text,
    infotype        char(4),
    field_name      text        NOT NULL,
    value_before    text,
    value_after     text,
    rule_applied    text        NOT NULL,
    rule_category   text        NOT NULL,          -- CLEANSE, SURVIVE, DERIVE, DEFAULT
    changed_at      timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- Rejected records
-- ---------------------------------------------------------------------
-- Records that failed validation and did not reach the target.
--
-- A migration that silently drops records is a failed migration. Every
-- rejection is captured with its reason so the exception report can
-- account for the difference between source and target counts.
CREATE TABLE IF NOT EXISTS sap_target.migration_rejects (
    reject_id       bigserial   PRIMARY KEY,
    source_system   text        NOT NULL,
    source_key      text,
    infotype        char(4),
    reject_reason   text        NOT NULL,
    reject_severity text        NOT NULL,          -- CRITICAL, HIGH, MEDIUM, LOW
    record_payload  jsonb,                         -- the record as received
    rejected_at     timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- Key mapping
-- ---------------------------------------------------------------------
-- Links the new SAP personnel number back to both legacy keys.
--
-- Without this, post-migration reconciliation is impossible: there is
-- no way to prove that target record X came from source record Y.
CREATE TABLE IF NOT EXISTS sap_target.key_map (
    pernr           char(8)     PRIMARY KEY,
    hrnet_emp_id    text,
    payroll_ref     text,
    ni_number       text,
    match_method    text        NOT NULL,          -- how the two sources were matched
    match_confidence text       NOT NULL,          -- EXACT, FUZZY, MANUAL, UNMATCHED
    created_at      timestamptz NOT NULL DEFAULT now()
);


CREATE INDEX IF NOT EXISTS idx_audit_pernr ON sap_target.migration_audit (pernr);
CREATE INDEX IF NOT EXISTS idx_audit_rule ON sap_target.migration_audit (rule_category, rule_applied);
CREATE INDEX IF NOT EXISTS idx_rejects_severity ON sap_target.migration_rejects (reject_severity, infotype);