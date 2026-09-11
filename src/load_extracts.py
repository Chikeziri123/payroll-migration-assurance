"""
Extract loader.

Lands the generated CSV extracts into PostgreSQL:

  - Source extracts go to the `legacy` schema, exactly as received
  - Answer keys go to a separate `ground_truth` schema
  - Every load writes a row to `legacy.extract_control`

THREE DESIGN DECISIONS

1. Everything loads as text.
   No parsing, no cleaning, no type conversion. If the date columns
   were typed as DATE, PostgreSQL would reject the unparseable values
   at load and the defects would never reach the profiling engine. A
   landing layer that rejects bad data cannot be used to find bad data.

2. Answer keys are physically separated.
   The truth files land in `ground_truth`, not `legacy`. The
   transformation code is given access to `legacy` only. That
   separation is what makes the detection rates in Phase 4 honest: if
   the profiler could read the answers, measuring it would be
   meaningless.

3. Extract control is written for every table.
   Row counts in, timestamps, source system. This is the first
   reconciliation point in a real migration, answering "does what
   landed match what the source system said it sent".

IDEMPOTENCY
Each table is truncated before load. Re-running the loader replaces the
data rather than appending to it, so a dress rehearsal can be repeated
without cumulative duplication.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
except ImportError:
    print("psycopg is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("python-dotenv is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


load_dotenv()


# =====================================================================
# Table definitions
# =====================================================================
# Column order must match the CSV header order produced by the
# generator. Declaring it explicitly rather than trusting the file
# means a changed CSV fails loudly here instead of silently loading
# values into the wrong columns.

SOURCE_TABLES: list[tuple[str, str, str, list[str]]] = [
    (
        "hrnet_employee.csv",
        "legacy.hrnet_employee",
        "HR.net",
        [
            "emp_id", "ni_number", "title", "first_name", "middle_name",
            "last_name", "known_as", "date_of_birth", "gender",
            "marital_status", "nationality", "hire_date", "leaver_date",
            "leaver_reason", "employment_status", "job_title", "department",
            "cost_centre", "location", "manager_emp_id", "contract_type", "fte",
        ],
    ),
    (
        "hrnet_address.csv",
        "legacy.hrnet_address",
        "HR.net",
        [
            "emp_id", "address_type", "address_line_1", "address_line_2",
            "city", "county", "postcode", "country", "is_primary",
        ],
    ),
    (
        "miraclepay_employee.csv",
        "legacy.miraclepay_employee",
        "MiraclePay",
        [
            "payroll_ref", "ni_number", "surname", "forename", "dob",
            "tax_code", "ni_category", "annual_salary", "pay_frequency",
            "pay_scale", "cost_centre", "start_date", "termination_date",
            "payment_method", "pension_scheme",
        ],
    ),
    (
        "miraclepay_bank.csv",
        "legacy.miraclepay_bank",
        "MiraclePay",
        [
            "payroll_ref", "bank_name", "sort_code", "account_number",
            "account_holder", "building_society_ref", "is_active",
        ],
    ),
    (
        "miraclepay_payment_history.csv",
        "legacy.miraclepay_payment_history",
        "MiraclePay",
        [
            "payroll_ref", "pay_period", "period_start", "period_end",
            "gross_pay", "basic_pay", "overtime", "bonus", "tax_deducted",
            "ni_deducted", "pension_deducted", "net_pay",
        ],
    ),
]

TRUTH_TABLES: list[tuple[str, str, list[str]]] = [
    (
        "_truth_salary_history.csv",
        "ground_truth.salary_history",
        ["payroll_ref", "sequence", "effective_from", "annual_salary", "change_reason"],
    ),
    (
        "_truth_seeded_defects.csv",
        "ground_truth.seeded_defects",
        ["defect_code", "source_table", "source_key", "field_name",
         "value_before", "value_after", "note"],
    ),
]


GROUND_TRUTH_DDL = """
-- =====================================================================
-- GROUND TRUTH SCHEMA
-- =====================================================================
-- The answer key. Holds the true salary history and every seeded
-- defect, written by the generator.
--
-- This schema exists so profiling and derivation accuracy can be
-- measured rather than asserted. It is deliberately kept apart from
-- the legacy schema, and no transformation code reads from it.
--
-- In a real migration there is no equivalent: you never know the true
-- answer. That is precisely why a synthetic population is the right
-- choice for an assurance framework, because it is the only way to
-- prove the assurance itself works.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS ground_truth;

