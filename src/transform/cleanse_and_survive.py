"""
Cleansing and survivorship.

Produces the golden record: one resolved view of each employee, built
from two source systems that do not agree with each other.

FOUR STAGES, IN THIS ORDER

  1. Match. Employees are matched between HR.net and MiraclePay using
     the three-level strategy in the mapping specification. Every
     matched person receives a personnel number, and both legacy keys
     are retained so every target record traces back to its sources.

  2. Cleanse. Values are normalised using the named routines in the
     specification: NI numbers, postcodes, sort codes, account numbers,
     FTE and dates.

  3. Survive. Where the two sources hold different values, the
     precedence rules decide which is carried forward. For date of
     birth and leaver status nothing decides: the record goes to manual
     review, because automating those would be faster and occasionally
     wrong, and occasionally wrong is not an acceptable standard for
     fields that drive statutory entitlements.

  4. Audit. Every change is written to sap_target.migration_audit with
     the value before, the value after and the rule applied.

WHY THE AUDIT TRAIL IS NOT OPTIONAL
When a payroll manager asks why an employee's cost centre differs from
the old system, the answer has to be a query rather than a
recollection. A migration nobody can interrogate is a migration nobody
can sign off.

WHAT IS NOT DONE HERE
No date-delimited history is constructed and nothing is written to the
infotypes. That is Phase 6. This phase resolves *what is true about
each person*; the next phase decides *how to express that in SAP*.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

try:
    import pandas as pd
    import psycopg
    import yaml
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing dependency: {e.name}. Run: pip install -r requirements.txt")
    sys.exit(1)

load_dotenv()


STAGING_DDL = """
CREATE SCHEMA IF NOT EXISTS staging;

