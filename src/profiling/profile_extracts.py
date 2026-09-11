"""
Data quality profiling engine.

Reads the rules in config/profiling_rules.yaml, evaluates them against
the landed legacy extracts, scores each table, and measures its own
detection rate against the seeded defect ground truth.

WHY PANDAS RATHER THAN SQL
Profiling at scale is normally done in SQL, pushed down to the
warehouse. Several checks here are genuinely awkward to express that
way: deciding whether a free-text date is parseable in any of seven
formats, judging whether a value is recoverable by normalisation, and
finding gaps in a monthly sequence per employee. Ninety thousand rows
fits comfortably in memory, so the rules are evaluated in pandas where
they can be read and reviewed.

At ten million rows this decision would be wrong, and the rules would
be rewritten as SQL. That is a scale trade-off, not an oversight.

THE SELF-MEASUREMENT
Every rule names the seeded defect code it targets. After evaluation
the engine compares what it found against ground_truth.seeded_defects
and reports, per defect type:

  detected      seeded records the rule correctly flagged
  missed        seeded records the rule did not flag
  extra         records flagged that were not seeded

"Extra" is not necessarily a false positive. Generating a random
surname can coincidentally produce a value that also trips another
rule. The report presents the number rather than judging it.

OUTPUT
Results are written to a `profiling` schema so they persist between
runs, and a readable report is printed. Dress rehearsals can then be
compared: rehearsal two should score better than rehearsal one.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import pandas as pd
    import psycopg
    import yaml
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing dependency: {e.name}. Run: pip install -r requirements.txt")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generator"))
import reference_data  # noqa: E402


load_dotenv()

# UK National Minimum Wage, annualised at 37.5 hours per week.
# Advisory only: apprentice and under-21 rates differ, and historical
# rates vary, so this flags for review rather than rejecting.
NMW_ANNUAL_FULL_TIME = 23795.0


PROFILING_DDL = """
CREATE SCHEMA IF NOT EXISTS profiling;

-- One row per rule per run. The run_id makes dress rehearsals
-- comparable: quality should improve between them, and this table is
-- the evidence of whether it did.
CREATE TABLE IF NOT EXISTS profiling.rule_results (
    run_id          text        NOT NULL,
    rule_id         text        NOT NULL,
    table_name      text        NOT NULL,
    column_name     text,
    rule_type       text        NOT NULL,
    severity        text        NOT NULL,
    description     text,
    records_checked integer     NOT NULL,
    records_failed  integer     NOT NULL,
    pass_rate       numeric(6,3),
    detects_defect  text,
    run_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, rule_id)
);

-- The individual records that failed, so a defect can be traced to a
-- source key rather than remaining a count.
CREATE TABLE IF NOT EXISTS profiling.rule_failures (
    run_id          text        NOT NULL,
    rule_id         text        NOT NULL,
    table_name      text        NOT NULL,
    source_key      text,
    column_name     text,
    failed_value    text,
    detail          text
);

CREATE TABLE IF NOT EXISTS profiling.table_scores (
    run_id          text        NOT NULL,
    table_name      text        NOT NULL,
    rules_evaluated integer     NOT NULL,
    weighted_score  numeric(6,3),
    grade           text,
    run_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, table_name)
);

-- Detection rate per defect type, measured against ground truth.
CREATE TABLE IF NOT EXISTS profiling.detection_scores (
    run_id          text        NOT NULL,
    defect_code     text        NOT NULL,
    rule_id         text,
    seeded          integer     NOT NULL,
    detected        integer     NOT NULL,
    missed          integer     NOT NULL,
    extra           integer     NOT NULL,
    detection_rate  numeric(6,3),
    run_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, defect_code)
);

CREATE INDEX IF NOT EXISTS idx_rule_failures_run
    ON profiling.rule_failures (run_id, rule_id);
