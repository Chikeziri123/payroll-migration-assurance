"""
SAP infotype construction.

Builds the six in-scope infotypes from staging.golden_employee and,
for PA0008, from the legacy payment history.

BUILD ORDER, AND WHY IT MATTERS
  PA0002  identity, single record, exercises the loader most simply
  PA0000  actions, which define the employment timeline every other
          infotype must sit inside
  PA0001  organisational assignment
  PA0006  addresses
  PA0009  bank details
  PA0008  basic pay, last, because the hire and leave dates from
          PA0000 anchor the first and last salary segments

THE GAP PROBLEM
The exclusion constraint on the target tables rejects overlapping
validity periods and says nothing about gaps. An employee with PA0008
records covering 2019 to 2022 and 2024 onwards passes the constraint
cleanly and is still broken: SAP expects continuous tiling from hire to
the high date, and a gap means no salary for that period.

A separate assertion therefore runs after construction, comparing the
union of each employee's ranges against their employment range from
PA0000. It runs here rather than in the reconciliation phase, because a
gap means the construction logic dropped a segment and that should
surface at build time.

INFERRING PA0008
No source system records why pay changed. Salary history is inferred
from step changes in basic pay across consecutive months, per rules
PAY-01 to PAY-05 in the mapping specification.

The dominant failure mode is the pro-rated transition month. When a
rise takes effect mid-month, that month is a blend of the old and new
rates, and a naive change detector produces three segments where there
are two, with a middle salary that never existed. Where the signature
is present the effective date is solved for rather than assumed:

  P = A/12 + (d/D) x (B/12 - A/12)
  therefore d = D x (P - A/12) / (B/12 - A/12)

where D is days in the month, A the old annual rate, B the new, P the
blended payment and d the number of days paid at the new rate. The
start date is then day (D - d + 1), not day d. Taking d directly would
place every mid-month rise one or more days early across the whole
population.

SCORING
The construction is measured against ground_truth.salary_history, which
the constructor never reads. The headline metric is day-weighted
absolute error: for every day of every employment, the absolute
difference between inferred and true annual salary, summed and
normalised. It penalises a wrong date and a wrong amount in proportion
to how long the error persists, so a rise detected a week late costs
almost nothing while a phantom band lasting two years costs a great
deal.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import date, timedelta

warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

try:
    import pandas as pd
    import psycopg
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing dependency: {e.name}. Run: pip install -r requirements.txt")
    sys.exit(1)

load_dotenv()

HIGH_DATE = date(9999, 12, 31)

# PAY-01: awards are rarely below 1 per cent, so 0.5 per cent absorbs
# rounding in monthly division without masking a genuine rise.
CHANGE_TOLERANCE = 0.005

# PAY-03: a month whose basic pay lies between these fractions of both
# neighbours is a blended transition month, not a distinct salary.
BLEND_LOWER, BLEND_UPPER = 0.10, 0.90


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


def day_before(d: date) -> date:
    return d - timedelta(days=1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


# =====================================================================
# PA0008 inference
# =====================================================================

@dataclass
class Segment:
    begda: date
    endda: date
    ansal: float
    method: str          # HIRE, STEP, BLEND_SOLVED, BLEND_ASSUMED


def infer_salary_segments(
    periods: list[tuple[date, float]],
    hire: date,
    leave: date | None,
    fte: float,
) -> tuple[list[Segment], list[str]]:
    """
    Infer PA0008 segments from (period_start, basic_pay) pairs.

    Returns (segments, notes). Notes record where a rule was applied in
    a way that a reviewer should know about.
    """
    notes: list[str] = []
    if not periods:
        return [], ["no payment history"]

    # PAY-04: zero and negative periods are unpaid leave, corrections or
    # maternity. They are not salary changes and must not terminate the
    # preceding segment.
    clean = [(d, v) for d, v in sorted(periods) if v and v > 0]
    if not clean:
        return [], ["all payment periods zero or negative"]

    # Annualise. BSGRD is applied separately on the target record, so
    # ANSAL here is the actual paid rate annualised.
    ann = [(d, round(v * 12, 2)) for d, v in clean]

    segments: list[Segment] = []
    i = 0
    n = len(ann)

    # Left censoring: the earliest payment tells us the rate at that
    # point, not when it started. The first segment is therefore dated
    # from the hire date, not from the first observed period.
    cur_start = hire
    cur_val = ann[0][1]
    cur_method = "HIRE"

    while i < n - 1:
        d_this, v_this = ann[i]
        d_next, v_next = ann[i + 1]

        if abs(v_next - cur_val) <= cur_val * CHANGE_TOLERANCE:
            i += 1
            continue

        # PAY-03: is period i+1 a blended transition rather than a new
        # salary? It is if its value sits strictly between the current
        # level and the following one.
        blended = False
        if i + 2 < n:
            v_after = ann[i + 2][1]
            lo, hi = sorted((cur_val, v_after))
            span = hi - lo
            if span > 0 and lo < v_next < hi:
                frac_low = (v_next - lo) / span
                if BLEND_LOWER <= frac_low <= BLEND_UPPER:
                    blended = True

        if blended:
            v_after = ann[i + 2][1]
            D = calendar.monthrange(d_next.year, d_next.month)[1]
            monthly_blend = v_next / 12
            a_m, b_m = cur_val / 12, v_after / 12
            if abs(b_m - a_m) > 1e-9:
                d_days = D * (monthly_blend - a_m) / (b_m - a_m)
                d_days = max(1, min(D, round(d_days)))
                # d_days counts days at the NEW rate, so the change
                # begins on day (D - d_days + 1).
                start_day = D - d_days + 1
                begda = date(d_next.year, d_next.month, start_day)
                method = "BLEND_SOLVED"
            else:
                begda = date(d_next.year, d_next.month, 1)
                method = "BLEND_ASSUMED"
                notes.append("blend detected but rates equal; dated to month start")

            segments.append(Segment(cur_start, day_before(begda), cur_val, cur_method))
            cur_start, cur_val, cur_method = begda, v_after, method
            i += 2
            continue

        # Ordinary step change. Dated to the first of the month in which
        # it appears, since nothing in the data locates it more precisely.
        begda = date(d_next.year, d_next.month, 1)
        if begda <= cur_start:
            begda = cur_start + timedelta(days=1)
        segments.append(Segment(cur_start, day_before(begda), cur_val, cur_method))
        cur_start, cur_val, cur_method = begda, v_next, "STEP"
        i += 1

    # PAY-05: close the final segment at the high date or the day before
    # leaving, so the sequence tiles continuously.
    final_end = day_before(leave) if leave else HIGH_DATE
    if final_end < cur_start:
        # The last detected change begins on or after the leaving date,
        # so it is not a salary period the employee ever served. Drop it
        # and extend the preceding segment to the end of employment.
        # Clamping the end forward, as the previous version did, produced
        # a segment that outlived the employment containing it.
        while segments and segments[-1].begda > final_end:
            segments.pop()
        if segments:
            last = segments[-1]
            segments[-1] = Segment(last.begda, final_end, last.ansal, last.method)
        else:
            segments.append(Segment(cur_start, cur_start, cur_val, cur_method))
    else:
        segments.append(Segment(cur_start, final_end, cur_val, cur_method))

    # Merge any adjacent segments that ended up with equal salary, which
    # can happen after a blend resolution.
    merged: list[Segment] = []
    for s in segments:
        if merged and abs(merged[-1].ansal - s.ansal) < 0.01 \
                and merged[-1].endda == day_before(s.begda):
            merged[-1] = Segment(merged[-1].begda, s.endda, s.ansal, merged[-1].method)
        else:
            merged.append(s)

    return merged, notes


# =====================================================================
# Gap assertion
# =====================================================================

def assert_tiling(
    segments: list[Segment], hire: date, leave: date | None
) -> list[str]:
    """
    Confirm segments tile the employment period continuously.

    The database constraint rejects overlaps and is silent on gaps, so
    this is the check that catches a dropped segment.
    """
    problems: list[str] = []
    if not segments:
        return ["no segments constructed"]

    expected_end = day_before(leave) if leave else HIGH_DATE

    if segments[0].begda != hire:
        problems.append(
            f"first segment starts {segments[0].begda}, employment starts {hire}")

    for a, b in zip(segments, segments[1:]):
        if b.begda != a.endda + timedelta(days=1):
            gap = (b.begda - a.endda).days - 1
            if gap > 0:
                problems.append(f"gap of {gap} day(s) after {a.endda}")
            else:
                problems.append(f"overlap of {-gap} day(s) at {b.begda}")

    if segments[-1].endda != expected_end:
        problems.append(
            f"last segment ends {segments[-1].endda}, expected {expected_end}")

    return problems


# =====================================================================
# Loading with savepoint and replay
# =====================================================================

def load_batch(cur, table: str, cols: list[str], rows: list[tuple],
               pernr: str, infotype: str, rejects: list) -> int:
    """
    Insert one employee's records for one infotype.

    The batch runs inside a savepoint. On an exclusion violation the
    savepoint is rolled back and the rows replayed individually, so the
    clean majority gets bulk speed while failures keep row-level
    attribution. A bare rejection count is far less useful on a
    dashboard than a row naming the conflicting key.
    """
    if not rows:
        return 0

    ph = ", ".join(["%s"] * len(cols))
    stmt = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"

    cur.execute("SAVEPOINT batch_sp")
    try:
        cur.executemany(stmt, rows)
        cur.execute("RELEASE SAVEPOINT batch_sp")
        return len(rows)
    except psycopg.errors.ExclusionViolation:
        cur.execute("ROLLBACK TO SAVEPOINT batch_sp")
    except psycopg.Error:
        cur.execute("ROLLBACK TO SAVEPOINT batch_sp")

    loaded = 0
    for row in rows:
        cur.execute("SAVEPOINT row_sp")
        try:
            cur.execute(stmt, row)
            cur.execute("RELEASE SAVEPOINT row_sp")
            loaded += 1
        except psycopg.Error as e:
            cur.execute("ROLLBACK TO SAVEPOINT row_sp")
            diag = getattr(e, "diag", None)
            rejects.append((
                "SAP_TARGET", pernr, infotype,
                f"{getattr(diag, 'constraint_name', None) or 'constraint'}: "
                f"{getattr(diag, 'message_detail', None) or str(e)[:300]}",
                "CRITICAL",
                json.dumps(dict(zip(cols, [str(v) for v in row]))),
            ))
    return loaded


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Construct SAP infotypes.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N employees, for testing")
    args = ap.parse_args()

    print("=" * 72)
    print("SAP INFOTYPE CONSTRUCTION")
    print("=" * 72)

    conn = connect()
    rejects: list = []

    try:
        with conn.cursor() as cur:
            for t in ("pa0008", "pa0009", "pa0006", "pa0002", "pa0001", "pa0000"):
                cur.execute(f"TRUNCATE TABLE sap_target.{t}")
        conn.commit()

        print("Loading golden records and payment history...")
        g = pd.read_sql("SELECT * FROM staging.golden_employee ORDER BY pernr", conn)
        if args.limit:
            g = g.head(args.limit)
        hist = pd.read_sql(
            """SELECT payroll_ref, period_start, basic_pay
               FROM legacy.miraclepay_payment_history""", conn)
        print(f"  Golden records    {len(g):>8,}")
        print(f"  Payment periods   {len(hist):>8,}")

        hist["_d"] = pd.to_datetime(hist["period_start"], errors="coerce")
        hist["_v"] = pd.to_numeric(hist["basic_pay"], errors="coerce")
        hist = hist.dropna(subset=["_d", "_v"])
                # Columns are renamed without the leading underscore, because
        # itertuples() cannot expose attributes beginning with one and
        # silently substitutes a positional name instead.
        hist = hist.rename(columns={"_d": "pdate", "_v": "pval"})
        by_ref: dict[str, list] = {}
        for ref, grp in hist.groupby("payroll_ref"):
            by_ref[ref] = list(zip(grp["pdate"].dt.date,
                                   grp["pval"].astype(float)))

        counts = dict.fromkeys(
            ["pa0000", "pa0001", "pa0002", "pa0006", "pa0008", "pa0009"], 0)
        skipped = 0
        gap_problems: list[tuple[str, str]] = []
        inferred: dict[str, list[Segment]] = {}
        method_counts: dict[str, int] = {}

        print()
        print("Constructing...")
        with conn.cursor() as cur:
            for row in g.itertuples():
                pernr = row.pernr
                hire = row.hire_date
                if hire is None:
                    skipped += 1
                    # One reject per infotype rather than one for the
                    # person. Every infotype BEGDA derives from the hire
                    # date, so all six genuinely fail, and a reconciliation
                    # that balances per infotype needs to see them that way.
                    for it in ("0000", "0001", "0002", "0006", "0008", "0009"):
                        rejects.append(("SAP_TARGET", pernr, it,
                                        "No parseable hire date; every infotype "
                                        "BEGDA derives from it",
                                        "CRITICAL", None))
                    continue
                leave = row.leaver_date
                emp_end = day_before(leave) if leave else HIGH_DATE
                if emp_end < hire:
                    emp_end = HIGH_DATE

                # ---- PA0002 personal data --------------------------
                if row.date_of_birth and row.last_name and row.first_name:
                    gesch = {"M": "1", "F": "2"}.get(row.gender, "3")
                    counts["pa0002"] += load_batch(
                        cur, "sap_target.pa0002",
                        ["pernr", "begda", "endda", "nachn", "vorna", "midnm",
                         "rufnm", "gbdat", "gesch", "famst", "natio", "perid"],
                        [(pernr, hire, emp_end, row.last_name[:40],
                          row.first_name[:40],
                          (row.middle_name or "")[:40] or None,
                          (row.known_as or "")[:40] or None,
                          row.date_of_birth, gesch, None, None, row.ni_number)],
                        pernr, "0002", rejects)
                else:
                    rejects.append(("SAP_TARGET", pernr, "0002",
                                    "Missing mandatory identity field "
                                    "(date of birth, surname or forename)",
                                    "CRITICAL", None))

                # ---- PA0000 actions --------------------------------
                actions = [(pernr, hire, emp_end, "01", None,
                            "0" if leave else "3")]
                if leave:
                    actions.append((pernr, leave, HIGH_DATE, "10", None, "0"))
                counts["pa0000"] += load_batch(
                    cur, "sap_target.pa0000",
                    ["pernr", "begda", "endda", "massn", "massg", "stat2"],
                    actions, pernr, "0000", rejects)

                # ---- PA0001 organisational assignment --------------
                # Single record spanning employment. Neither source holds
                # organisational history, so historical assignment is not
                # migratable. Open issue OI-01.
                counts["pa0001"] += load_batch(
                    cur, "sap_target.pa0001",
                    ["pernr", "begda", "endda", "bukrs", "werks", "persg",
                     "persk", "kostl", "orgeh", "plans", "stell"],
                    [(pernr, hire, emp_end, "1000", "1000", "1", "01",
                      (row.cost_centre or None), None, "99999999", None)],
                    pernr, "0001", rejects)

                # ---- PA0006 addresses ------------------------------
                if row.address_line_1 and row.city and row.postcode:
                    counts["pa0006"] += load_batch(
                        cur, "sap_target.pa0006",
                        ["pernr", "subty", "begda", "endda", "stras", "locat",
                         "ort01", "ort02", "pstlz", "land1"],
                        [(pernr, "1", hire, emp_end, row.address_line_1[:60],
                          (row.address_line_2 or "")[:40] or None,
                          row.city[:40],
                          (row.county or "")[:40] or None,
                          row.postcode[:10], "GB")],
                        pernr, "0006", rejects)
                else:
                    missing = [f for f, v in (("address line 1", row.address_line_1),
                                              ("city", row.city),
                                              ("postcode", row.postcode))
                               if not v]
                    rejects.append(("SAP_TARGET", pernr, "0006",
                                    f"Address incomplete, PA0006 not created. "
                                    f"Missing: {', '.join(missing)}",
                                    "HIGH", None))

                # ---- PA0009 bank details ---------------------------
                if row.sort_code and row.account_number:
                    zlsch = {"BACS": "U", "Bank Transfer": "U",
                             "Cheque": "C"}.get(row.payment_method, "U")
                    counts["pa0009"] += load_batch(
                        cur, "sap_target.pa0009",
                        ["pernr", "subty", "begda", "endda", "emftx",
                         "bankl", "bankn", "zlsch"],
                        [(pernr, "0", hire, emp_end,
                          (row.account_holder or "")[:40] or None,
                          row.sort_code, row.account_number, zlsch)],
                        pernr, "0009", rejects)
                else:
                    rejects.append(("SAP_TARGET", pernr, "0009",
                                    "No valid bank details; employee cannot be paid",
                                    "CRITICAL", None))

                # ---- PA0008 basic pay ------------------------------
                periods = by_ref.get(row.payroll_ref or "", [])
                fte = float(row.fte) if row.fte is not None else 1.0
                segs, notes = infer_salary_segments(periods, hire, leave, fte)

                if not segs:
                    rejects.append(("SAP_TARGET", pernr, "0008",
                                    f"Cannot derive basic pay: {'; '.join(notes)}",
                                    "CRITICAL", None))
                else:
                    inferred[row.payroll_ref or pernr] = segs
                    for s in segs:
                        method_counts[s.method] = method_counts.get(s.method, 0) + 1

                    problems = assert_tiling(segs, hire, leave)
                    if problems:
                        gap_problems.append((pernr, "; ".join(problems)))
                        rejects.append(("SAP_TARGET", pernr, "0008",
                                        f"Tiling assertion failed: "
                                        f"{'; '.join(problems)[:400]}",
                                        "HIGH", None))

                    bsgrd = round(fte * 100, 2)
                    rows8 = [(pernr, s.begda, s.endda, round(s.ansal, 2),
                              "GBP", bsgrd) for s in segs]
                    counts["pa0008"] += load_batch(
                        cur, "sap_target.pa0008",
                        ["pernr", "begda", "endda", "ansal", "waers", "bsgrd"],
                        rows8, pernr, "0008", rejects)

            for r in rejects:
                cur.execute(
                    """INSERT INTO sap_target.migration_rejects
                       (source_system, source_key, infotype, reject_reason,
                        reject_severity, record_payload)
                       VALUES (%s,%s,%s,%s,%s,%s)""", r)
        conn.commit()

        # ---------------- report ----------------
        print()
        print("-" * 72)
        print("INFOTYPE RECORD COUNTS")
        print("-" * 72)
        for k in ["pa0000", "pa0001", "pa0002", "pa0006", "pa0008", "pa0009"]:
            print(f"  {k.upper():<10} {counts[k]:>10,}")
        print(f"  {'SKIPPED':<10} {skipped:>10,}   (no hire date)")

        print()
        print("-" * 72)
        print("PA0008 SEGMENT DERIVATION METHOD")
        print("-" * 72)
        for m, n in sorted(method_counts.items(), key=lambda x: -x[1]):
            print(f"  {m:<16} {n:>10,}")
        print(f"  {'employees':<16} {len(inferred):>10,}")

        print()
        print("-" * 72)
        print("TILING ASSERTION")
        print("-" * 72)
        print(f"  Employees with a gap or overlap: {len(gap_problems):,}")
        for p, msg in gap_problems[:5]:
            print(f"    {p}  {msg[:70]}")

        print()
        print("-" * 72)
        print("REJECTS")
        print("-" * 72)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT reject_severity, count(*)
                   FROM sap_target.migration_rejects
                   GROUP BY reject_severity ORDER BY 1""")
            for sev, n in cur.fetchall():
                print(f"  {sev:<12} {n:>8,}")

        # ---------------- accuracy ----------------
        print()
        print("-" * 72)
        print("PA0008 ACCURACY AGAINST GROUND TRUTH")
        print("-" * 72)
        # Scoring is restricted to the observable window. Salary changes
        # that occurred before the first payment record cannot be
        # inferred from payment data: there is no evidence of them.
        # Scoring against them measures the unmeasurable and makes a
        # working derivation look broken. The window start is stated in
        # the output so the restriction is visible rather than hidden.
        win_start = hist["pdate"].min().date()

        truth = pd.read_sql(
            """SELECT payroll_ref, effective_from, annual_salary
               FROM ground_truth.salary_history""", conn)
        truth["_d"] = pd.to_datetime(truth["effective_from"], errors="coerce")
        truth["_v"] = pd.to_numeric(truth["annual_salary"], errors="coerce")
        t_by_ref: dict[str, list[tuple[date, float]]] = {}
        truth = truth.rename(columns={"_d": "tdate", "_v": "tval"})
        for ref, grp in truth.dropna(subset=["tdate", "tval"]).groupby("payroll_ref"):
            t_by_ref[ref] = sorted(zip(grp["tdate"].dt.date,
                                       grp["tval"].astype(float)))

        exact_d = w7 = w31 = tot_changes = 0
        exact_a = w1 = wpct = tot_amounts = 0
        seg_over = seg_under = seg_exact = 0
        wsum = 0.0
        wdays = 0
        mean_sal = 0.0

        for ref, segs in inferred.items():
            tv_full = t_by_ref.get(ref)
            if not tv_full:
                continue

            # Restrict truth to the observable window: the rate in force
            # when payment records begin, plus every change after it.
            obs_start = max(segs[0].begda, win_start)
            prior = [v for d, v in tv_full if d <= obs_start]
            tv = ([(obs_start, prior[-1])] if prior else []) + \
                 [(d, v) for d, v in tv_full if d > obs_start]
            if not tv:
                continue
            segs_obs = [s for s in segs if s.endda >= obs_start]
            if not segs_obs:
                continue
            segs, segs_all = segs_obs, segs

            if len(segs) > len(tv):
                seg_over += 1
            elif len(segs) < len(tv):
                seg_under += 1
            else:
                seg_exact += 1

            # Date accuracy on change points, excluding the hire segment
            true_dates = [d for d, _ in tv[1:]]
            inf_dates = [s.begda for s in segs[1:]]
            for td in true_dates:
                tot_changes += 1
                if not inf_dates:
                    continue
                best = min(abs((td - i).days) for i in inf_dates)
                if best == 0:
                    exact_d += 1
                if best <= 7:
                    w7 += 1
                if best <= 31:
                    w31 += 1

            # Amount accuracy
            for td, tvv in tv:
                tot_amounts += 1
                match = [s for s in segs if s.begda <= td <= s.endda]
                if not match:
                    continue
                got = match[0].ansal
                if abs(got - tvv) < 0.01:
                    exact_a += 1
                if abs(got - tvv) <= 1.0:
                    w1 += 1
                if tvv and abs(got - tvv) / tvv <= 0.005:
                    wpct += 1

            # Day-weighted absolute error over the employment period
            start = obs_start
            end = min(segs[-1].endda, date.today())
            if end < start:
                continue
            cur_day = start
            step = 7          # weekly sampling; daily is needlessly slow
            while cur_day <= end:
                inf = next((s.ansal for s in segs_all
                            if s.begda <= cur_day <= s.endda), None)
                tru = None
                for d, v in tv_full:
                    if d <= cur_day:
                        tru = v
                    else:
                        break
                if inf is not None and tru is not None:
                    wsum += abs(inf - tru) * step
                    wdays += step
                    mean_sal += tru * step
                cur_day += timedelta(days=step)

        def pc(a, b):
            return (100.0 * a / b) if b else 0.0

        print(f"  Employees scored              {len(inferred):>8,}")
        print(f"  Observable window begins      {win_start}")
        print( "  Changes before that date have no payment evidence and are")
        print( "  excluded from scoring rather than counted as misses.")
        print()
        print("  Segment count")
        print(f"    exact match                 {seg_exact:>8,}  {pc(seg_exact, len(inferred)):>6.1f}%")
        print(f"    over-segmented              {seg_over:>8,}  {pc(seg_over, len(inferred)):>6.1f}%")
        print(f"    under-segmented             {seg_under:>8,}  {pc(seg_under, len(inferred)):>6.1f}%")
        print()
        print("  Change date accuracy")
        print(f"    exact                       {exact_d:>8,}  {pc(exact_d, tot_changes):>6.1f}%")
        print(f"    within 7 days               {w7:>8,}  {pc(w7, tot_changes):>6.1f}%")
        print(f"    within 31 days              {w31:>8,}  {pc(w31, tot_changes):>6.1f}%")
        print()
        print("  Amount accuracy")
        print(f"    exact                       {exact_a:>8,}  {pc(exact_a, tot_amounts):>6.1f}%")
        print(f"    within GBP 1                {w1:>8,}  {pc(w1, tot_amounts):>6.1f}%")
        print(f"    within 0.5 per cent         {wpct:>8,}  {pc(wpct, tot_amounts):>6.1f}%")
        print()
        if wdays:
            mae = wsum / wdays
            msal = mean_sal / wdays
            print("  HEADLINE: day-weighted absolute error")
            print(f"    mean error per person-day   GBP {mae:>10,.2f}")
            print(f"    mean true salary            GBP {msal:>10,.2f}")
            print(f"    error as share of salary    {100*mae/msal:>10.3f}%")

    finally:
        conn.close()

    print()
    print("Construction complete.")


if __name__ == "__main__":
    main()
