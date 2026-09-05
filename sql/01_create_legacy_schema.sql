-- =====================================================================
-- LEGACY SOURCE SCHEMA
-- =====================================================================
-- Represents two source systems as they would be received in a
-- migration extract: HR.net (HR master data) and MiraclePay (payroll).
--
-- Design principle: this schema is deliberately imperfect. It carries
-- the weaknesses real legacy systems have, because the migration
-- challenge is in the imperfection, not in the volume.
--
-- Note the near-total absence of constraints. Extracts arrive as flat
-- files from systems whose own validation is unknown, so the landing
-- layer accepts what it is given and profiling reports on it. Adding
-- constraints here would silently reject the very defects we are
-- meant to find.
--
-- Effective dating (Option A): neither source holds a change history
-- for most fields. Only the payment history table carries temporal
-- information, and SAP basic pay history must be inferred from it.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS legacy;


-- ---------------------------------------------------------------------
-- HR.net: employee master
-- ---------------------------------------------------------------------
-- One row per employee, holding current state only. Dates are stored
-- as text because the source system permits free-text entry, which is
-- the origin of a large share of migration date defects.
CREATE TABLE IF NOT EXISTS legacy.hrnet_employee (
    emp_id              text,           -- HR.net internal key
    ni_number           text,           -- National Insurance number
    title               text,
    first_name          text,
    middle_name         text,
    last_name           text,
    known_as            text,
    date_of_birth       text,           -- free text in source
    gender              text,           -- inconsistent coding in source
    marital_status      text,
    nationality         text,
    hire_date           text,           -- free text in source
    leaver_date         text,           -- null if still employed
    leaver_reason       text,
    employment_status   text,
    job_title           text,
    department          text,
    cost_centre         text,
    location            text,
    manager_emp_id      text,           -- self-referencing, may be orphaned
    contract_type       text,
    fte                 text,           -- stored as text, may be "1", "1.0", "100%"
    extracted_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- HR.net: addresses
-- ---------------------------------------------------------------------
-- Separate table, one or more rows per employee. No enforced single
-- primary address, which is itself a data quality problem to find.
CREATE TABLE IF NOT EXISTS legacy.hrnet_address (
    emp_id              text,
    address_type        text,           -- HOME, POSTAL, EMERGENCY
    address_line_1      text,
    address_line_2      text,
    city                text,
    county              text,
    postcode            text,
    country             text,
    is_primary          text,           -- "Y"/"N"/"YES"/"1", inconsistent
    extracted_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- MiraclePay: payroll master
-- ---------------------------------------------------------------------
-- One row per employee, current state. Uses its own key (payroll_ref)
-- rather than HR.net's emp_id, so the two systems must be matched on
-- NI number. That match is imperfect and is a core migration risk.
CREATE TABLE IF NOT EXISTS legacy.miraclepay_employee (
    payroll_ref         text,           -- MiraclePay internal key
    ni_number           text,           -- the join key to HR.net
    surname             text,           -- may differ from HR.net
    forename            text,
    dob                 text,
    tax_code            text,
    ni_category         text,
    annual_salary       text,           -- stored as text, may carry symbols
    pay_frequency       text,
    pay_scale           text,
    cost_centre         text,           -- may conflict with HR.net
    start_date          text,
    termination_date    text,
    payment_method      text,
    pension_scheme      text,
    extracted_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- MiraclePay: bank details
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legacy.miraclepay_bank (
    payroll_ref         text,
    bank_name           text,
    sort_code           text,           -- format varies: 12-34-56, 123456
    account_number      text,           -- may have lost leading zeros
    account_holder      text,
    building_society_ref text,
    is_active           text,
    extracted_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- MiraclePay: payment history
-- ---------------------------------------------------------------------
-- The only genuinely temporal table in either source. One row per
-- employee per pay period. Basic pay history for SAP PA0008 must be
-- inferred from step changes in gross_pay across consecutive periods.
--
-- The inference is imperfect: mid-period changes, unpaid leave and
-- bonuses paid through the same line all distort the signal. Each
-- distortion requires a documented rule, and those rules are the
-- migration assumptions the business must accept.
CREATE TABLE IF NOT EXISTS legacy.miraclepay_payment_history (
    payroll_ref         text,
    pay_period          text,           -- "2024-04", "APR24", inconsistent
    period_start        text,
    period_end          text,
    gross_pay           text,
    basic_pay           text,
    overtime            text,
    bonus               text,
    tax_deducted        text,
    ni_deducted         text,
    pension_deducted    text,
    net_pay             text,
    extracted_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- Extract control
-- ---------------------------------------------------------------------
-- Records what was extracted, when, and how many rows. In a real
-- migration this is the first reconciliation point: does the number of
-- rows landed match the number the source system reported sending?
CREATE TABLE IF NOT EXISTS legacy.extract_control (
    extract_id          bigserial PRIMARY KEY,
    source_system       text NOT NULL,
    table_name          text NOT NULL,
    rows_extracted      integer NOT NULL,
    extract_started_at  timestamptz NOT NULL,
    extract_finished_at timestamptz NOT NULL DEFAULT now(),
    notes               text
);