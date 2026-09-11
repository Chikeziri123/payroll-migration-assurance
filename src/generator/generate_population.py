"""
Synthetic legacy population generator.

Produces two legacy source extracts, HR.net and MiraclePay, for a
population of UK employees, with data quality defects seeded at
controlled rates.

DESIGN
The generator works in four stages.

  1. Build a "true" population. Each person has a definitive identity,
     employment record and salary history. This is reality, and no
     source system sees all of it.

  2. Project that truth into HR.net format. HR sees identity,
     organisation and dates, but no pay history.

  3. Project it into MiraclePay format, including a monthly payment
     history derived from the true salary events with realistic noise.

  4. Apply defects. Each source is corrupted independently at the rates
     defined in the defect catalogue, and every corruption is logged to
     ground truth.

WHY SALARY TRUTH IS GENERATED FIRST
Phase 6 must infer salary history from the payment history, because no
source system records why pay changed. Generating the true change
events first means that inference can be scored: we know the correct
answer and can measure how close the derivation gets.

REPRODUCIBILITY
Everything derives from a single seed. The same seed produces a byte
identical population, which is what makes dress rehearsals comparable.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

try:
    from faker import Faker
except ImportError:
    print("Faker is not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

import reference_data as rd
import defect_catalogue as dc


# =====================================================================
# The true population
# =====================================================================

@dataclass
class SalaryEvent:
    """A genuine salary change. The truth Phase 6 must rediscover."""
    effective_from: date
    annual_salary: float
    reason: str          # HIRE, ANNUAL_AWARD, PROMOTION, REGRADE


@dataclass
class TruePerson:
    """
    A person as they actually are, before either system's view of them.

    No source system holds all of this. HR.net sees identity and
    organisation; MiraclePay sees pay and banking. The overlap between
    them is where conflicts arise.
    """
    person_id: int
    title: str
    first_name: str
    middle_name: str
    last_name: str
    known_as: str
    date_of_birth: date
    gender: str                  # "M" or "F" internally
    marital_status: str
    nationality: str
    ni_number: str

    hire_date: date
    leaver_date: date | None
    leaver_reason: str | None
    employment_status: str

    department: rd.Department
    job: rd.Job
    location: rd.Location
    contract_type: str
    fte: float
    pay_scale: str

    salary_events: list[SalaryEvent] = field(default_factory=list)

    address_line_1: str = ""
    address_line_2: str = ""
    city: str = ""
    county: str = ""
    postcode: str = ""

    bank_name: str = ""
    sort_code: str = ""
    account_number: str = ""

    tax_code: str = ""
    ni_category: str = ""
    payment_method: str = ""
    pension_scheme: str = ""

    # Populated during generation
    hrnet_emp_id: str = ""
    payroll_ref: str = ""
    manager_person_id: int | None = None

    # Controls whether this person appears in each source
    in_hrnet: bool = True
    in_payroll: bool = True

    @property
    def current_salary(self) -> float:
        return self.salary_events[-1].annual_salary if self.salary_events else 0.0

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


# =====================================================================
# Identity generation
# =====================================================================

# HMRC never issues these prefixes, so they are excluded from valid
# generation and reserved for seeding invalid values.
_NI_FIRST_LETTERS = "ABCEGHJKLMNOPRSTWXYZ"
_NI_SECOND_LETTERS = "ABCEGHJKLMNPRSTWXYZ"
_NI_INVALID_PREFIXES = {"BG", "GB", "NK", "KN", "TN", "ZZ", "D", "F", "I", "Q", "U", "V"}
_NI_SUFFIXES = "ABCD"


def generate_ni_number(rng: random.Random, used: set[str]) -> str:
    """
    Generate a valid, unique National Insurance number.

    The rules are HMRC's: the first letter may not be D, F, I, Q, U or
    V, the second may not be D, F, I, O, Q, U or V, and the pair may
    not form a reserved prefix.
    """
    for _ in range(500):
        a = rng.choice(_NI_FIRST_LETTERS)
        b = rng.choice(_NI_SECOND_LETTERS)
        prefix = a + b
        if prefix in _NI_INVALID_PREFIXES or a in _NI_INVALID_PREFIXES:
            continue
        digits = f"{rng.randint(0, 999999):06d}"
        suffix = rng.choice(_NI_SUFFIXES)
        ni = f"{prefix}{digits}{suffix}"
        if ni not in used:
            used.add(ni)
            return ni
    raise RuntimeError("Could not generate a unique NI number")


def generate_sort_code(rng: random.Random) -> tuple[str, str]:
    """Return (bank name, six digit sort code) using real UK prefixes."""
    bank = rd.weighted_choice(rng, [(b, w) for b, _, w in rd.BANKS])
    prefix = next(p for n, p, _ in rd.BANKS if n == bank)
    return bank, prefix + f"{rng.randint(0, 9999):04d}"


def generate_account_number(rng: random.Random) -> str:
    """
    Eight digit UK account number.

    Deliberately biased towards numbers starting with zero, because
    those are the ones that lose their leading zeros in spreadsheet
    handling. Without that bias, defect BK-01 would have almost no
    eligible records.
    """
    if rng.random() < 0.18:
        return "0" + f"{rng.randint(0, 9999999):07d}"
    return f"{rng.randint(10000000, 99999999):08d}"


# =====================================================================
# Employment and salary history
# =====================================================================

def generate_hire_date(rng: random.Random, today: date) -> date:
    """
    Hire dates weighted towards recent years.

    A flat distribution over twenty years produces an unrealistic
    number of very long-serving employees and too few recent starters,
    which matters because recent starters are where missing NI numbers
    and incomplete records concentrate.
    """
    band = rd.weighted_choice(rng, [
        ("recent", 34),      # within 3 years
        ("mid", 38),         # 3 to 10 years
        ("long", 21),        # 10 to 20 years
        ("very_long", 7),    # 20 to 35 years
    ])
    days = {
        "recent": rng.randint(30, 365 * 3),
        "mid": rng.randint(365 * 3, 365 * 10),
        "long": rng.randint(365 * 10, 365 * 20),
        "very_long": rng.randint(365 * 20, 365 * 35),
    }[band]
    return today - timedelta(days=days)


def generate_salary_history(
    rng: random.Random,
    job: rd.Job,
    fte: float,
    hire_date: date,
    end_date: date,
) -> list[SalaryEvent]:
    """
    Build a genuine salary history for one employee.

    This is the truth. The payment history is derived from it, and
    Phase 6 must infer it back.

    Pattern: a starting salary at hire, then annual awards each April
    with occasional promotions. Award sizes vary, which is what makes
    the inference non-trivial; a constant 2 per cent every year would
    be trivially detectable.
    """
    events: list[SalaryEvent] = []

    # Starting salary within the job band, skewed towards the lower end
    # since most people are not hired at the top of a band.
    span = job.salary_max - job.salary_min
    start_full_time = job.salary_min + span * (rng.random() ** 1.6)
    start = round(start_full_time * fte, 2)

    events.append(SalaryEvent(hire_date, start, "HIRE"))

    current = start
    year = hire_date.year

    while True:
        year += 1
        award_date = date(year, 4, 1)
        if award_date > end_date:
            break
        if award_date <= hire_date:
            continue

        # Not every employee receives an award every year.
        roll = rng.random()
        if roll < 0.14:
            continue                      # no award this year

        if roll < 0.20:
            # Promotion or regrade: a step change
            uplift = rng.uniform(0.08, 0.22)
            reason = "PROMOTION"
        elif roll < 0.27:
            uplift = rng.uniform(0.045, 0.075)
            reason = "REGRADE"
        else:
            uplift = rng.uniform(0.015, 0.042)
            reason = "ANNUAL_AWARD"

        proposed = current * (1 + uplift)

        # Cap runaway compounding. Thirty-five years of annual awards
        # plus promotions would otherwise take a long-serving employee
        # far above any credible figure for their job. Real pay
        # structures have band ceilings, and beyond the ceiling awards
        # are consolidated or paid as non-consolidated lump sums.
        ceiling = job.salary_max * 1.35 * fte
        if proposed > ceiling:
            if reason == "PROMOTION":
                # A promotion can exceed the band, because the person has
                # moved to a bigger job.
                proposed = min(proposed, ceiling * 1.25)
            else:
                # An ordinary award at the ceiling is reduced to a
                # token uplift.
                proposed = current * (1 + rng.uniform(0.0, 0.012))
                if proposed - current < 1:
                    continue

        current = round(proposed, 2)
        events.append(SalaryEvent(award_date, current, reason))

    return events


def salary_on(events: list[SalaryEvent], when: date) -> float:
    """The salary in force on a given date."""
    applicable = [e for e in events if e.effective_from <= when]
    return applicable[-1].annual_salary if applicable else events[0].annual_salary


# =====================================================================
# Payment history
# =====================================================================

def month_starts(start: date, end: date) -> list[date]:
    """Every month start between two dates inclusive."""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        d = date(y, m, 1)
        if d >= date(start.year, start.month, 1):
            out.append(d)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


@dataclass
class PaymentPeriod:
    period_start: date
    period_end: date
    basic_pay: float
    overtime: float
    bonus: float
    gross_pay: float
    tax: float
    ni: float
    pension: float
    net_pay: float


def generate_payment_history(
    rng: random.Random,
    person: TruePerson,
    window_start: date,
    window_end: date,
) -> list[PaymentPeriod]:
    """
    Produce monthly payment records from the true salary history.

    The noise added here is what makes Phase 6's inference a genuine
    problem rather than a lookup:

      - Overtime and bonus vary month to month without any salary change
      - A salary change mid-month produces a blended basic pay figure
      - Occasional unpaid periods produce zero or reduced pay

    Rule PAY-02 in the mapping specification exists precisely because
    of the first of these.
    """
    periods: list[PaymentPeriod] = []

    first = max(person.hire_date, window_start)
    last = min(person.leaver_date or window_end, window_end)
    if first > last:
        return periods

    overtime_prone = person.job.department in ("Operations", "Field Services", "Engineering")

    for ms in month_starts(first, last):
        # Month end
        if ms.month == 12:
            me = date(ms.year, 12, 31)
        else:
            me = date(ms.year, ms.month + 1, 1) - timedelta(days=1)

        salary_start = salary_on(person.salary_events, ms)
        salary_end = salary_on(person.salary_events, me)

        if salary_start != salary_end:
            # Mid-month change: blend the two rates.
            change = next(
                e.effective_from for e in person.salary_events
                if ms < e.effective_from <= me
            )
            days_in_month = (me - ms).days + 1
            days_old = (change - ms).days
            days_new = days_in_month - days_old
            monthly = (
                (salary_start / 12) * (days_old / days_in_month)
                + (salary_end / 12) * (days_new / days_in_month)
            )
        else:
            monthly = salary_start / 12

        basic = round(monthly, 2)

        overtime = 0.0
        if overtime_prone and rng.random() < 0.42:
            overtime = round(rng.uniform(40, 620), 2)

        bonus = 0.0
        if ms.month == 3 and rng.random() < 0.28:
            bonus = round(person.current_salary * rng.uniform(0.02, 0.09), 2)
        elif rng.random() < 0.02:
            bonus = round(rng.uniform(100, 900), 2)

        gross = round(basic + overtime + bonus, 2)

        # Simplified UK deductions. Not a tax engine, but proportionate
        # enough that the totals look credible.
        taxable = max(0.0, gross - 1047.50)
        tax = round(taxable * 0.20, 2)
        ni_able = max(0.0, gross - 1048.00)
        ni_ded = round(ni_able * 0.08, 2)
        pension = round(basic * 0.05, 2) if person.pension_scheme != "OPTED-OUT" else 0.0
        net = round(gross - tax - ni_ded - pension, 2)

        periods.append(PaymentPeriod(ms, me, basic, overtime, bonus, gross, tax, ni_ded, pension, net))

    return periods


# =====================================================================
# Population builder
# =====================================================================

def build_population(
    rng: random.Random,
    fake: Faker,
    size: int,
    today: date,
    history_years: int,
) -> list[TruePerson]:
    """Create the true population before either system's view of it."""
    people: list[TruePerson] = []
    used_ni: set[str] = set()
    window_start = date(today.year - history_years, today.month, 1)

    for i in range(1, size + 1):
        gender = "M" if rng.random() < 0.62 else "F"   # engineering skew

        if gender == "M":
            first = fake.first_name_male()
            title = rd.weighted_choice(rng, [("Mr", 94), ("Dr", 4), ("Prof", 2)])
        else:
            first = fake.first_name_female()
            title = rd.weighted_choice(rng, [("Mrs", 38), ("Ms", 34), ("Miss", 22), ("Dr", 5), ("Prof", 1)])

        last = fake.last_name()
        middle = fake.first_name() if rng.random() < 0.42 else ""
        known_as = first if rng.random() < 0.88 else fake.first_name()

        hire_date = generate_hire_date(rng, today)

        # Age at hire between 18 and 60, weighted to mid-career.
        age_at_hire = int(rng.triangular(18, 60, 27))
        dob = hire_date - timedelta(days=int(365.25 * age_at_hire) + rng.randint(0, 364))

        status = rd.weighted_choice(rng, rd.EMPLOYMENT_STATUSES)
        leaver_date = None
        leaver_reason = None
        if status == "Leaver":
            min_service = 90
            max_service = max(min_service + 1, (today - hire_date).days)
            leaver_date = hire_date + timedelta(days=rng.randint(min_service, max_service))
            if leaver_date > today:
                leaver_date = today - timedelta(days=rng.randint(1, 200))
            leaver_reason = rd.weighted_choice(rng, rd.LEAVER_REASONS)

        dept = rd.weighted_choice_obj(rng, rd.DEPARTMENTS)
        job = rd.weighted_choice_obj(rng, rd.jobs_for_department(dept.name))
        loc = rd.weighted_choice_obj(rng, rd.LOCATIONS)
        contract = rd.weighted_choice(rng, rd.CONTRACT_TYPES)

        fte = rd.weighted_choice(rng, [(1.0, 82), (0.5, 7), (0.6, 4), (0.8, 7)])

        end_for_salary = leaver_date or today
        salary_events = generate_salary_history(rng, job, fte, hire_date, end_for_salary)

        bank_name, sort_code = generate_sort_code(rng)

        person = TruePerson(
            person_id=i,
            title=title,
            first_name=first,
            middle_name=middle,
            last_name=last,
            known_as=known_as,
            date_of_birth=dob,
            gender=gender,
            marital_status=rd.weighted_choice(rng, [
                ("Single", 38), ("Married", 44), ("Divorced", 11),
                ("Widowed", 3), ("Civil Partnership", 4),
            ]),
            nationality=rd.weighted_choice(rng, [
                ("British", 82), ("Irish", 3), ("Polish", 4), ("Romanian", 2),
                ("Indian", 3), ("Nigerian", 2), ("Portuguese", 1),
                ("Italian", 1), ("Spanish", 1), ("French", 1),
            ]),
            ni_number=generate_ni_number(rng, used_ni),
            hire_date=hire_date,
            leaver_date=leaver_date,
            leaver_reason=leaver_reason,
            employment_status=status,
            department=dept,
            job=job,
            location=loc,
            contract_type=contract,
            fte=fte,
            pay_scale=rd.weighted_choice(rng, rd.PAY_SCALES),
            salary_events=salary_events,
            address_line_1=fake.street_address().replace("\n", ", "),
            address_line_2="",
            city=fake.city(),
            county=fake.county() if hasattr(fake, "county") else "",
            postcode=fake.postcode(),
            bank_name=bank_name,
            sort_code=sort_code,
            account_number=generate_account_number(rng),
            tax_code=rd.weighted_choice(rng, rd.TAX_CODES),
            ni_category=rd.weighted_choice(rng, rd.NI_CATEGORIES),
            payment_method=rd.weighted_choice(rng, rd.PAYMENT_METHODS),
            pension_scheme=rd.weighted_choice(rng, rd.PENSION_SCHEMES),
            hrnet_emp_id=f"E{i:06d}",
            payroll_ref=f"MP{i:06d}",
        )
        people.append(person)

    # Assign managers within department. Senior roles manage; the most
    # senior person in each department has no manager.
    by_dept: dict[str, list[TruePerson]] = {}
    for p in people:
        by_dept.setdefault(p.department.name, []).append(p)

    for dept_name, members in by_dept.items():
        seniors = sorted(members, key=lambda p: p.current_salary, reverse=True)
        managers = seniors[: max(1, len(seniors) // 12)]
        for p in members:
            if p in managers:
                p.manager_person_id = None if p is managers[0] else managers[0].person_id
            else:
                p.manager_person_id = rng.choice(managers).person_id

    return people


# =====================================================================
# Defect application
# =====================================================================

def _select(rng: random.Random, people: list[TruePerson], rate: float) -> list[TruePerson]:
    """Randomly select a proportion of the population."""
    n = max(1, round(len(people) * rate))
    return rng.sample(people, min(n, len(people)))


def apply_record_level_defects(
    rng: random.Random,
    people: list[TruePerson],
    gt: dc.GroundTruth,
) -> list[TruePerson]:
    """
    Defects that add or remove whole records.

    Applied before field-level corruption, because a record that does
    not exist cannot have a corrupted field.
    """
    # MT-01: present in HR only
    for p in _select(rng, people, dc.defect_by_code("MT-01").rate):
        p.in_payroll = False
        gt.record("MT-01", "hrnet_employee", p.hrnet_emp_id, "__record__",
                  note="No corresponding payroll record")

    # MT-02: present in payroll only
    eligible = [p for p in people if p.in_payroll]
    for p in _select(rng, eligible, dc.defect_by_code("MT-02").rate):
        p.in_hrnet = False
        gt.record("MT-02", "miraclepay_employee", p.payroll_ref, "__record__",
                  note="No corresponding HR record")

    # MT-03: duplicate HR record for the same person
    duplicates: list[TruePerson] = []
    eligible = [p for p in people if p.in_hrnet]
    for p in _select(rng, eligible, dc.defect_by_code("MT-03").rate):
        import copy
        dup = copy.deepcopy(p)
        dup.person_id = 900000 + p.person_id
        dup.hrnet_emp_id = f"E9{p.person_id:05d}"
        dup.in_payroll = False
        # A rehire looks like a later hire date
        dup.hire_date = p.hire_date + timedelta(days=rng.randint(400, 2000))
        if dup.hire_date > date.today():
            dup.hire_date = p.hire_date + timedelta(days=200)
        duplicates.append(dup)
        gt.record("MT-03", "hrnet_employee", dup.hrnet_emp_id, "__record__",
                  value_before=p.hrnet_emp_id, value_after=dup.hrnet_emp_id,
                  note=f"Duplicate of {p.hrnet_emp_id}, same NI number")

    return people + duplicates


# =====================================================================
# CSV writers
# =====================================================================


def to_float_or_none(v):
    """Return a float if the string is a plain number, else None."""
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _d(d: date | None) -> str:
    return d.isoformat() if d else ""


def write_hrnet_employee(
    path: Path, rng: random.Random, people: list[TruePerson], gt: dc.GroundTruth
) -> int:
    """Write the HR.net employee extract, applying HR-side defects."""
    rows = []
    active = [p for p in people if p.in_hrnet]

    # Pre-select defect populations so each record is corrupted at most
    # once per defect type.
    d_unparseable = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("DT-01").rate))
    # DT-02 can only be seeded where the day is 12 or lower, since a day
    # above 12 cannot be misread as a month. Eligibility is therefore
    # about 39 per cent of records and the rate is scaled accordingly.
    _amb_eligible = [p for p in active if p.hire_date.day <= 12]
    _amb_scale = len(active) / max(1, len(_amb_eligible))
    d_ambiguous = set(p.hrnet_emp_id for p in _select(
        rng, _amb_eligible, min(0.95, dc.defect_by_code("DT-02").rate * _amb_scale)))
    # Scale subset rates. DT-03 only applies to leavers, so the catalogue
    # rate, which is expressed against the whole population, is scaled up
    # by the inverse of the leaver proportion. Without this the seeded
    # count falls far below the stated rate.
    _leavers = [p for p in active if p.leaver_date]
    _leaver_scale = len(active) / max(1, len(_leavers))
    d_leaver_before = set(p.hrnet_emp_id for p in _select(
        rng, _leavers, min(0.95, dc.defect_by_code("DT-03").rate * _leaver_scale)))
    d_bad_dob = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("DT-04").rate))
    d_orphan_cc = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("RF-01").rate))
    d_orphan_mgr = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("RF-02").rate))
    d_gender = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-02").rate))
    d_fte = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-03").rate))
    d_ws = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-07").rate))

    by_id = {p.person_id: p for p in people}

    for p in active:
        eid = p.hrnet_emp_id

        # Dates
        hire = _d(p.hire_date)
        if eid in d_unparseable:
            bad = dc.corrupt_date_unparseable(rng, hire)
            gt.record("DT-01", "hrnet_employee", eid, "hire_date", hire, bad)
            hire = bad
        elif eid in d_ambiguous and p.hire_date.day <= 12 and p.hire_date.month <= 12:
            amb = p.hire_date.strftime("%d/%m/%Y")
            gt.record("DT-02", "hrnet_employee", eid, "hire_date", hire, amb,
                      note="Day and month could be transposed")
            hire = amb
        elif rng.random() < 0.35:
            hire = p.hire_date.strftime("%d/%m/%Y")   # normal UK format variance

        leaver = _d(p.leaver_date)
        if eid in d_leaver_before and p.leaver_date:
            bad = _d(p.hire_date - timedelta(days=rng.randint(30, 900)))
            gt.record("DT-03", "hrnet_employee", eid, "leaver_date", leaver, bad)
            leaver = bad

        dob = _d(p.date_of_birth)
        if eid in d_bad_dob:
            bad = _d(dc.implausible_dob(rng))
            gt.record("DT-04", "hrnet_employee", eid, "date_of_birth", dob, bad)
            dob = bad

        # Cost centre
        cc = p.department.cost_centre
        if eid in d_orphan_cc:
            bad = f"CC9{rng.randint(100, 999)}0000"
            gt.record("RF-01", "hrnet_employee", eid, "cost_centre", cc, bad,
                      note="Cost centre not present in finance reference data")
            cc = bad

        # Manager
        mgr = ""
        if p.manager_person_id and p.manager_person_id in by_id:
            mgr = by_id[p.manager_person_id].hrnet_emp_id
        if eid in d_orphan_mgr:
            bad = f"E{rng.randint(800000, 899999)}"
            gt.record("RF-02", "hrnet_employee", eid, "manager_emp_id", mgr, bad,
                      note="Manager ID does not exist in the extract")
            mgr = bad

        # Gender coding
        gender = p.gender
        if eid in d_gender:
            var = dc.gender_variant(rng, p.gender)
            # Only a genuine change counts. A "variant" identical to the
            # clean value is not a defect, and recording it as one would
            # make any detection rate measured against this log wrong.
            if var != p.gender:
                gt.record("FM-02", "hrnet_employee", eid, "gender", p.gender, var)
                gender = var

        # FTE format
        fte = str(p.fte)
        if eid in d_fte:
            var = dc.fte_variant(rng, p.fte)
            if var != fte:
                gt.record("FM-03", "hrnet_employee", eid, "fte", fte, var)
                fte = var

        # Whitespace and case
        last_name = p.last_name
        if eid in d_ws:
            var = dc.corrupt_whitespace_case(rng, p.last_name)
            if var != p.last_name:
                gt.record("FM-07", "hrnet_employee", eid, "last_name", p.last_name, var)
                last_name = var

        rows.append({
            "emp_id": eid,
            "ni_number": p.ni_number,
            "title": p.title,
            "first_name": p.first_name,
            "middle_name": p.middle_name,
            "last_name": last_name,
            "known_as": p.known_as,
            "date_of_birth": dob,
            "gender": gender,
            "marital_status": p.marital_status,
            "nationality": p.nationality,
            "hire_date": hire,
            "leaver_date": leaver,
            "leaver_reason": p.leaver_reason or "",
            "employment_status": p.employment_status,
            "job_title": p.job.title,
            "department": p.department.name,
            "cost_centre": cc,
            "location": p.location.name,
            "manager_emp_id": mgr,
            "contract_type": p.contract_type,
            "fte": fte,
        })

    _write_csv(path, rows)
    return len(rows)