-- The golden record. One row per person, holding the surviving value
-- for every field, already cleansed.
--
-- This is deliberately flat and denormalised. It is not a target
-- structure; it is the resolved truth about each employee, from which
-- the SAP infotypes are constructed in Phase 6. Keeping the two
-- separate means survivorship decisions can be reviewed without
-- reading infotype construction logic.
CREATE TABLE IF NOT EXISTS staging.golden_employee (
    pernr               char(8)     PRIMARY KEY,
    hrnet_emp_id        text,
    payroll_ref         text,

    ni_number           text,
    title               text,
    first_name          text,
    middle_name         text,
    last_name           text,
    known_as            text,
    date_of_birth       date,
    gender              text,
    marital_status      text,
    nationality         text,

    hire_date           date,
    leaver_date         date,
    leaver_reason       text,
    employment_status   text,

    job_title           text,
    department          text,
    cost_centre         text,
    location            text,
    contract_type       text,
    fte                 numeric(5,4),

    annual_salary       numeric(15,2),
    pay_scale           text,
    tax_code            text,
    ni_category         text,
    payment_method      text,
    pension_scheme      text,

    address_line_1      text,
    address_line_2      text,
    city                text,
    county              text,
    postcode            text,
    country             text,

    bank_name           text,
    sort_code           text,
    account_number      text,
    account_holder      text,

    -- Quality flags carried forward so Phase 6 and the reconciliation
    -- pack can account for records that are incomplete by design.
    has_critical_defect boolean     NOT NULL DEFAULT false,
    review_required     boolean     NOT NULL DEFAULT false,
    review_reason       text,

    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_golden_review
    ON staging.golden_employee (review_required, has_critical_defect);
"""


# =====================================================================
# Connection and transformations
# =====================================================================

def connect() -> psycopg.Connection:
    req = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [k for k in req if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    return psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                "%d %b %Y", "%d %B %Y", "%d%m%Y"]

NI_PATTERN = re.compile(
    r"^(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z][0-9]{6}[A-D]$"
)
POSTCODE_PATTERN = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$")


def parse_uk_date(v):
    """Parse a free-text date. Returns a date or None."""
    if blank(v):
        return None
    s = str(v).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def normalise_ni(v) -> str | None:
    """Upper case, strip separators, then validate against HMRC format."""
    if blank(v):
        return None
    s = re.sub(r"[\s\-.]", "", str(v)).upper()
    return s if NI_PATTERN.match(s) else None


def normalise_postcode(v) -> tuple[str | None, bool]:
    """
    Normalise a UK postcode. Returns (value, was_valid).

    An invalid postcode is returned trimmed rather than nulled, because
    a wrong address is more useful than no address when someone has to
    correct it manually.
    """
    if blank(v):
        return None, False
    s = re.sub(r"\s+", "", str(v)).upper()
    if len(s) >= 5:
        s = s[:-3] + " " + s[-3:]
    return s, bool(POSTCODE_PATTERN.match(s))


def normalise_sort_code(v) -> str | None:
    if blank(v):
        return None
    s = re.sub(r"[\s\-.]", "", str(v))
    return s if re.fullmatch(r"[0-9]{6}", s) else None


def normalise_account_number(v) -> tuple[str | None, bool]:
    """
    Normalise a UK account number. Returns (value, was_padded).

    Six and seven digit numbers have lost leading zeros in extraction
    and are recovered by padding. Shorter values are too damaged to
    recover safely and are rejected.
    """
    if blank(v):
        return None, False
    s = re.sub(r"\D", "", str(v))
    if len(s) == 8:
        return s, False
    if len(s) in (6, 7):
        return s.rjust(8, "0"), True
    return None, False


def normalise_fte(v) -> float | None:
    if blank(v):
        return None
    s = str(v).strip().upper()
    if s in ("FULL TIME", "FT"):
        return 1.0
    if s == "PART TIME":
        return 0.5
    pct = "%" in s
    s = s.replace("%", "").replace(",", "")
    try:
        n = float(s)
    except ValueError:
        return None
    if pct or n > 1.5:
        n = n / 100.0
    return n if 0 < n <= 1.5 else None


def to_money(v) -> float | None:
    if blank(v):
        return None
    s = str(v).replace("\u00a3", "").replace(",", "").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def title_case(v) -> str | None:
    """Capitalise words, preserving Mc, Mac and O' prefixes."""
    if blank(v):
        return None
    s = " ".join(str(v).split())
    out = []
    for w in s.split(" "):
        if len(w) > 2 and w.upper().startswith("MC"):
            out.append("Mc" + w[2:].capitalize())
        elif len(w) > 2 and w.upper().startswith("O'"):
            out.append("O'" + w[2:].capitalize())
        else:
            out.append(w.capitalize())
    return " ".join(out)


# =====================================================================
# Audit and reject recording
# =====================================================================

class Audit:
    """Accumulates audit and reject rows for bulk insert."""

    def __init__(self) -> None:
        self.changes: list[tuple] = []
        self.rejects: list[tuple] = []

    def change(self, pernr, system, key, infotype, field,
               before, after, rule, category) -> None:
        self.changes.append((
            pernr, system, key, infotype, field,
            None if before is None else str(before)[:500],
            None if after is None else str(after)[:500],
            rule, category,
        ))

    def reject(self, system, key, infotype, reason, severity, payload=None) -> None:
        import json
        self.rejects.append((
            system, key, infotype, reason[:500], severity,
            json.dumps(payload, default=str) if payload else None,
        ))

    def flush(self, conn) -> tuple[int, int]:
        with conn.cursor() as cur:
            for c in self.changes:
                cur.execute(
                    """INSERT INTO sap_target.migration_audit
                       (pernr, source_system, source_key, infotype, field_name,
                        value_before, value_after, rule_applied, rule_category)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", c)
            for r in self.rejects:
                cur.execute(
                    """INSERT INTO sap_target.migration_rejects
                       (source_system, source_key, infotype, reject_reason,
                        reject_severity, record_payload)
                       VALUES (%s,%s,%s,%s,%s,%s)""", r)
        return len(self.changes), len(self.rejects)


# =====================================================================
# Matching
# =====================================================================

def match_employees(hr: pd.DataFrame, pay: pd.DataFrame, audit: Audit):
    """
    Match employees between the two systems.

    Returns (matches, hr_only, pay_only) where matches is a list of
    (hr_row, pay_row, method, confidence).

    Duplicate NI numbers are deliberately excluded from exact matching.
    A naive join on a field containing duplicates silently multiplies
    rows, and in a migration that means creating employees in the
    target who do not exist. Those records go to manual review.
    """
    hr = hr.copy()
    pay = pay.copy()
    hr["_ni"] = hr["ni_number"].apply(lambda v: normalise_ni(v) or "")
    pay["_ni"] = pay["ni_number"].apply(lambda v: normalise_ni(v) or "")

    hr_counts = hr[hr["_ni"] != ""]["_ni"].value_counts()
    pay_counts = pay[pay["_ni"] != ""]["_ni"].value_counts()
    ambiguous = set(hr_counts[hr_counts > 1].index) | set(pay_counts[pay_counts > 1].index)

    pay_by_ni = {}
    for _, r in pay.iterrows():
        if r["_ni"] and r["_ni"] not in ambiguous:
            pay_by_ni[r["_ni"]] = r

    matches, hr_unmatched = [], []
    used_pay = set()

    # Level 1: exact on normalised NI
    for _, h in hr.iterrows():
        ni = h["_ni"]
        if ni and ni in pay_by_ni:
            p = pay_by_ni[ni]
            matches.append((h, p, "EXACT_NI", "EXACT"))
            used_pay.add(p["payroll_ref"])
        else:
            hr_unmatched.append(h)

    # Level 2: fuzzy on surname plus date of birth
    remaining_pay = [r for _, r in pay.iterrows()
                     if r["payroll_ref"] not in used_pay]
    pay_by_name_dob = {}
    for p in remaining_pay:
        d = parse_uk_date(p["dob"])
        if d and not blank(p["surname"]):
            pay_by_name_dob.setdefault(
                (str(p["surname"]).strip().upper(), d), []).append(p)

    still_unmatched = []
    for h in hr_unmatched:
        d = parse_uk_date(h["date_of_birth"])
        k = (str(h["last_name"]).strip().upper(), d) if d else None
        cands = pay_by_name_dob.get(k, []) if k else []
        cands = [c for c in cands if c["payroll_ref"] not in used_pay]
        if len(cands) == 1:
            p = cands[0]
            matches.append((h, p, "FUZZY_NAME_DOB", "FUZZY"))
            used_pay.add(p["payroll_ref"])
            audit.reject(
                "HR.net/MiraclePay", str(h["emp_id"]), None,
                f"Matched on surname and date of birth rather than NI number. "
                f"HR NI '{h['ni_number']}', payroll NI '{p['ni_number']}'. "
                f"Requires confirmation before go-live.",
                "MEDIUM",
            )
        else:
            still_unmatched.append(h)

    pay_only = [r for _, r in pay.iterrows() if r["payroll_ref"] not in used_pay]
    return matches, still_unmatched, pay_only, ambiguous


# =====================================================================
# Survivorship
# =====================================================================

def survive(h, p, pernr: str, audit: Audit) -> dict:
    """
    Resolve one employee from two source records.

    Each field is taken from the system that owns it operationally,
    rather than from a general preference for one source. The reasoning
    for each is in the mapping specification and is restated in the
    rule name written to the audit trail.
    """
    rec: dict = {"pernr": pernr,
                 "hrnet_emp_id": None if h is None else h["emp_id"],
                 "payroll_ref": None if p is None else p["payroll_ref"],
                 "review_required": False,
                 "review_reason": None,
                 "has_critical_defect": False}
    reasons: list[str] = []

    def audit_change(field, before, after, rule, cat="SURVIVE"):
        audit.change(pernr, "HR.net/MiraclePay",
                     rec["hrnet_emp_id"] or rec["payroll_ref"],
                     None, field, before, after, rule, cat)

    # ---- Identity: HR is the system of record -----------------------
    if h is not None:
        rec["last_name"] = title_case(h["last_name"])
        rec["first_name"] = title_case(h["first_name"])
        if p is not None and not blank(p["surname"]):
            if str(p["surname"]).strip().upper() != str(h["last_name"]).strip().upper():
                audit_change("last_name", p["surname"], rec["last_name"],
                             "SURV-SURNAME-HR-WINS")
        if rec["last_name"] != h["last_name"]:
            audit_change("last_name", h["last_name"], rec["last_name"],
                         "CLEANSE-TITLE-CASE", "CLEANSE")
        rec["title"] = h["title"]
        rec["middle_name"] = title_case(h["middle_name"])
        rec["known_as"] = title_case(h["known_as"])
        rec["marital_status"] = h["marital_status"]
        rec["nationality"] = h["nationality"]
    else:
        rec["last_name"] = title_case(p["surname"])
        rec["first_name"] = title_case(p["forename"])
        for k in ("title", "middle_name", "known_as", "marital_status", "nationality"):
            rec[k] = None

    # ---- NI number --------------------------------------------------
    raw_ni = (h["ni_number"] if h is not None else p["ni_number"])
    ni = normalise_ni(raw_ni)
    if ni is None and p is not None:
        ni = normalise_ni(p["ni_number"])
    rec["ni_number"] = ni
    if ni is None:
        rec["has_critical_defect"] = True
        reasons.append("No valid National Insurance number")
    elif str(raw_ni) != ni:
        audit_change("ni_number", raw_ni, ni, "CLEANSE-NI-NORMALISE", "CLEANSE")

    # ---- Date of birth: no automatic survivorship -------------------
    # The field drives NI category, pension auto-enrolment and statutory
    # entitlements. Where the sources disagree the migration does not
    # choose.
    dob_h = parse_uk_date(h["date_of_birth"]) if h is not None else None
    dob_p = parse_uk_date(p["dob"]) if p is not None else None
    if dob_h and dob_p and dob_h != dob_p:
        rec["date_of_birth"] = None
        rec["review_required"] = True
        rec["has_critical_defect"] = True
        reasons.append(f"Date of birth conflict: HR {dob_h}, payroll {dob_p}")
        audit_change("date_of_birth", f"{dob_h} / {dob_p}", None,
                     "SURV-DOB-NO-AUTO-RESOLUTION")
    else:
        rec["date_of_birth"] = dob_h or dob_p
        if rec["date_of_birth"] is None:
            rec["has_critical_defect"] = True
            reasons.append("No parseable date of birth")

    # ---- Gender -----------------------------------------------------
    g = None if h is None else str(h["gender"]).strip().upper()
    rec["gender"] = {"M": "M", "MALE": "M", "1": "M",
                     "F": "F", "FEMALE": "F", "2": "F"}.get(g)
    if h is not None and rec["gender"] != h["gender"]:
        audit_change("gender", h["gender"], rec["gender"],
                     "CLEANSE-GENDER-MAP", "CLEANSE")

    # ---- Employment dates: HR wins, unless payroll proves otherwise --
    hire_h = parse_uk_date(h["hire_date"]) if h is not None else None
    hire_p = parse_uk_date(p["start_date"]) if p is not None else None
    rec["hire_date"] = hire_h or hire_p
    if hire_h and hire_p and abs((hire_h - hire_p).days) > 14:
        audit_change("hire_date", hire_p, hire_h, "SURV-HIRE-DATE-HR-WINS")
    if rec["hire_date"] is None:
        rec["has_critical_defect"] = True
        reasons.append("No parseable hire date")

    leave_h = parse_uk_date(h["leaver_date"]) if h is not None else None
    leave_p = parse_uk_date(p["termination_date"]) if p is not None else None
    rec["leaver_date"] = leave_h or leave_p
    if rec["leaver_date"] and rec["hire_date"] and rec["leaver_date"] < rec["hire_date"]:
        rec["review_required"] = True
        reasons.append(f"Leaver date {rec['leaver_date']} precedes hire date "
                       f"{rec['hire_date']}")
        audit_change("leaver_date", rec["leaver_date"], None,
                     "SURV-LEAVER-BEFORE-HIRE-REVIEW")
        rec["leaver_date"] = None

    rec["leaver_reason"] = None if h is None else h["leaver_reason"]
    rec["employment_status"] = None if h is None else h["employment_status"]

    # ---- Organisation: HR for structure, payroll for cost centre ----
    if h is not None:
        rec["job_title"] = h["job_title"]
        rec["department"] = h["department"]
        rec["location"] = h["location"]
        rec["contract_type"] = h["contract_type"]
        rec["fte"] = normalise_fte(h["fte"])
        if rec["fte"] is None:
            rec["fte"] = 1.0
            audit_change("fte", h["fte"], 1.0, "DEFAULT-FTE-100", "DEFAULT")
        elif str(h["fte"]) != str(rec["fte"]):
            audit_change("fte", h["fte"], rec["fte"], "CLEANSE-FTE-NORMALISE", "CLEANSE")
    else:
        for k in ("job_title", "department", "location", "contract_type"):
            rec[k] = None
        rec["fte"] = 1.0

    # Cost centre: payroll wins, because that is where salary cost
    # actually posts to the general ledger.
    cc_h = None if h is None else h["cost_centre"]
    cc_p = None if p is None else p["cost_centre"]
    rec["cost_centre"] = cc_p or cc_h
    if cc_h and cc_p and str(cc_h).strip() != str(cc_p).strip():
        audit_change("cost_centre", cc_h, cc_p, "SURV-COST-CENTRE-PAYROLL-WINS")

    # ---- Pay --------------------------------------------------------
    if p is not None:
        rec["annual_salary"] = to_money(p["annual_salary"])
        if rec["annual_salary"] is not None and str(p["annual_salary"]) != f"{rec['annual_salary']:.2f}":
            audit_change("annual_salary", p["annual_salary"], rec["annual_salary"],
                         "CLEANSE-SALARY-NUMERIC", "CLEANSE")
        rec["pay_scale"] = p["pay_scale"]
        rec["tax_code"] = None if blank(p["tax_code"]) else str(p["tax_code"]).strip().upper()
        rec["ni_category"] = p["ni_category"]
        rec["payment_method"] = p["payment_method"]
        rec["pension_scheme"] = p["pension_scheme"]
    else:
        for k in ("annual_salary", "pay_scale", "tax_code", "ni_category",
                  "payment_method", "pension_scheme"):
            rec[k] = None

    if reasons:
        rec["review_reason"] = "; ".join(reasons)[:1000]
        if rec["has_critical_defect"]:
            rec["review_required"] = True

    return rec


def attach_address(rec: dict, addrs: pd.DataFrame, audit: Audit) -> None:
    """
    Attach the surviving address.

    Where several are flagged primary, or none are, the most complete
    record wins and a defect is raised. Guessing silently would be
    worse than guessing loudly.
    """
    eid = rec.get("hrnet_emp_id")
    for k in ("address_line_1", "address_line_2", "city", "county",
              "postcode", "country"):
        rec[k] = None
    if not eid:
        return

    rows = addrs[addrs["emp_id"] == eid]
    if rows.empty:
        rec["review_required"] = True
        rec["review_reason"] = ((rec.get("review_reason") or "") +
                                "; No address record").strip("; ")[:1000]
        return

    flagged = rows[rows["is_primary"].astype(str).str.strip().str.upper()
                   .isin(["Y", "YES", "1"])]
    if len(flagged) == 1:
        chosen = flagged.iloc[0]
    else:
        chosen = rows.iloc[0]
        audit.change(rec["pernr"], "HR.net", eid, "0006", "address_selection",
                     f"{len(flagged)} flagged primary of {len(rows)}",
                     "first record selected",
                     "SURV-ADDRESS-AMBIGUOUS-PRIMARY", "SURVIVE")

    rec["address_line_1"] = chosen["address_line_1"]
    rec["address_line_2"] = chosen["address_line_2"]
    rec["city"] = title_case(chosen["city"])
    rec["county"] = title_case(chosen["county"])
    rec["country"] = chosen["country"]

    pc, valid = normalise_postcode(chosen["postcode"])
    rec["postcode"] = pc
    if not valid:
        audit.change(rec["pernr"], "HR.net", eid, "0006", "postcode",
                     chosen["postcode"], pc,
                     "CLEANSE-POSTCODE-INVALID-RETAINED", "CLEANSE")
    elif str(chosen["postcode"]) != pc:
        audit.change(rec["pernr"], "HR.net", eid, "0006", "postcode",
                     chosen["postcode"], pc, "CLEANSE-POSTCODE-NORMALISE", "CLEANSE")


def attach_bank(rec: dict, banks: pd.DataFrame, audit: Audit) -> None:
    """
    Attach bank details.

    Where more than one account is active the record is rejected
    outright. The migration cannot choose which account to pay, and a
    wrong choice means paying the wrong person.
    """
    ref = rec.get("payroll_ref")
    for k in ("bank_name", "sort_code", "account_number", "account_holder"):
        rec[k] = None
    if not ref:
        return

    rows = banks[banks["payroll_ref"] == ref]
    active = rows[rows["is_active"].astype(str).str.strip().str.upper()
                  .isin(["Y", "YES", "1"])]

    if len(active) > 1:
        rec["review_required"] = True
        rec["has_critical_defect"] = True
        rec["review_reason"] = ((rec.get("review_reason") or "") +
                                f"; {len(active)} active bank accounts").strip("; ")[:1000]
        audit.reject("MiraclePay", ref, "0009",
                     f"{len(active)} active bank accounts. Cannot determine "
                     f"which account to pay.", "CRITICAL")
        return

    if active.empty:
        if rows.empty:
            return
        chosen = rows.iloc[0]
    else:
        chosen = active.iloc[0]

    rec["bank_name"] = chosen["bank_name"]
    rec["account_holder"] = chosen["account_holder"]

    sc = normalise_sort_code(chosen["sort_code"])
    if sc is None:
        rec["has_critical_defect"] = True
        rec["review_required"] = True
        rec["review_reason"] = ((rec.get("review_reason") or "") +
                                f"; Invalid sort code '{chosen['sort_code']}'"
                                ).strip("; ")[:1000]
        audit.reject("MiraclePay", ref, "0009",
                     f"Sort code '{chosen['sort_code']}' is not six digits "
                     f"after normalisation.", "CRITICAL")
    else:
        rec["sort_code"] = sc
        if str(chosen["sort_code"]) != sc:
            audit.change(rec["pernr"], "MiraclePay", ref, "0009", "sort_code",
                         chosen["sort_code"], sc, "CLEANSE-SORTCODE-NORMALISE",
                         "CLEANSE")

    acct, padded = normalise_account_number(chosen["account_number"])
    if acct is None:
        rec["has_critical_defect"] = True
        rec["review_required"] = True
        rec["review_reason"] = ((rec.get("review_reason") or "") +
                                f"; Unrecoverable account number").strip("; ")[:1000]
        audit.reject("MiraclePay", ref, "0009",
                     f"Account number '{chosen['account_number']}' cannot be "
                     f"recovered to eight digits.", "CRITICAL")
    else:
        rec["account_number"] = acct
        if padded:
            audit.change(rec["pernr"], "MiraclePay", ref, "0009", "account_number",
                         chosen["account_number"], acct,
                         "CLEANSE-ACCOUNT-PAD-LEADING-ZEROS", "CLEANSE")


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cleanse, match and resolve the golden employee record.")
    ap.add_argument("--start-pernr", type=int, default=10000001)
    args = ap.parse_args()

    print("=" * 72)
    print("CLEANSING AND SURVIVORSHIP")
    print("=" * 72)

    conn = connect()
    audit = Audit()

    try:
        with conn.cursor() as cur:
            cur.execute(STAGING_DDL)
            cur.execute("TRUNCATE TABLE staging.golden_employee")
            cur.execute("TRUNCATE TABLE sap_target.key_map CASCADE")
            cur.execute("TRUNCATE TABLE sap_target.migration_audit")
            cur.execute("TRUNCATE TABLE sap_target.migration_rejects")
        conn.commit()

        print("Loading source tables...")
        hr = pd.read_sql("SELECT * FROM legacy.hrnet_employee", conn)
        pay = pd.read_sql("SELECT * FROM legacy.miraclepay_employee", conn)
        addr = pd.read_sql("SELECT * FROM legacy.hrnet_address", conn)
        bank = pd.read_sql("SELECT * FROM legacy.miraclepay_bank", conn)
        for df in (hr, pay, addr, bank):
            df.where(pd.notna(df), None, inplace=True)
        print(f"  HR employees      {len(hr):>7,}")
        print(f"  Payroll employees {len(pay):>7,}")

        print()
        print("Matching...")
        matches, hr_only, pay_only, ambiguous = match_employees(hr, pay, audit)
        exact = sum(1 for m in matches if m[2] == "EXACT_NI")
        fuzzy = sum(1 for m in matches if m[2] == "FUZZY_NAME_DOB")
        print(f"  Exact on NI number        {exact:>7,}")
        print(f"  Fuzzy on surname and DOB  {fuzzy:>7,}")
        print(f"  HR only, unmatched        {len(hr_only):>7,}")
        print(f"  Payroll only, unmatched   {len(pay_only):>7,}")
        print(f"  Ambiguous NI numbers      {len(ambiguous):>7,}"
              f"   (excluded from exact matching)")

        print()
        print("Resolving golden records...")
        pernr = args.start_pernr
        records = []

        for h, p, method, conf in matches:
            rec = survive(h, p, f"{pernr:08d}", audit)
            attach_address(rec, addr, audit)
            attach_bank(rec, bank, audit)
            rec["_match_method"], rec["_confidence"] = method, conf
            records.append(rec)
            pernr += 1

        for h in hr_only:
            rec = survive(h, None, f"{pernr:08d}", audit)
            attach_address(rec, addr, audit)
            rec["review_required"] = True
            rec["review_reason"] = ((rec.get("review_reason") or "") +
                                    "; No payroll record").strip("; ")[:1000]
            rec["_match_method"], rec["_confidence"] = "HR_ONLY", "UNMATCHED"
            audit.reject("HR.net", str(h["emp_id"]), None,
                         "Employee present in HR with no payroll record. May be a "
                         "non-paid worker or a starter not yet set up.", "HIGH")
            records.append(rec)
            pernr += 1

        for p in pay_only:
            rec = survive(None, p, f"{pernr:08d}", audit)
            attach_bank(rec, bank, audit)
            rec["review_required"] = True
            rec["has_critical_defect"] = True
            rec["review_reason"] = ((rec.get("review_reason") or "") +
                                    "; No HR record").strip("; ")[:1000]
            rec["_match_method"], rec["_confidence"] = "PAYROLL_ONLY", "UNMATCHED"
            audit.reject("MiraclePay", str(p["payroll_ref"]), None,
                         "Payroll record with no HR record. Someone is being paid "
                         "with no employment record. Control concern.", "CRITICAL")
            records.append(rec)
            pernr += 1

        print(f"  Golden records built      {len(records):>7,}")

        print()
        print("Writing...")
        cols = [
            "pernr", "hrnet_emp_id", "payroll_ref", "ni_number", "title",
            "first_name", "middle_name", "last_name", "known_as",
            "date_of_birth", "gender", "marital_status", "nationality",
            "hire_date", "leaver_date", "leaver_reason", "employment_status",
            "job_title", "department", "cost_centre", "location",
            "contract_type", "fte", "annual_salary", "pay_scale", "tax_code",
            "ni_category", "payment_method", "pension_scheme",
            "address_line_1", "address_line_2", "city", "county", "postcode",
            "country", "bank_name", "sort_code", "account_number",
            "account_holder", "has_critical_defect", "review_required",
            "review_reason",
        ]
        with conn.cursor() as cur:
            ph = ", ".join(["%s"] * len(cols))
            for rec in records:
                cur.execute(
                    f"INSERT INTO staging.golden_employee ({', '.join(cols)}) "
                    f"VALUES ({ph})",
                    tuple(rec.get(c) for c in cols),
                )
                cur.execute(
                    """INSERT INTO sap_target.key_map
                       (pernr, hrnet_emp_id, payroll_ref, ni_number,
                        match_method, match_confidence)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (rec["pernr"], rec["hrnet_emp_id"], rec["payroll_ref"],
                     rec["ni_number"], rec["_match_method"], rec["_confidence"]),
                )
        n_audit, n_rej = audit.flush(conn)
        conn.commit()

        print(f"  staging.golden_employee   {len(records):>7,} rows")
        print(f"  sap_target.key_map        {len(records):>7,} rows")
        print(f"  migration_audit           {n_audit:>7,} rows")
        print(f"  migration_rejects         {n_rej:>7,} rows")

        # ---------------- report ----------------
        print()
        print("-" * 72)
        print("SURVIVORSHIP DECISIONS BY RULE")
        print("-" * 72)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT rule_category, rule_applied, count(*)
                   FROM sap_target.migration_audit
                   GROUP BY rule_category, rule_applied
                   ORDER BY rule_category, count(*) DESC""")
            cat = None
            for c, rule, n in cur.fetchall():
                if c != cat:
                    print(f"  {c}")
                    cat = c
                print(f"    {rule:<48} {n:>7,}")

        print()
        print("-" * 72)
        print("RECORDS REQUIRING REVIEW")
        print("-" * 72)
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM staging.golden_employee
                           WHERE review_required""")
            rev = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM staging.golden_employee
                           WHERE has_critical_defect""")
            crit = cur.fetchone()[0]
            print(f"  Requiring review          {rev:>7,}  "
                  f"({100*rev/len(records):.1f}% of population)")
            print(f"  With a critical defect    {crit:>7,}  "
                  f"({100*crit/len(records):.1f}% of population)")

            cur.execute(
                """SELECT reject_severity, count(*)
                   FROM sap_target.migration_rejects
                   GROUP BY reject_severity
                   ORDER BY CASE reject_severity
                     WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                     WHEN 'MEDIUM' THEN 3 ELSE 4 END""")
            print()
            print("  Rejects by severity:")
            for sev, n in cur.fetchall():
                print(f"    {sev:<12} {n:>7,}")

    finally:
        conn.close()

    print()
    print("Cleansing and survivorship complete.")


if __name__ == "__main__":
    main()
