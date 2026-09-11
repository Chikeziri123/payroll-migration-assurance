"""
Defect catalogue for the synthetic legacy population.

WHY THIS MODULE EXISTS
A migration assurance framework that cannot be measured is an assertion.
This module seeds known defects into the generated population at
controlled rates, and records every one of them to a ground truth log.

Phase 4's profiling engine is then measured against that log, producing
a detection rate per defect type. That turns "the profiler found some
problems" into "the profiler detected 94 per cent of seeded National
Insurance defects and missed the following six", which is a claim that
can be defended.

DEFECT SELECTION
Every defect below is one that genuinely occurs in HR and payroll
migrations. They are not invented to make the profiler look clever.
Sources of each pattern are noted in the comments.

RATES
Rates are deliberately higher than a well-run system would carry,
because a 0.1 per cent defect rate across 2,300 records produces two
instances and no statistical signal. These rates are typical of what
an ageing system that has been through acquisitions actually holds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


# =====================================================================
# Defect definitions
# =====================================================================

@dataclass(frozen=True)
class DefectType:
    code: str
    name: str
    description: str
    target_table: str
    target_field: str
    rate: float            # proportion of eligible records affected
    severity: str          # expected severity when detected
    detectable: bool       # whether profiling can reasonably find it


DEFECT_CATALOGUE: list[DefectType] = [

    # -----------------------------------------------------------------
    # National Insurance number defects
    # -----------------------------------------------------------------
    # The most consequential field in UK payroll. An invalid NI number
    # blocks HMRC Real Time Information submission.
    DefectType(
        code="NI-01",
        name="Malformed NI number",
        description=(
            "NI number fails the HMRC format. Typically a transposed "
            "character or an invalid prefix such as BG, GB, NK, KN, TN "
            "or ZZ, all of which are reserved and never issued."
        ),
        target_table="miraclepay_employee",
        target_field="ni_number",
        rate=0.030,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="NI-02",
        name="Duplicate NI number",
        description=(
            "The same NI number appears against two different employees. "
            "Usually caused by a keying error, occasionally by a genuine "
            "duplicate person record."
        ),
        target_table="miraclepay_employee",
        target_field="ni_number",
        rate=0.015,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="NI-03",
        name="Missing NI number",
        description=(
            "No NI number recorded. Common for recent starters and "
            "overseas hires awaiting allocation."
        ),
        target_table="miraclepay_employee",
        target_field="ni_number",
        rate=0.012,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="NI-04",
        name="NI number formatting variance",
        description=(
            "Valid NI number stored with inconsistent spacing or case, "
            "for example 'ab 12 34 56 c'. Not a defect in itself, but it "
            "breaks the exact match between systems if not normalised."
        ),
        target_table="miraclepay_employee",
        target_field="ni_number",
        rate=0.060,
        severity="LOW",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Date defects
    # -----------------------------------------------------------------
    DefectType(
        code="DT-01",
        name="Unparseable date",
        description=(
            "Date held as free text that cannot be parsed, for example "
            "'unknown', 'see file', '00/00/0000' or a partial date."
        ),
        target_table="hrnet_employee",
        target_field="hire_date",
        rate=0.022,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="DT-02",
        name="Ambiguous date format",
        description=(
            "Date where day and month could be transposed, for example "
            "05/03/2019. Cannot be resolved from the data alone."
        ),
        target_table="hrnet_employee",
        target_field="hire_date",
        rate=0.035,
        severity="MEDIUM",
        detectable=True,
    ),
    DefectType(
        code="DT-03",
        name="Leaver date before hire date",
        description=(
            "Logically impossible employment period. Usually a keying "
            "error in the year."
        ),
        target_table="hrnet_employee",
        target_field="leaver_date",
        rate=0.008,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="DT-04",
        name="Implausible date of birth",
        description=(
            "Date of birth implying an age under 16 or over 80 at hire. "
            "Frequently a default value such as 01/01/1900 or a "
            "mis-keyed year."
        ),
        target_table="hrnet_employee",
        target_field="date_of_birth",
        rate=0.010,
        severity="HIGH",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Cross-system conflicts
    # -----------------------------------------------------------------
    # The defining problem of a two-source migration.
    DefectType(
        code="XS-01",
        name="Cost centre conflict",
        description=(
            "HR and payroll hold different cost centres for the same "
            "employee. Typically a transfer processed in one system and "
            "not the other."
        ),
        target_table="miraclepay_employee",
        target_field="cost_centre",
        rate=0.070,
        severity="MEDIUM",
        detectable=True,
    ),
    DefectType(
        code="XS-02",
        name="Surname conflict",
        description=(
            "HR and payroll hold different surnames, usually because a "
            "name change after marriage was recorded in one system only."
        ),
        target_table="miraclepay_employee",
        target_field="surname",
        rate=0.025,
        severity="LOW",
        detectable=True,
    ),
    DefectType(
        code="XS-03",
        name="Date of birth conflict",
        description=(
            "HR and payroll hold different dates of birth. No automatic "
            "survivorship is applied, because the field drives NI "
            "category and pension auto-enrolment."
        ),
        target_table="miraclepay_employee",
        target_field="dob",
        rate=0.012,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="XS-04",
        name="Employment date conflict",
        description=(
            "HR hire date differs materially from the payroll start "
            "date, beyond the normal payroll cut-off difference."
        ),
        target_table="miraclepay_employee",
        target_field="start_date",
        rate=0.030,
        severity="LOW",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Record matching defects
    # -----------------------------------------------------------------
    DefectType(
        code="MT-01",
        name="HR only, no payroll record",
        description=(
            "Employee exists in HR.net with no corresponding payroll "
            "record. May be a genuine non-paid worker or a new starter "
            "not yet set up."
        ),
        target_table="hrnet_employee",
        target_field="__record__",
        rate=0.018,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="MT-02",
        name="Payroll only, no HR record",
        description=(
            "Employee is being paid with no HR record. A control concern "
            "rather than a data quality issue."
        ),
        target_table="miraclepay_employee",
        target_field="__record__",
        rate=0.012,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="MT-03",
        name="Duplicate employee record",
        description=(
            "The same person appears twice in HR.net under different "
            "employee IDs, typically from a rehire processed as a new "
            "starter."
        ),
        target_table="hrnet_employee",
        target_field="__record__",
        rate=0.010,
        severity="HIGH",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Bank detail defects
    # -----------------------------------------------------------------
    # The highest operational consequence: an error here means someone
    # is not paid.
    DefectType(
        code="BK-01",
        name="Account number with lost leading zeros",
        description=(
            "Account number held as six or seven digits because leading "
            "zeros were stripped when the extract passed through a "
            "spreadsheet. Recoverable by left-padding."
        ),
        target_table="miraclepay_bank",
        target_field="account_number",
        rate=0.045,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="BK-02",
        name="Sort code format variance",
        description=(
            "Sort code stored with hyphens, spaces or full stops rather "
            "than six plain digits. Recoverable by normalisation."
        ),
        target_table="miraclepay_bank",
        target_field="sort_code",
        rate=0.080,
        severity="LOW",
        detectable=True,
    ),
    DefectType(
        code="BK-03",
        name="Invalid sort code",
        description=(
            "Sort code that is not six digits after normalisation. Not "
            "recoverable."
        ),
        target_table="miraclepay_bank",
        target_field="sort_code",
        rate=0.008,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="BK-04",
        name="Multiple active bank accounts",
        description=(
            "More than one bank record flagged active for the same "
            "employee. The migration cannot choose which account to pay."
        ),
        target_table="miraclepay_bank",
        target_field="is_active",
        rate=0.015,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="BK-05",
        name="Account holder name mismatch",
        description=(
            "Account holder differs from the employee name. Often "
            "legitimate, such as a joint account, but requires "
            "confirmation."
        ),
        target_table="miraclepay_bank",
        target_field="account_holder",
        rate=0.022,
        severity="MEDIUM",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Referential integrity
    # -----------------------------------------------------------------
    DefectType(
        code="RF-01",
        name="Orphaned cost centre",
        description=(
            "Cost centre value that does not exist in the finance "
            "reference data, usually a closed or superseded code."
        ),
        target_table="hrnet_employee",
        target_field="cost_centre",
        rate=0.020,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="RF-02",
        name="Orphaned manager reference",
        description=(
            "manager_emp_id points to an employee ID that does not "
            "exist, typically because the manager left and their record "
            "was archived."
        ),
        target_table="hrnet_employee",
        target_field="manager_emp_id",
        rate=0.035,
        severity="MEDIUM",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Format and completeness
    # -----------------------------------------------------------------
    DefectType(
        code="FM-01",
        name="Invalid postcode",
        description=(
            "Postcode that does not match the UK pattern, including "
            "truncated postcodes and free text such as 'N/A'."
        ),
        target_table="hrnet_address",
        target_field="postcode",
        rate=0.028,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="FM-02",
        name="Inconsistent gender coding",
        description=(
            "Gender recorded inconsistently across M, Male, m, F, "
            "Female, f and blank, reflecting multiple data entry "
            "generations."
        ),
        target_table="hrnet_employee",
        target_field="gender",
        rate=0.200,
        severity="LOW",
        detectable=True,
    ),
    DefectType(
        code="FM-03",
        name="Inconsistent FTE format",
        description=(
            "Full-time equivalent stored variously as 1, 1.0, 0.5, "
            "100%, 50%, FULL TIME."
        ),
        target_table="hrnet_employee",
        target_field="fte",
        rate=0.180,
        severity="MEDIUM",
        detectable=True,
    ),
    DefectType(
        code="FM-04",
        name="Missing mandatory address",
        description=(
            "Employee has no address record at all. Blocks issue of "
            "payslips and statutory documents."
        ),
        target_table="hrnet_address",
        target_field="__record__",
        rate=0.014,
        severity="HIGH",
        detectable=True,
    ),
    DefectType(
        code="FM-05",
        name="No primary address flagged",
        description=(
            "Employee has multiple addresses with none flagged primary, "
            "or several flagged primary."
        ),
        target_table="hrnet_address",
        target_field="is_primary",
        rate=0.025,
        severity="MEDIUM",
        detectable=True,
    ),
    DefectType(
        code="FM-06",
        name="Salary stored with currency symbol",
        description=(
            "Annual salary held as text carrying a currency symbol or "
            "thousands separator, for example '£34,500.00'."
        ),
        target_table="miraclepay_employee",
        target_field="annual_salary",
        rate=0.090,
        severity="LOW",
        detectable=True,
    ),
    DefectType(
        code="FM-07",
        name="Whitespace and case inconsistency",
        description=(
            "Leading or trailing whitespace, or inconsistent casing, in "
            "name and address fields."
        ),
        target_table="hrnet_employee",
        target_field="last_name",
        rate=0.070,
        severity="LOW",
        detectable=True,
    ),

    # -----------------------------------------------------------------
    # Payroll control defects
    # -----------------------------------------------------------------
    DefectType(
        code="PR-01",
        name="Employee paid after leaving date",
        description=(
            "Payments recorded in periods after the HR leaver date. A "
            "material control failure rather than a data quality issue."
        ),
        target_table="miraclepay_payment_history",
        target_field="__record__",
        rate=0.010,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="PR-02",
        name="Salary below National Minimum Wage",
        description=(
            "Annualised pay below the National Minimum Wage for the "
            "stated working capacity."
        ),
        target_table="miraclepay_employee",
        target_field="annual_salary",
        rate=0.006,
        severity="CRITICAL",
        detectable=True,
    ),
    DefectType(
        code="PR-03",
        name="Payment history gap",
        description=(
            "One or more missing pay periods in an otherwise continuous "
            "employment. May be unpaid leave or may be a lost extract "
            "row."
        ),
        target_table="miraclepay_payment_history",
        target_field="__record__",
        rate=0.030,
        severity="MEDIUM",
        detectable=True,
    ),
    DefectType(
        code="PR-04",
        name="Unmapped tax code",
        description=(
            "Tax code that does not match any recognised HMRC pattern."
        ),
        target_table="miraclepay_employee",
        target_field="tax_code",
        rate=0.009,
        severity="HIGH",
        detectable=True,
    ),
]


# =====================================================================
# Ground truth log
# =====================================================================

@dataclass
class SeededDefect:
    """A single defect instance, recorded as it is applied."""
    defect_code: str
    source_table: str
    source_key: str
    field_name: str
    value_before: Any
    value_after: Any
    note: str = ""


class GroundTruth:
    """
    Records every defect the generator seeds.

    This is the answer key. Phase 4's profiling engine is scored
    against it, which is what allows the project to state a detection
    rate rather than a count of findings.
    """

    def __init__(self) -> None:
        self._records: list[SeededDefect] = []

    def record(
        self,
        defect_code: str,
        source_table: str,
        source_key: str,
        field_name: str,
        value_before: Any = None,
        value_after: Any = None,
        note: str = "",
    ) -> None:
        self._records.append(
            SeededDefect(
                defect_code=defect_code,
                source_table=source_table,
                source_key=str(source_key),
                field_name=field_name,
                value_before=value_before,
                value_after=value_after,
                note=note,
            )
        )

    def all(self) -> list[SeededDefect]:
        return list(self._records)

    def count(self, defect_code: str | None = None) -> int:
        if defect_code is None:
            return len(self._records)
        return sum(1 for r in self._records if r.defect_code == defect_code)

    def keys_for(self, defect_code: str) -> set[str]:
        """Source keys affected by a given defect type."""
        return {r.source_key for r in self._records if r.defect_code == defect_code}

    def summary(self) -> list[dict]:
        """Per-defect-type counts, for reporting after generation."""
        by_code: dict[str, int] = {}
        for r in self._records:
            by_code[r.defect_code] = by_code.get(r.defect_code, 0) + 1

        rows = []
        for d in DEFECT_CATALOGUE:
            rows.append({
                "code": d.code,
                "name": d.name,
                "table": d.target_table,
                "target_rate": d.rate,
                "seeded_count": by_code.get(d.code, 0),
                "severity": d.severity,
            })
        return rows

    def as_rows(self) -> list[tuple]:
        """Tuples ready for database insert."""
        return [
            (
                r.defect_code,
                r.source_table,
                r.source_key,
                r.field_name,
                None if r.value_before is None else str(r.value_before),
                None if r.value_after is None else str(r.value_after),
                r.note,
            )
            for r in self._records
        ]


# =====================================================================
# Corruption functions
# =====================================================================
# Each takes a clean value and returns a corrupted one. Kept separate
# from the generator so the corruption logic is independently testable.

# NI prefixes HMRC never issues. Using these produces a value that
# looks plausible but is definitively invalid.
INVALID_NI_PREFIXES = ["BG", "GB", "NK", "KN", "TN", "ZZ", "DA", "FB"]

UNPARSEABLE_DATES = [
    "unknown", "UNKNOWN", "n/a", "N/A", "see file", "TBC",
    "00/00/0000", "//", "1900", "not recorded", "-",
]

INVALID_POSTCODES = [
    "N/A", "UNKNOWN", "NE1", "XX99 9XX", "00000", "TBC",
    "NE11AA1", "SEE FILE", "", "ZZ99 9ZZ",
]

GENDER_VARIANTS = {
    "M": ["M", "m", "Male", "MALE", "male", "1"],
    "F": ["F", "f", "Female", "FEMALE", "female", "2"],
}

FTE_VARIANTS = {
    1.0: ["1", "1.0", "1.00", "100%", "100", "FULL TIME", "FT"],
    0.5: ["0.5", "0.50", "50%", "50", "PART TIME"],
    0.6: ["0.6", "0.60", "60%", "60"],
    0.8: ["0.8", "0.80", "80%", "80"],
}

INVALID_TAX_CODES = ["XXXX", "123", "1257", "TAXCODE", "0", "L1257", "NONE"]


def corrupt_ni_malformed(rng: random.Random, clean: str) -> str:
    """Replace the prefix with one HMRC never issues."""
    prefix = rng.choice(INVALID_NI_PREFIXES)
    return prefix + clean[2:]


def corrupt_ni_formatting(rng: random.Random, clean: str) -> str:
    """Valid NI number with inconsistent spacing or case."""
    style = rng.choice(["spaced", "lower", "spaced_lower", "trailing"])
    if style == "spaced":
        return f"{clean[0:2]} {clean[2:4]} {clean[4:6]} {clean[6:8]} {clean[8]}"
    if style == "lower":
        return clean.lower()
    if style == "spaced_lower":
        return f"{clean[0:2]} {clean[2:4]} {clean[4:6]} {clean[6:8]} {clean[8]}".lower()
    return clean + " "


def corrupt_date_unparseable(rng: random.Random, clean: str) -> str:
    return rng.choice(UNPARSEABLE_DATES)


def corrupt_sort_code(rng: random.Random, clean: str) -> str:
    """Introduce separator variance into an otherwise valid sort code."""
    style = rng.choice(["hyphen", "space", "dot"])
    sep = {"hyphen": "-", "space": " ", "dot": "."}[style]
    return f"{clean[0:2]}{sep}{clean[2:4]}{sep}{clean[4:6]}"


def corrupt_sort_code_invalid(rng: random.Random, clean: str) -> str:
    """Produce a sort code that cannot be recovered."""
    style = rng.choice(["short", "long", "alpha"])
    if style == "short":
        return clean[:5]
    if style == "long":
        return clean + str(rng.randint(0, 9))
    return clean[:4] + "XX"


def corrupt_account_leading_zeros(rng: random.Random, clean: str) -> str:
    """
    Simulate leading zeros lost in spreadsheet handling.

    Only applies where the account number genuinely starts with a zero,
    which is how it happens in reality.
    """
    stripped = clean.lstrip("0")
    return stripped if stripped else clean


def corrupt_salary_format(rng: random.Random, clean: float) -> str:
    # "trailing_zeros" was removed: a value like 34500.0000 is still a
    # plain number, so it is not a detectable formatting defect.
    style = rng.choice(["symbol", "comma", "both"])
    if style == "symbol":
        return f"\u00a3{clean:.2f}"
    if style == "comma":
        return f"{clean:,.2f}"
    return f"\u00a3{clean:,.2f}"


def corrupt_whitespace_case(rng: random.Random, clean: str) -> str:
    style = rng.choice(["leading", "trailing", "both", "upper", "lower", "double"])
    if style == "leading":
        return "  " + clean
    if style == "trailing":
        return clean + "   "
    if style == "both":
        return f"  {clean}  "
    if style == "upper":
        return clean.upper()
    if style == "lower":
        return clean.lower()
    return clean.replace(" ", "  ")


def corrupt_postcode(rng: random.Random, clean: str) -> str:
    return rng.choice(INVALID_POSTCODES)


def gender_variant(rng: random.Random, base: str) -> str:
    return rng.choice(GENDER_VARIANTS[base])


def fte_variant(rng: random.Random, base: float) -> str:
    key = min(FTE_VARIANTS.keys(), key=lambda k: abs(k - base))
    return rng.choice(FTE_VARIANTS[key])


def implausible_dob(rng: random.Random) -> date:
    """A date of birth that is clearly wrong."""
    style = rng.choice(["default_1900", "too_young", "too_old", "future"])
    if style == "default_1900":
        return date(1900, 1, 1)
    if style == "too_young":
        return date.today() - timedelta(days=365 * rng.randint(10, 15))
    if style == "too_old":
        return date.today() - timedelta(days=365 * rng.randint(82, 95))
    return date.today() + timedelta(days=rng.randint(1, 400))


def defect_by_code(code: str) -> DefectType | None:
    for d in DEFECT_CATALOGUE:
        if d.code == code:
            return d
    return None


def expected_count(code: str, population: int) -> float:
    """Expected number of instances at the catalogue rate."""
    d = defect_by_code(code)
    return 0.0 if d is None else d.rate * population


# ---------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------

def _validate() -> None:
    codes = [d.code for d in DEFECT_CATALOGUE]
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate defect codes in catalogue")

    bad_rates = [d.code for d in DEFECT_CATALOGUE if not 0 < d.rate < 1]
    if bad_rates:
        raise ValueError(f"Defect rates must be between 0 and 1: {bad_rates}")

    valid_sev = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
    bad_sev = [d.code for d in DEFECT_CATALOGUE if d.severity not in valid_sev]
    if bad_sev:
        raise ValueError(f"Invalid severity on defects: {bad_sev}")


_validate()