def write_hrnet_address(
    path: Path, rng: random.Random, people: list[TruePerson], gt: dc.GroundTruth
) -> int:
    rows = []
    active = [p for p in people if p.in_hrnet]

    d_missing = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-04").rate))
    d_postcode = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-01").rate))
    d_primary = set(p.hrnet_emp_id for p in _select(rng, active, dc.defect_by_code("FM-05").rate))

    # One Faker instance, reused. Constructing one per row is wasteful and
    # obscures where randomness is drawn from.
    fake = Faker("en_GB")

    for p in active:
        eid = p.hrnet_emp_id

        if eid in d_missing:
            gt.record("FM-04", "hrnet_address", eid, "__record__",
                      note="Employee has no address record")
            continue

        postcode = p.postcode
        if eid in d_postcode:
            bad = dc.corrupt_postcode(rng, p.postcode)
            gt.record("FM-01", "hrnet_address", eid, "postcode", p.postcode, bad)
            postcode = bad

        primary_flag = rng.choice(["Y", "YES", "1", "Y"])

        if eid in d_primary:
            style = rng.choice(["none", "both"])
            if style == "none":
                primary_flag = "N"
                gt.record("FM-05", "hrnet_address", eid, "is_primary", "Y", "N",
                          note="No address flagged primary")
            else:
                gt.record("FM-05", "hrnet_address", eid, "is_primary", "Y", "Y",
                          note="Multiple addresses flagged primary")

        rows.append({
            "emp_id": eid,
            "address_type": "HOME",
            "address_line_1": p.address_line_1,
            "address_line_2": p.address_line_2,
            "city": p.city,
            "county": p.county,
            "postcode": postcode,
            "country": "United Kingdom",
            "is_primary": primary_flag,
        })

        # Second address for some, which is where the primary flag
        # ambiguity becomes a real problem.
        if eid in d_primary or rng.random() < 0.11:
            second_primary = "Y" if (eid in d_primary
                                     and primary_flag.strip().upper() in ("Y", "YES", "1")) else "N"
            rows.append({
                "emp_id": eid,
                "address_type": rng.choice(["POSTAL", "TEMPORARY"]),
                "address_line_1": fake.street_address().replace("\n", ", "),
                "address_line_2": "",
                "city": p.city,
                "county": p.county,
                "postcode": p.postcode,
                "country": "United Kingdom",
                "is_primary": second_primary,
            })

    _write_csv(path, rows)
    return len(rows)