CREATE TABLE IF NOT EXISTS ground_truth.salary_history (
    payroll_ref     text,
    sequence        text,
    effective_from  text,
    annual_salary   text,
    change_reason   text,
    loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ground_truth.seeded_defects (
    defect_code     text,
    source_table    text,
    source_key      text,
    field_name      text,
    value_before    text,
    value_after     text,
    note            text,
    loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seeded_defects_code
    ON ground_truth.seeded_defects (defect_code);
CREATE INDEX IF NOT EXISTS idx_seeded_defects_key
    ON ground_truth.seeded_defects (source_table, source_key);
CREATE INDEX IF NOT EXISTS idx_salary_history_ref
    ON ground_truth.salary_history (payroll_ref);
"""


# =====================================================================
# Connection
# =====================================================================

def connect() -> psycopg.Connection:
    """Open a database connection from environment variables."""
    required = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing environment variables: {', '.join(missing)}. "
            "Check that .env exists and is populated."
        )

    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


# =====================================================================
# CSV reading
# =====================================================================

def read_csv(path: Path, expected_columns: list[str]) -> list[tuple]:
    """
    Read a CSV into tuples, validating the header.

    Validating the header is not ceremony. If the generator changes a
    column order and the loader does not notice, values land in the
    wrong columns and every downstream check becomes meaningless while
    still appearing to pass.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Extract not found: {path}. "
            "Run src/generator/generate_population.py first."
        )

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []

        header = [h.strip().lstrip("\ufeff") for h in header]

        if header != expected_columns:
            only_in_file = [c for c in header if c not in expected_columns]
            only_expected = [c for c in expected_columns if c not in header]
            raise ValueError(
                f"Header mismatch in {path.name}.\n"
                f"  Expected: {expected_columns}\n"
                f"  Found:    {header}\n"
                f"  Unexpected columns: {only_in_file}\n"
                f"  Missing columns:    {only_expected}"
            )

        rows = []
        for line_no, row in enumerate(reader, start=2):
            if len(row) != len(expected_columns):
                raise ValueError(
                    f"{path.name} line {line_no}: expected "
                    f"{len(expected_columns)} fields, found {len(row)}"
                )
            # Empty string and NULL are different things. The source
            # systems store blanks, not nulls, so blanks are preserved
            # as blanks.
            rows.append(tuple(row))

    return rows


# =====================================================================
# Loading
# =====================================================================

def load_table(
    conn: psycopg.Connection,
    table: str,
    columns: list[str],
    rows: list[tuple],
    batch_size: int = 5000,
) -> int:
    """
    Truncate and load a table.

    Uses COPY, which is markedly faster than INSERT for the 80,000 row
    payment history. Truncating first makes the load idempotent, so a
    dress rehearsal can be repeated without duplicating data.
    """
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table}")

        if not rows:
            return 0

        collist = ", ".join(columns)
        with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)

    return len(rows)