"""


# =====================================================================
# Helpers
# =====================================================================

def connect() -> psycopg.Connection:
    required = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    return psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def blank(s) -> bool:
    return s is None or (isinstance(s, str) and s.strip() == "")


def try_parse_date(value: str, formats: list[str]):
    """Attempt to parse a free-text date. Returns a date or None."""
    if blank(value):
        return None
    v = str(value).strip()
    for fmt in formats:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def to_number(value):
    """Parse a number that may carry a currency symbol or separators."""
    if blank(value):
        return None
    v = str(value).strip().replace("\u00a3", "").replace(",", "").replace("%", "")
    try:
        return float(v)
    except ValueError:
        return None


def normalise_fte(value) -> float | None:
    if blank(value):
        return None
    v = str(value).strip().upper()
    if v in ("FULL TIME", "FT"):
        return 1.0
    if v == "PART TIME":
        return 0.5
    n = to_number(v)
    if n is None:
        return None
    return n / 100.0 if "%" in str(value) or n > 1.5 else n


# =====================================================================
# Result container
# =====================================================================

class RuleResult:
    def __init__(self, rule: dict, checked: int, failures: list[tuple]):
        self.rule_id = rule["id"]
        self.table = rule.get("table", "")
        self.column = rule.get("column")
        self.type = rule["type"]
        self.severity = rule["severity"]
        self.description = " ".join(str(rule.get("description", "")).split())
        self.detects_defect = rule.get("detects_defect")
        self.checked = checked
        # (source_key, failed_value, detail)
        self.failures = failures

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def pass_rate(self) -> float:
        if self.checked == 0:
            return 100.0
        return round(100.0 * (self.checked - self.failed) / self.checked, 3)

    @property
    def failed_keys(self) -> set[str]:
        return {str(f[0]) for f in self.failures if f[0] is not None}


# =====================================================================
# Generic rule evaluators
# =====================================================================

def key_column_for(table: str) -> str:
    return {
        "legacy.hrnet_employee": "emp_id",
        "legacy.hrnet_address": "emp_id",
        "legacy.miraclepay_employee": "payroll_ref",
        "legacy.miraclepay_bank": "payroll_ref",
        "legacy.miraclepay_payment_history": "payroll_ref",
    }.get(table, "")


def eval_completeness(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    fails = [
        (r[key] if key else None, r[col], "missing or blank")
        for _, r in df.iterrows() if blank(r[col])
    ]
    return RuleResult(rule, len(df), fails)


def eval_uniqueness(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    work = df
    if rule.get("ignore_blank"):
        work = df[~df[col].apply(blank)]
    counts = work[col].value_counts()
    dupes = set(counts[counts > 1].index)
    fails = [
        (r[key] if key else None, r[col], f"value appears {counts[r[col]]} times")
        for _, r in work.iterrows() if r[col] in dupes
    ]
    return RuleResult(rule, len(work), fails)


def eval_pattern(rule, df, ctx, invert: bool = False) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    pat = re.compile(ctx["patterns"][rule["pattern"]])
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if rule.get("ignore_blank") and blank(v):
            continue
        checked += 1
        t = "" if v is None else str(v)
        if rule.get("strip_spaces_first"):
            t = re.sub(r"[\s\-.]", "", t)
        if rule.get("uppercase_first"):
            t = t.upper()
        ok = bool(pat.match(t))
        if invert:
            ok = not ok
        if not ok:
            fails.append((r[key] if key else None, v, f"fails {rule['pattern']}"))
    return RuleResult(rule, checked, fails)


def eval_domain(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    allowed = rule["allowed"]
    cs = rule.get("case_sensitive", True)
    allowed_cmp = set(allowed) if cs else {a.upper() for a in allowed}
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if rule.get("ignore_blank") and blank(v):
            continue
        checked += 1
        cmp = str(v) if cs else str(v).upper()
        if cmp not in allowed_cmp:
            fails.append((r[key] if key else None, v, "value outside allowed list"))
    return RuleResult(rule, checked, fails)


def eval_date_parseable(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    fmts = ctx["date_formats"]
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            if rule.get("ignore_blank"):
                continue
            checked += 1
            fails.append((r[key] if key else None, v, "missing"))
            continue
        checked += 1
        if try_parse_date(v, fmts) is None:
            fails.append((r[key] if key else None, v, "not parseable as a date"))
    return RuleResult(rule, checked, fails)


def eval_date_range(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    fmts = ctx["date_formats"]
    lo = date.fromisoformat(rule["min_date"])
    hi = date.today() if rule["max_date"] == "TODAY" else date.fromisoformat(rule["max_date"])
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            continue
        d = try_parse_date(v, fmts)
        if d is None:
            continue          # unparseable is a different rule's concern
        checked += 1
        if d < lo or d > hi:
            fails.append((r[key] if key else None, v, f"outside {lo} to {hi}"))
    return RuleResult(rule, checked, fails)


def eval_cross_field(rule, df, ctx) -> RuleResult:
    col, other = rule["column"], rule["other_column"]
    key, fmts = key_column_for(rule["table"]), ctx["date_formats"]
    op = rule["operator"]
    fails, checked = [], 0
    for _, r in df.iterrows():
        a, b = r[col], r[other]
        if rule.get("ignore_blank") and (blank(a) or blank(b)):
            continue
        if rule.get("both_must_parse"):
            da, db = try_parse_date(a, fmts), try_parse_date(b, fmts)
            if da is None or db is None:
                continue
            checked += 1
            ok = (da >= db) if op == "ge" else (da > db) if op == "gt" else (da <= db)
            if not ok:
                fails.append((r[key] if key else None, a, f"{col} {op} {other} violated ({a} vs {b})"))
    return RuleResult(rule, checked, fails)


def eval_referential_set(rule, df, ctx) -> RuleResult:
    col, key = rule["column"], key_column_for(rule["table"])
    fn_name = ctx["reference_sets"][rule["reference_set"]].split(".")[-1]
    valid = getattr(reference_data, fn_name)()
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            continue
        checked += 1
        if str(v).strip() not in valid:
            fails.append((r[key] if key else None, v, "not in reference data"))
    return RuleResult(rule, checked, fails)


def eval_referential_self(rule, df, ctx) -> RuleResult:
    col, keycol = rule["column"], rule["key_column"]
    valid = set(df[keycol].astype(str))
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if rule.get("ignore_blank") and blank(v):
            continue
        checked += 1
        if str(v).strip() not in valid:
            fails.append((r[keycol], v, "reference does not exist in this extract"))
    return RuleResult(rule, checked, fails)


def eval_orphan_child(rule, df, ctx) -> RuleResult:
    """Parents with no child row. Used for employees with no address."""
    parent = ctx["tables"][rule["parent_table"]]
    pcol, ccol = rule["parent_column"], rule["column"]
    have = set(df[ccol].astype(str))
    fails = [
        (r[pcol], r[pcol], "no corresponding child record")
        for _, r in parent.iterrows() if str(r[pcol]) not in have
    ]
    return RuleResult(rule, len(parent), fails)


def eval_aggregate(rule, df, ctx) -> RuleResult:
    gb, wc = rule["group_by"], rule["where_column"]
    wv = {str(x).upper() for x in rule["where_values"]}
    cond = rule["condition"]
    fails = []
    grouped = df.groupby(gb)
    for k, g in grouped:
        n = sum(1 for _, r in g.iterrows() if str(r[wc]).strip().upper() in wv)
        if cond == "exactly_one_where" and n != 1:
            fails.append((k, str(n), f"expected exactly 1 flagged, found {n}"))
        elif cond == "at_most_one_where" and n > 1:
            fails.append((k, str(n), f"expected at most 1 flagged, found {n}"))
    return RuleResult(rule, len(grouped), fails)


# =====================================================================
# Custom rule functions
# =====================================================================

def check_date_ambiguity(rule, ctx) -> RuleResult:
    """
    Dates written in a format where day and month could be transposed,
    and where the day is 12 or lower so the ambiguity cannot be resolved
    from the value itself.
    """
    df = ctx["tables"][rule["table"]]
    col, key = rule["column"], key_column_for(rule["table"])
    slashed = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$")
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            continue
        m = slashed.match(str(v))
        if not m:
            continue
        checked += 1
        d, mth = int(m.group(1)), int(m.group(2))
        if d <= 12 and mth <= 12 and d != mth:
            fails.append((r[key], v, f"could be read as {d}/{mth} or {mth}/{d}"))
    return RuleResult(rule, checked, fails)


def check_ni_formatting(rule, ctx) -> RuleResult:
    """
    NI numbers that are valid in content but stored with inconsistent
    spacing or case. Recoverable, but they defeat an exact join.
    """
    df = ctx["tables"][rule["table"]]
    col, key = rule["column"], key_column_for(rule["table"])
    strict = re.compile(ctx["patterns"]["ni_number_strict"])
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            continue
        checked += 1
        raw = str(v)
        cleaned = re.sub(r"[\s\-.]", "", raw).upper()
        if strict.match(cleaned) and raw != cleaned:
            fails.append((r[key], v, "valid but not normalised"))
    return RuleResult(rule, checked, fails)


def check_national_minimum_wage(rule, ctx) -> RuleResult:
    """
    Annualised pay below the National Minimum Wage at the stated
    capacity. Advisory: apprentice and under-21 rates differ.
    """
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    hr = ctx["tables"]["legacy.hrnet_employee"]

    fte_by_ni = {}
    for _, r in hr.iterrows():
        ni = re.sub(r"[\s\-.]", "", str(r["ni_number"])).upper()
        f = normalise_fte(r["fte"])
        if ni and f:
            fte_by_ni[ni] = f

    fails, checked = [], 0
    for _, r in pay.iterrows():
        salary = to_number(r["annual_salary"])
        if salary is None:
            continue
        checked += 1
        ni = re.sub(r"[\s\-.]", "", str(r["ni_number"])).upper()
        fte = fte_by_ni.get(ni, 1.0)
        threshold = NMW_ANNUAL_FULL_TIME * fte
        if salary < threshold * 0.97:      # 3% tolerance for rate changes
            fails.append((
                r["payroll_ref"], r["annual_salary"],
                f"£{salary:,.0f} below £{threshold:,.0f} at {fte:.2f} FTE",
            ))
    return RuleResult(rule, checked, fails)


def _normalise_ni(v) -> str:
    return re.sub(r"[\s\-.]", "", str(v)).upper() if not blank(v) else ""


def check_match_rate(rule, ctx) -> RuleResult:
    """HR employees with no payroll record on normalised NI number."""
    hr = ctx["tables"]["legacy.hrnet_employee"]
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    pay_ni = {_normalise_ni(v) for v in pay["ni_number"] if not blank(v)}
    fails = []
    for _, r in hr.iterrows():
        ni = _normalise_ni(r["ni_number"])
        if not ni or ni not in pay_ni:
            fails.append((r["emp_id"], r["ni_number"],
                          "no payroll record on normalised NI number"))
    return RuleResult(rule, len(hr), fails)


def check_orphan_payroll(rule, ctx) -> RuleResult:
    """Payroll records with no HR record."""
    hr = ctx["tables"]["legacy.hrnet_employee"]
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    hr_ni = {_normalise_ni(v) for v in hr["ni_number"] if not blank(v)}
    fails = []
    for _, r in pay.iterrows():
        ni = _normalise_ni(r["ni_number"])
        if not ni or ni not in hr_ni:
            fails.append((r["payroll_ref"], r["ni_number"],
                          "no HR record on normalised NI number"))
    return RuleResult(rule, len(pay), fails)


def check_cross_system_conflict(rule, ctx) -> RuleResult:
    """Compare a field between HR and payroll for matched employees."""
    hr = ctx["tables"]["legacy.hrnet_employee"]
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    fmts = ctx["date_formats"]
    hrc, payc = rule["hr_column"], rule["pay_column"]
    tol = rule.get("tolerance_days", 0)

    hr_by_ni = {}
    for _, r in hr.iterrows():
        ni = _normalise_ni(r["ni_number"])
        if ni:
            hr_by_ni.setdefault(ni, r)

    fails, checked = [], 0
    for _, r in pay.iterrows():
        ni = _normalise_ni(r["ni_number"])
        h = hr_by_ni.get(ni)
        if h is None:
            continue
        a, b = h[hrc], r[payc]
        if blank(a) or blank(b):
            continue
        checked += 1

        if rule.get("as_date"):
            da, db = try_parse_date(a, fmts), try_parse_date(b, fmts)
            if da is None or db is None:
                continue
            diff = abs((da - db).days)
            if diff > tol:
                fails.append((r["payroll_ref"], f"{a} / {b}",
                              f"HR {a}, payroll {b}, {diff} days apart"))
        else:
            va, vb = str(a), str(b)
            if rule.get("normalise"):
                va, vb = va.strip().upper(), vb.strip().upper()
            if va != vb:
                fails.append((r["payroll_ref"], f"{a} / {b}",
                              f"HR '{a}', payroll '{b}'"))
    return RuleResult(rule, checked, fails)


def check_account_holder_match(rule, ctx) -> RuleResult:
    """Bank account holder name against the employee's own name."""
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    bank = ctx["tables"]["legacy.miraclepay_bank"]
    names = {
        r["payroll_ref"]: f"{r['forename']} {r['surname']}".strip().upper()
        for _, r in pay.iterrows()
    }
    fails, checked = [], 0
    seen = set()
    for _, r in bank.iterrows():
        ref = r["payroll_ref"]
        if ref in seen:
            continue
        seen.add(ref)
        expected = names.get(ref)
        holder = str(r["account_holder"]).strip().upper()
        if not expected or blank(holder):
            continue
        checked += 1
        if holder != expected:
            fails.append((ref, r["account_holder"],
                          f"holder '{holder}' vs employee '{expected}'"))
    return RuleResult(rule, checked, fails)