def write_miraclepay_employee(
    path: Path, rng: random.Random, people: list[TruePerson], gt: dc.GroundTruth
) -> int:
    rows = []
    active = [p for p in people if p.in_payroll]

    d_ni_bad = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("NI-01").rate))
    d_ni_dup = _select(rng, active, dc.defect_by_code("NI-02").rate)
    d_ni_missing = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("NI-03").rate))
    d_ni_fmt = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("NI-04").rate))
    d_cc_conflict = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("XS-01").rate))
    d_surname = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("XS-02").rate))
    d_dob = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("XS-03").rate))
    d_startdate = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("XS-04").rate))
    d_salary_fmt = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("FM-06").rate))
    d_nmw = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("PR-02").rate))
    d_taxcode = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("PR-04").rate))

    # Duplicate NI numbers: copy one employee's NI onto another
    dup_map: dict[str, str] = {}
    for p in d_ni_dup:
        donor = rng.choice([x for x in active if x.payroll_ref != p.payroll_ref])
        dup_map[p.payroll_ref] = donor.ni_number
        gt.record("NI-02", "miraclepay_employee", p.payroll_ref, "ni_number",
                  p.ni_number, donor.ni_number,
                  note=f"Duplicates NI of {donor.payroll_ref}")

    fake = Faker("en_GB")

    for p in active:
        ref = p.payroll_ref

        ni = p.ni_number
        if ref in dup_map:
            ni = dup_map[ref]
        elif ref in d_ni_missing:
            gt.record("NI-03", "miraclepay_employee", ref, "ni_number", ni, "")
            ni = ""
        elif ref in d_ni_bad:
            bad = dc.corrupt_ni_malformed(rng, ni)
            gt.record("NI-01", "miraclepay_employee", ref, "ni_number", ni, bad)
            ni = bad
        elif ref in d_ni_fmt:
            var = dc.corrupt_ni_formatting(rng, ni)
            gt.record("NI-04", "miraclepay_employee", ref, "ni_number", ni, var)
            ni = var

        surname = p.last_name
        if ref in d_surname:
            alt = fake.last_name()
            gt.record("XS-02", "miraclepay_employee", ref, "surname", p.last_name, alt,
                      note="Payroll surname differs from HR")
            surname = alt

        dob = _d(p.date_of_birth)
        if ref in d_dob:
            shifted = p.date_of_birth + timedelta(days=rng.choice([-365, -31, -1, 1, 31, 365]))
            gt.record("XS-03", "miraclepay_employee", ref, "dob", dob, _d(shifted),
                      note="Payroll date of birth differs from HR")
            dob = _d(shifted)

        cc = p.department.cost_centre
        if ref in d_cc_conflict:
            other = rng.choice([d for d in rd.DEPARTMENTS if d.cost_centre != cc])
            gt.record("XS-01", "miraclepay_employee", ref, "cost_centre", cc, other.cost_centre,
                      note="Payroll cost centre differs from HR")
            cc = other.cost_centre

        start = _d(p.hire_date)
        if ref in d_startdate:
            shifted = p.hire_date + timedelta(days=rng.randint(20, 120))
            gt.record("XS-04", "miraclepay_employee", ref, "start_date", start, _d(shifted),
                      note="Payroll start date differs materially from HR hire date")
            start = _d(shifted)
        elif rng.random() < 0.22:
            start = _d(p.hire_date + timedelta(days=rng.randint(0, 14)))

        salary_val = p.current_salary
        if ref in d_nmw:
            salary_val = round(rng.uniform(8000, 15500) * p.fte, 2)
            gt.record("PR-02", "miraclepay_employee", ref, "annual_salary",
                      p.current_salary, salary_val,
                      note="Below National Minimum Wage at stated FTE")

        salary_str = f"{salary_val:.2f}"
        if ref in d_salary_fmt:
            var = dc.corrupt_salary_format(rng, salary_val)
            # Trailing-zero variants are still numerically clean, so they
            # are not a detectable defect and are not recorded as one.
            if var != salary_str and to_float_or_none(var) is None:
                gt.record("FM-06", "miraclepay_employee", ref, "annual_salary", salary_str, var)
                salary_str = var

        tax_code = p.tax_code
        if ref in d_taxcode:
            bad = rng.choice(dc.INVALID_TAX_CODES)
            gt.record("PR-04", "miraclepay_employee", ref, "tax_code", tax_code, bad)
            tax_code = bad

        rows.append({
            "payroll_ref": ref,
            "ni_number": ni,
            "surname": surname,
            "forename": p.first_name,
            "dob": dob,
            "tax_code": tax_code,
            "ni_category": p.ni_category,
            "annual_salary": salary_str,
            "pay_frequency": "MONTHLY",
            "pay_scale": p.pay_scale,
            "cost_centre": cc,
            "start_date": start,
            "termination_date": _d(p.leaver_date),
            "payment_method": p.payment_method,
            "pension_scheme": p.pension_scheme,
        })

    _write_csv(path, rows)
    return len(rows)