def write_extract_control(
    conn: psycopg.Connection,
    source_system: str,
    table_name: str,
    row_count: int,
    started_at: datetime,
    notes: str = "",
) -> None:
    """Record the load in the extract control table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO legacy.extract_control
                (source_system, table_name, rows_extracted,
                 extract_started_at, notes)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (source_system, table_name, row_count, started_at, notes),
        )


def verify_counts(conn: psycopg.Connection, expected: dict[str, int]) -> list[str]:
    """
    Confirm the database holds what the files held.

    This is the first reconciliation in the migration and it is
    deliberately simple: did every row that left the file arrive in the
    table? A load that silently drops rows is the worst kind of
    failure, because everything downstream looks fine.
    """
    problems = []
    with conn.cursor() as cur:
        for table, expected_rows in expected.items():
            cur.execute(f"SELECT count(*) FROM {table}")
            actual = cur.fetchone()[0]
            if actual != expected_rows:
                problems.append(
                    f"{table}: file held {expected_rows:,}, "
                    f"table holds {actual:,} (difference {actual - expected_rows:+,})"
                )
    return problems


# =====================================================================
# Entry point
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Load generated extracts into the legacy and ground_truth schemas."
    )
    ap.add_argument("--input", type=str, default="output/extracts",
                    help="Directory holding the generated CSV extracts")
    ap.add_argument("--skip-truth", action="store_true",
                    help="Load source extracts only, leaving answer keys untouched")
    args = ap.parse_args()

    in_dir = Path(args.input)
    started = datetime.now(timezone.utc)

    print("=" * 68)
    print("EXTRACT LOADER")
    print("=" * 68)
    print(f"Input directory : {in_dir}")
    print(f"Target database : {os.environ.get('PGDATABASE', '(not set)')}"
          f" on {os.environ.get('PGHOST', '(not set)')}")
    print()

    # Read every file before touching the database. A header mismatch
    # in the last file should not leave the first four already loaded.
    print("Reading extracts...")
    source_data: list[tuple[str, str, list[str], list[tuple]]] = []
    for filename, table, system, columns in SOURCE_TABLES:
        rows = read_csv(in_dir / filename, columns)
        source_data.append((table, system, columns, rows))
        print(f"  {filename:<34} {len(rows):>8,} rows")

    truth_data: list[tuple[str, list[str], list[tuple]]] = []
    if not args.skip_truth:
        for filename, table, columns in TRUTH_TABLES:
            rows = read_csv(in_dir / filename, columns)
            truth_data.append((table, columns, rows))
            print(f"  {filename:<34} {len(rows):>8,} rows   (answer key)")

    print()
    print("Connecting...")
    conn = connect()

    try:
        if not args.skip_truth:
            print("Ensuring ground_truth schema exists...")
            with conn.cursor() as cur:
                cur.execute(GROUND_TRUTH_DDL)
            conn.commit()

        expected: dict[str, int] = {}

        print()
        print("Loading source extracts...")
        for table, system, columns, rows in source_data:
            n = load_table(conn, table, columns, rows)
            write_extract_control(conn, system, table, n, started)
            expected[table] = n
            print(f"  {table:<40} {n:>8,} rows")
        conn.commit()

        if truth_data:
            print()
            print("Loading answer keys...")
            for table, columns, rows in truth_data:
                n = load_table(conn, table, columns, rows)
                expected[table] = n
                print(f"  {table:<40} {n:>8,} rows")
            conn.commit()

        print()
        print("Verifying row counts...")
        problems = verify_counts(conn, expected)
        if problems:
            print()
            print("  RECONCILIATION FAILED")
            for pr in problems:
                print(f"    {pr}")
            raise SystemExit(1)
        print("  All tables reconcile to their source files.")

        # Load summary from extract control, which is what a real
        # migration reports rather than the loader's own print output.
        print()
        print("-" * 68)
        print("EXTRACT CONTROL")
        print("-" * 68)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_system, table_name, rows_extracted
                FROM legacy.extract_control
                WHERE extract_started_at = %s
                ORDER BY source_system, table_name
                """,
                (started,),
            )
            total = 0
            for system, table, count in cur.fetchall():
                total += count
                print(f"  {system:<12} {table:<40} {count:>8,}")
            print("  " + "-" * 62)
            print(f"  {'':<12} {'TOTAL SOURCE ROWS':<40} {total:>8,}")

    finally:
        conn.close()

    print()
    print("Load complete.")


if __name__ == "__main__":
    main()