def check_paid_after_leaving(rule, ctx) -> RuleResult:
    """Payments recorded in periods after the leaving date."""
    hist = ctx["tables"]["legacy.miraclepay_payment_history"]
    pay = ctx["tables"]["legacy.miraclepay_employee"]
    fmts = ctx["date_formats"]

    term = {}
    for _, r in pay.iterrows():
        d = try_parse_date(r["termination_date"], fmts)
        if d:
            term[r["payroll_ref"]] = d

    fails, checked = [], 0
    flagged = set()
    for _, r in hist.iterrows():
        ref = r["payroll_ref"]
        t = term.get(ref)
        if not t:
            continue
        checked += 1
        ps = try_parse_date(r["period_start"], fmts)
        if ps and ps > t and ref not in flagged:
            flagged.add(ref)
            fails.append((ref, r["pay_period"],
                          f"paid in {r['pay_period']}, left {t}"))
    return RuleResult(rule, checked, fails)


def check_payment_gaps(rule, ctx) -> RuleResult:
    """Missing months within an otherwise continuous payment history."""
    hist = ctx["tables"]["legacy.miraclepay_payment_history"]
    fails = 0
    out = []
    for ref, g in hist.groupby("payroll_ref"):
        periods = sorted(str(p) for p in g["pay_period"] if not blank(p))
        if len(periods) < 3:
            continue
        months = []
        for p in periods:
            try:
                y, m = p.split("-")
                months.append(int(y) * 12 + int(m))
            except ValueError:
                continue
        if len(months) < 3:
            continue
        expected = months[-1] - months[0] + 1
        if len(months) < expected:
            missing = expected - len(months)
            out.append((ref, f"{periods[0]} to {periods[-1]}",
                        f"{missing} missing period(s)"))
    return RuleResult(rule, len(hist.groupby("payroll_ref")), out)