def write_miraclepay_bank(
    path: Path, rng: random.Random, people: list[TruePerson], gt: dc.GroundTruth
) -> int:
    rows = []
    active = [p for p in people if p.in_payroll]

    zero_eligible = [p for p in active if p.account_number.startswith("0")]
    d_zeros = set(p.payroll_ref for p in _select(rng, zero_eligible, min(0.9, dc.defect_by_code("BK-01").rate / 0.18)))
    d_sc_fmt = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("BK-02").rate))
    d_sc_bad = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("BK-03").rate))
    d_multi = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("BK-04").rate))
    d_holder = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("BK-05").rate))

    fake = Faker("en_GB")

    for p in active:
        ref = p.payroll_ref

        acct = p.account_number
        if ref in d_zeros:
            bad = dc.corrupt_account_leading_zeros(rng, acct)
            if bad != acct:
                gt.record("BK-01", "miraclepay_bank", ref, "account_number", acct, bad,
                          note="Leading zeros lost in extract")
                acct = bad

        sort_code = p.sort_code
        if ref in d_sc_bad:
            bad = dc.corrupt_sort_code_invalid(rng, sort_code)
            gt.record("BK-03", "miraclepay_bank", ref, "sort_code", sort_code, bad)
            sort_code = bad
        elif ref in d_sc_fmt:
            var = dc.corrupt_sort_code(rng, sort_code)
            gt.record("BK-02", "miraclepay_bank", ref, "sort_code", sort_code, var)
            sort_code = var

        holder = f"{p.first_name} {p.last_name}".upper()
        if ref in d_holder:
            alt = f"{fake.first_name()} {p.last_name}".upper()
            gt.record("BK-05", "miraclepay_bank", ref, "account_holder", holder, alt,
                      note="Account holder differs from employee name")
            holder = alt

        rows.append({
            "payroll_ref": ref,
            "bank_name": p.bank_name,
            "sort_code": sort_code,
            "account_number": acct,
            "account_holder": holder,
            "building_society_ref": "" if rng.random() > 0.03 else f"ROLL{rng.randint(10000, 99999)}",
            "is_active": "Y",
        })

        if ref in d_multi:
            bank2, sc2 = generate_sort_code(rng)
            gt.record("BK-04", "miraclepay_bank", ref, "is_active", "1 active", "2 active",
                      note="Multiple active bank accounts")
            rows.append({
                "payroll_ref": ref,
                "bank_name": bank2,
                "sort_code": sc2,
                "account_number": generate_account_number(rng),
                "account_holder": holder,
                "building_society_ref": "",
                "is_active": "Y",
            })

    _write_csv(path, rows)
    return len(rows)


