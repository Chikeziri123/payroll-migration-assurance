# UK Payroll Migration Assurance Framework

A simulated HR and payroll data migration from two legacy systems into SAP HCM, built to demonstrate senior data migration analysis.

The point is not a dashboard. It is an assurance framework that can prove things about itself: a measured defect detection rate, a measured inference accuracy, an auditable survivorship trail, and a database that physically refuses invalid employment history.

## What it measures about itself

| Claim | Measured value |
|---|---|
| Defect detection | 48 rules find 94.5% of 2,650 seeded defects, 30 of 33 defect types at 100% |
| Salary inference accuracy | 0.022% day-weighted error on a £43,504 mean salary, £9.44 per person-day |
| Amount accuracy | 94.1% of inferred salaries within £1, 99.9% within 0.5% |
| Reconciliation | 23 dbt tests, all passing, source to target fully accounted for |

Every figure comes from running the code, not from an estimate.

## Two things stated openly

**Target is SAP-shaped, not SAP.** Real SAP table and field names (pa0000, pernr, begda, endda) implemented in PostgreSQL, because SAP has no free tier. The structures, validity period semantics and infotype relationships are real. The system is not.

**All data is synthetic.** HR and payroll data is personal data, so it is generated rather than sourced. This is what makes the rest possible: the generator plants 2,650 known defects across 33 types, which gives ground truth, which is what allows the framework to score itself.

## Architecture

Two legacy sources, 90,284 rows, land untyped so that defects survive into the landing layer rather than being rejected at load. A 48-rule profiling engine scores them and scores its own detection rate against the seeded defects. Records are matched, cleansed and resolved into a single golden record per person, with every change written to an audit trail. Six SAP infotypes are then constructed, and a dbt layer reconciles the result as tests rather than reports.

    legacy  →  profiling  →  staging.golden_employee  →  sap_target  →  recon

**Matching.** 2,043 exact on normalised NI number, 178 fuzzy on surname plus date of birth, 75 HR only, 38 payroll only. 2,334 people in total, which reconciles exactly against both source populations. 57 ambiguous NI numbers are excluded from exact matching entirely, because a naive join on a field containing duplicates silently multiplies rows, and in a migration that means creating employees who do not exist.

**Validity periods enforced by the database.** Every infotype carries a PostgreSQL exclusion constraint on (pernr, daterange(begda, endda)), so overlapping employment periods are rejected at insert rather than found later.

**Salary history inferred, not copied.** Neither source records why pay changed. PA0008 segments are derived from step changes in payment history, including an algebraic solver for mid-month transitions where a month is a blend of two rates. Accuracy is scored against ground truth that the constructor never reads.

## What the reconciliation found

The dbt layer exists to fail when the migration is wrong. On its first run it found three defects that nothing else had caught:

- **PA0006 had no reject branch.** Seven employees with an address but no postcode were dropped silently, while PA0002 and PA0009 correctly wrote rejects for their own missing fields.
- **Hire date rejects were unusable.** An employee whose hire date was the string `n/a` lost four infotypes at once. A reject existed, but recorded against the person rather than the infotypes, so the reconciliation could not consume it.
- **A salary segment outlived its employment.** Where a leaving date coincided with a salary change, the final PA0008 segment was clamped forward and ended after the employee had left.

All three were fixed. The build is green because the defects are gone, not because the tests were loosened.

## Design decisions

**Date of birth gets no survivorship rule.** Where the sources disagree, the field is nulled, the record flagged critical, and a human decides. Automating it would be faster and occasionally wrong, and occasionally wrong is not acceptable for a field driving NI category and pension auto-enrolment.

**Cost centre survives from payroll, identity from HR.** Payroll cost centre determines where salary cost posts to the general ledger. HR is the system of record for identity, because payroll surnames go stale after marriage.

**Multiple active bank accounts are rejected outright.** The migration cannot choose which account to pay.

**Rules live in YAML, not code.** A data quality rule is a business statement a payroll manager can dispute. Buried in Python it never gets reviewed.

**Eligibility and rejection are kept separate.** An employee correctly has no PA0006 if they have no address. The reconciliation distinguishes "nothing to migrate" from "something lost", which is what lets the control totals balance rather than carrying a permanent unexplained gap.

## Known limitations

These are in the project because they are true.

**Five defect types the profiler cannot fully see.** Postcodes like ZZ99 9ZZ match the UK pattern perfectly and no such postcode exists; catching them needs a lookup. FTE stored as "1" versus "1.0" is inconsistent but not invalid. Someone aged 85 hired thirty years ago was 55 at hire, which is plausible.

**62% of salary changes predate the payment window.** Careers run to 35 years; the payment history covers 4. Changes before the first payment record have no evidence and cannot be inferred. Scoring is restricted to the observable window and the restriction is printed in the output rather than hidden.

**Historical organisational assignment is not migratable.** Neither source holds cost centre history, so each employee gets a single PA0001 record. Historical payroll cost in SAP will attribute to the current cost centre. Open issue OI-01.

**Profiling runs in pandas, not SQL.** 90,000 rows fits in memory. At ten million this would be wrong and the rules would be rewritten as SQL.

## Stack

PostgreSQL 18, Python 3.12, dbt Core 1.12 with dbt-postgres, pandas, pydantic, Faker, Power BI.

## Status

Phases 0 to 7 complete. Power BI control dashboard and the reconciliation evidence pack are in progress.