def check_age_plausibility(rule, ctx) -> RuleResult:
    """
    Date of birth must imply a plausible age at hire.

    An absolute date range is not enough. A date of birth in 1935 is
    unremarkable on its own, but implies a ninety year old recruit if
    the hire date is recent. The two fields have to be read together.
    """
    df = ctx["tables"][rule["table"]]
    fmts = ctx["date_formats"]
    lo, hi = rule["min_age_at_hire"], rule["max_age_at_hire"]
    fails, checked = [], 0
    for _, r in df.iterrows():
        dob = try_parse_date(r["date_of_birth"], fmts)
        if dob is None:
            continue
        checked += 1
        if dob > date.today():
            fails.append((r["emp_id"], r["date_of_birth"], "date of birth in the future"))
            continue
        hire = try_parse_date(r["hire_date"], fmts)
        ref = hire or date.today()
        age = (ref - dob).days / 365.25
        if age < lo or age > hi:
            fails.append((r["emp_id"], r["date_of_birth"],
                          f"age {age:.0f} at {'hire' if hire else 'today'}, "
                          f"outside {lo} to {hi}"))
    return RuleResult(rule, checked, fails)


def check_fte_format(rule, ctx) -> RuleResult:
    """
    Full-time equivalent must be a plain decimal in a sensible range.

    A format check alone passes '100' and '50', which are numerically
    clean but mean one hundred and fifty FTE. Range and format have to
    be tested together.
    """
    df = ctx["tables"][rule["table"]]
    fails, checked = [], 0
    numeric = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
    for _, r in df.iterrows():
        v = r["fte"]
        if blank(v):
            continue
        checked += 1
        raw = str(v).strip()
        if not numeric.match(raw):
            fails.append((r["emp_id"], v, "not a plain decimal"))
            continue
        n = float(raw)
        if n <= 0 or n > 1.5:
            fails.append((r["emp_id"], v, f"value {n} outside 0 to 1.5"))
    return RuleResult(rule, checked, fails)