def write_payment_history(
    path: Path,
    rng: random.Random,
    people: list[TruePerson],
    gt: dc.GroundTruth,
    window_start: date,
    window_end: date,
) -> int:
    rows = []
    active = [p for p in people if p.in_payroll]

    d_gap = set(p.payroll_ref for p in _select(rng, active, dc.defect_by_code("PR-03").rate))
    leavers = [p for p in active if p.leaver_date]
    # PR-01 applies only to leavers, so the population rate is scaled by
    # the inverse of the leaver proportion.
    _scale = len(active) / max(1, len(leavers))
    d_paid_after = set(
        p.payroll_ref for p in _select(
            rng, leavers, min(0.95, dc.defect_by_code("PR-01").rate * _scale))
    ) if leavers else set()

    for p in active:
        periods = generate_payment_history(rng, p, window_start, window_end)

        if p.payroll_ref in d_gap and len(periods) > 6:
            drop_at = rng.randint(2, len(periods) - 3)
            dropped = periods[drop_at]
            gt.record("PR-03", "miraclepay_payment_history", p.payroll_ref, "__record__",
                      value_before=str(dropped.period_start), value_after="",
                      note="Missing pay period in continuous employment")
            periods = periods[:drop_at] + periods[drop_at + 1:]

        if p.payroll_ref in d_paid_after and p.leaver_date and periods:
            extra_start = date(p.leaver_date.year, p.leaver_date.month, 1)
            for k in range(1, rng.randint(2, 4)):
                m = extra_start.month + k
                y = extra_start.year + (m - 1) // 12
                m = ((m - 1) % 12) + 1
                ms = date(y, m, 1)
                if ms > window_end:
                    break
                me = (date(y, m + 1, 1) - timedelta(days=1)) if m < 12 else date(y, 12, 31)
                basic = round(p.current_salary / 12, 2)
                periods.append(PaymentPeriod(ms, me, basic, 0.0, 0.0, basic,
                                             round(basic * 0.18, 2), round(basic * 0.07, 2),
                                             0.0, round(basic * 0.75, 2)))
            gt.record("PR-01", "miraclepay_payment_history", p.payroll_ref, "__record__",
                      value_before=_d(p.leaver_date), value_after="",
                      note="Payments recorded after leaving date")

        for per in periods:
            rows.append({
                "payroll_ref": p.payroll_ref,
                "pay_period": per.period_start.strftime("%Y-%m"),
                "period_start": _d(per.period_start),
                "period_end": _d(per.period_end),
                "gross_pay": f"{per.gross_pay:.2f}",
                "basic_pay": f"{per.basic_pay:.2f}",
                "overtime": f"{per.overtime:.2f}",
                "bonus": f"{per.bonus:.2f}",
                "tax_deducted": f"{per.tax:.2f}",
                "ni_deducted": f"{per.ni:.2f}",
                "pension_deducted": f"{per.pension:.2f}",
                "net_pay": f"{per.net_pay:.2f}",
            })

    _write_csv(path, rows)
    return len(rows)


def write_salary_truth(path: Path, people: list[TruePerson]) -> int:
    """
    The true salary history.

    Not a source extract. This is the answer key Phase 6's derivation
    is scored against, and it must never be read by the transformation
    code.
    """
    rows = []
    for p in people:
        if not p.in_payroll:
            continue
        for i, e in enumerate(p.salary_events):
            rows.append({
                "payroll_ref": p.payroll_ref,
                "sequence": i + 1,
                "effective_from": _d(e.effective_from),
                "annual_salary": f"{e.annual_salary:.2f}",
                "change_reason": e.reason,
            })
    _write_csv(path, rows)
    return len(rows)


def write_ground_truth(path: Path, gt: dc.GroundTruth) -> int:
    rows = [
        {
            "defect_code": r.defect_code,
            "source_table": r.source_table,
            "source_key": r.source_key,
            "field_name": r.field_name,
            "value_before": r.value_before,
            "value_after": r.value_after,
            "note": r.note,
        }
        for r in gt.all()
    ]
    _write_csv(path, rows)
    return len(rows)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# =====================================================================
# Entry point
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate synthetic HR and payroll legacy extracts."
    )
    ap.add_argument("--size", type=int, default=2300,
                    help="Number of employees (default 2300)")
    ap.add_argument("--seed", type=int, default=20260906,
                    help="Random seed. The same seed reproduces the same population.")
    ap.add_argument("--history-years", type=int, default=4,
                    help="Years of payment history to generate (default 4)")
    ap.add_argument("--output", type=str, default="output/extracts",
                    help="Output directory")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    Faker.seed(args.seed)
    fake = Faker("en_GB")

    today = date.today()
    window_start = date(today.year - args.history_years, today.month, 1)

    out = Path(args.output)

    print("=" * 68)
    print("SYNTHETIC LEGACY POPULATION GENERATOR")
    print("=" * 68)
    print(f"Population        : {args.size:,} employees")
    print(f"Seed              : {args.seed}")
    print(f"Payment history   : {window_start} to {today}")
    print(f"Output directory  : {out}")
    print()

    print("Building true population...")
    people = build_population(rng, fake, args.size, today, args.history_years)

    gt = dc.GroundTruth()

    print("Applying record level defects...")
    people = apply_record_level_defects(rng, people, gt)

    print("Writing HR.net extracts...")
    n_hr = write_hrnet_employee(out / "hrnet_employee.csv", rng, people, gt)
    n_addr = write_hrnet_address(out / "hrnet_address.csv", rng, people, gt)

    print("Writing MiraclePay extracts...")
    n_pay = write_miraclepay_employee(out / "miraclepay_employee.csv", rng, people, gt)
    n_bank = write_miraclepay_bank(out / "miraclepay_bank.csv", rng, people, gt)
    n_hist = write_payment_history(out / "miraclepay_payment_history.csv",
                                   rng, people, gt, window_start, today)

    print("Writing answer keys...")
    n_truth = write_salary_truth(out / "_truth_salary_history.csv", people)
    n_gt = write_ground_truth(out / "_truth_seeded_defects.csv", gt)

    print()
    print("-" * 68)
    print("EXTRACT ROW COUNTS")
    print("-" * 68)
    print(f"  hrnet_employee                {n_hr:>8,}")
    print(f"  hrnet_address                 {n_addr:>8,}")
    print(f"  miraclepay_employee           {n_pay:>8,}")
    print(f"  miraclepay_bank               {n_bank:>8,}")
    print(f"  miraclepay_payment_history    {n_hist:>8,}")
    print()
    print(f"  _truth_salary_history         {n_truth:>8,}   (answer key, not a source)")
    print(f"  _truth_seeded_defects         {n_gt:>8,}   (answer key, not a source)")

    print()
    print("-" * 68)
    print("SEEDED DEFECTS BY TYPE")
    print("-" * 68)
    print(f"  {'Code':<7} {'Name':<38} {'Seeded':>7} {'Sev':>9}")
    print("  " + "-" * 64)
    for row in gt.summary():
        if row["seeded_count"] > 0:
            print(f"  {row['code']:<7} {row['name'][:37]:<38} "
                  f"{row['seeded_count']:>7,} {row['severity']:>9}")
    print("  " + "-" * 64)
    print(f"  {'':<46} {gt.count():>7,}")
    print()
    print("Generation complete.")


if __name__ == "__main__":
    main()