def check_text_hygiene(rule, ctx) -> RuleResult:
    """
    Whitespace and case inconsistency in a free-text field.

    Whitespace is a definitive defect. Case uniformity is weaker
    evidence, since short surnames are legitimately capitalised, so it
    is only flagged where the value is long enough for the pattern to
    mean something.
    """
    df = ctx["tables"][rule["table"]]
    col, key = rule["column"], key_column_for(rule["table"])
    fails, checked = [], 0
    for _, r in df.iterrows():
        v = r[col]
        if blank(v):
            continue
        checked += 1
        raw = str(v)
        if raw != raw.strip():
            fails.append((r[key], repr(v), "leading or trailing whitespace"))
        elif "  " in raw:
            fails.append((r[key], repr(v), "doubled internal whitespace"))
        elif len(raw) > 2 and raw.isalpha() and (raw.isupper() or raw.islower()):
            case = "upper" if raw.isupper() else "lower"
            fails.append((r[key], v, f"wholly {case} case"))
    return RuleResult(rule, checked, fails)


CUSTOM_FUNCTIONS = {
    "check_date_ambiguity": check_date_ambiguity,
    "check_ni_formatting": check_ni_formatting,
    "check_national_minimum_wage": check_national_minimum_wage,
    "check_match_rate": check_match_rate,
    "check_orphan_payroll": check_orphan_payroll,
    "check_cross_system_conflict": check_cross_system_conflict,
    "check_account_holder_match": check_account_holder_match,
    "check_paid_after_leaving": check_paid_after_leaving,
    "check_payment_gaps": check_payment_gaps,
    "check_age_plausibility": check_age_plausibility,
    "check_fte_format": check_fte_format,
    "check_text_hygiene": check_text_hygiene,
}

GENERIC_EVALUATORS = {
    "completeness": eval_completeness,
    "uniqueness": eval_uniqueness,
    "pattern": eval_pattern,
    "pattern_not": lambda r, d, c: eval_pattern(r, d, c, invert=True),
    "domain": eval_domain,
    "date_parseable": eval_date_parseable,
    "date_range": eval_date_range,
    "cross_field": eval_cross_field,
    "referential_set": eval_referential_set,
    "referential_self": eval_referential_self,
    "orphan_child": eval_orphan_child,
    "aggregate": eval_aggregate,
}


# =====================================================================
# Scoring
# =====================================================================

def score_table(results: list[RuleResult], weights: dict) -> float:
    """
    Weighted quality score for a table.

    Weighting by severity means one critical failure moves the score
    more than a hundred cosmetic ones, which is correct: a table with
    an unpayable bank account is not in better shape than one with a
    hundred inconsistent capitalisations.
    """
    num = den = 0.0
    for r in results:
        w = weights.get(r.severity, 1)
        num += w * r.pass_rate
        den += w
    return round(num / den, 3) if den else 100.0


def grade_for(score: float, thresholds: list[dict]) -> tuple[str, str]:
    for t in sorted(thresholds, key=lambda x: -x["min_score"]):
        if score >= t["min_score"]:
            return t["grade"], t["description"]
    return "F", "Not fit for migration"


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Profile the landed legacy extracts.")
    ap.add_argument("--config", default="config/profiling_rules.yaml")
    ap.add_argument("--run-id", default=None,
                    help="Label for this run, for example DR1. Defaults to a timestamp.")
    ap.add_argument("--no-score", action="store_true",
                    help="Skip measurement against ground truth")
    args = ap.parse_args()

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    print("=" * 72)
    print("DATA QUALITY PROFILING")
    print("=" * 72)
    print(f"Run ID  : {run_id}")
    print(f"Rules   : {len(cfg['rules'])} from {args.config}")
    print()

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(PROFILING_DDL)
        conn.commit()

        tables = sorted({r["table"] for r in cfg["rules"]
                         if r.get("table", "").startswith("legacy.")})
        for r in cfg["rules"]:
            if r.get("parent_table"):
                tables.append(r["parent_table"])
        tables = sorted(set(tables))

        print("Loading tables...")
        loaded: dict[str, pd.DataFrame] = {}
        for t in tables:
            df = pd.read_sql(f"SELECT * FROM {t}", conn)
            df = df.astype(object).where(pd.notna(df), None)
            loaded[t] = df
            print(f"  {t:<42} {len(df):>8,} rows")

        ctx = {
            "tables": loaded,
            "patterns": cfg["patterns"],
            "date_formats": cfg["date_formats"],
            "reference_sets": cfg["reference_sets"],
        }

        print()
        print("Evaluating rules...")
        results: list[RuleResult] = []
        for rule in cfg["rules"]:
            if rule["type"] == "custom":
                fn = CUSTOM_FUNCTIONS.get(rule["function"])
                if fn is None:
                    print(f"  {rule['id']}: no function named {rule['function']}, skipped")
                    continue
                res = fn(rule, ctx)
            else:
                ev = GENERIC_EVALUATORS.get(rule["type"])
                if ev is None:
                    print(f"  {rule['id']}: unknown rule type {rule['type']}, skipped")
                    continue
                res = ev(rule, loaded[rule["table"]], ctx)
            results.append(res)

        # ---------------- persist ----------------
        with conn.cursor() as cur:
            cur.execute("DELETE FROM profiling.rule_results WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM profiling.rule_failures WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM profiling.table_scores WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM profiling.detection_scores WHERE run_id = %s", (run_id,))

            for r in results:
                cur.execute(
                    """INSERT INTO profiling.rule_results
                       (run_id, rule_id, table_name, column_name, rule_type,
                        severity, description, records_checked, records_failed,
                        pass_rate, detects_defect)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id, r.rule_id, r.table, r.column, r.type, r.severity,
                     r.description, r.checked, r.failed, r.pass_rate, r.detects_defect),
                )
                for key, val, detail in r.failures[:5000]:
                    cur.execute(
                        """INSERT INTO profiling.rule_failures
                           (run_id, rule_id, table_name, source_key,
                            column_name, failed_value, detail)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (run_id, r.rule_id, r.table,
                         None if key is None else str(key)[:100],
                         r.column,
                         None if val is None else str(val)[:200],
                         detail[:300]),
                    )
        conn.commit()

        # ---------------- report ----------------
        weights = cfg["scoring"]["severity_weights"]
        thresholds = cfg["scoring"]["thresholds"]

        print()
        print("-" * 72)
        print("RULE RESULTS")
        print("-" * 72)
        print(f"  {'Rule':<12}{'Sev':<9}{'Checked':>9}{'Failed':>8}{'Pass':>8}  Description")
        print("  " + "-" * 68)
        for r in sorted(results, key=lambda x: (x.table, x.rule_id)):
            flag = "  " if r.failed == 0 else "! "
            print(f"{flag}{r.rule_id:<12}{r.severity:<9}{r.checked:>9,}"
                  f"{r.failed:>8,}{r.pass_rate:>7.1f}%  {r.description[:60]}")

        print()
        print("-" * 72)
        print("TABLE QUALITY SCORES")
        print("-" * 72)
        by_table: dict[str, list[RuleResult]] = {}
        for r in results:
            by_table.setdefault(r.table, []).append(r)

        with conn.cursor() as cur:
            for t, rs in sorted(by_table.items()):
                s = score_table(rs, weights)
                g, desc = grade_for(s, thresholds)
                print(f"  {t:<42} {s:>7.2f}   {g}   {desc}")
                cur.execute(
                    """INSERT INTO profiling.table_scores
                       (run_id, table_name, rules_evaluated, weighted_score, grade)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (run_id, t, len(rs), s, g),
                )
        conn.commit()

        # ---------------- self-measurement ----------------
        if not args.no_score:
            print()
            print("-" * 72)
            print("DETECTION RATE AGAINST GROUND TRUTH")
            print("-" * 72)
            print("  Measures each rule against the defects the generator seeded.")
            print("  'Extra' means flagged but not seeded, which is not always a")
            print("  false positive: random data can coincidentally trip a rule.")
            print()

            gt = pd.read_sql("SELECT * FROM ground_truth.seeded_defects", conn)
            seeded_keys: dict[str, set[str]] = {}
            for code, g in gt.groupby("defect_code"):
                seeded_keys[code] = {str(k) for k in g["source_key"]}

            by_defect: dict[str, RuleResult] = {}
            for r in results:
                if r.detects_defect:
                    prev = by_defect.get(r.detects_defect)
                    if prev is None or r.failed > prev.failed:
                        by_defect[r.detects_defect] = r

            print(f"  {'Code':<7}{'Rule':<12}{'Seeded':>8}{'Found':>8}"
                  f"{'Hit':>7}{'Miss':>7}{'Extra':>7}{'Rate':>8}")
            print("  " + "-" * 66)

            tot_seed = tot_hit = 0
            with conn.cursor() as cur:
                for code in sorted(seeded_keys):
                    seeded = seeded_keys[code]
                    r = by_defect.get(code)
                    found = r.failed_keys if r else set()
                    hit = len(seeded & found)
                    miss = len(seeded - found)
                    extra = len(found - seeded)
                    rate = round(100.0 * hit / len(seeded), 1) if seeded else 0.0
                    tot_seed += len(seeded)
                    tot_hit += hit
                    mark = " " if rate >= 90 else "!"
                    print(f" {mark}{code:<7}{(r.rule_id if r else '-'):<12}"
                          f"{len(seeded):>8,}{len(found):>8,}{hit:>7,}"
                          f"{miss:>7,}{extra:>7,}{rate:>7.1f}%")
                    cur.execute(
                        """INSERT INTO profiling.detection_scores
                           (run_id, defect_code, rule_id, seeded, detected,
                            missed, extra, detection_rate)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (run_id, code, r.rule_id if r else None,
                         len(seeded), hit, miss, extra, rate),
                    )
            conn.commit()

            overall = round(100.0 * tot_hit / tot_seed, 1) if tot_seed else 0.0
            print("  " + "-" * 66)
            print(f"  {'OVERALL':<19}{tot_seed:>8,}{'':>8}{tot_hit:>7,}"
                  f"{tot_seed - tot_hit:>7,}{'':>7}{overall:>7.1f}%")

    finally:
        conn.close()

    print()
    print(f"Results stored under run_id '{run_id}' in the profiling schema.")


if __name__ == "__main__":
    main()
