"""
Reference data for the synthetic legacy population.

This module holds the organisational structures that employees are
generated against: cost centres, departments, locations, job families
and pay scales.

WHY THIS IS A SEPARATE MODULE
Reference data is the backbone of realistic HR data. If employees are
assigned random cost centre strings, every downstream referential
integrity check is meaningless, because there is nothing to check
against. Defining a real structure here means the profiling engine can
legitimately ask "does this cost centre exist?" and get a meaningful
answer.

It also means orphan defects can be seeded deliberately: a cost centre
that is NOT in this list is a genuine referential break, not an
accident of generation.

The structures are modelled on a mid-sized UK facilities and
engineering business, which is the sector the source job description
comes from.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------
# Personnel areas in SAP terms. Weighted so the population clusters in
# a head office and a few large sites, as a real business would, rather
# than spreading evenly.

@dataclass(frozen=True)
class Location:
    code: str           # SAP personnel area (werks)
    name: str           # as held in HR.net
    weight: int         # relative share of population


LOCATIONS: list[Location] = [
    Location("1000", "Newcastle Head Office", 30),
    Location("1010", "Gateshead Depot", 12),
    Location("1020", "Sunderland Site", 10),
    Location("2000", "Leeds Regional Office", 14),
    Location("2010", "Sheffield Depot", 8),
    Location("3000", "Manchester Regional Office", 12),
    Location("4000", "Birmingham Site", 9),
    Location("5000", "London Office", 5),
]


# ---------------------------------------------------------------------
# Departments and cost centres
# ---------------------------------------------------------------------
# One department maps to one cost centre and one SAP org unit. In a real
# business the relationship is messier, but a clean one-to-one here keeps
# the conflict testing focused on the HR-versus-payroll disagreement
# rather than on structural ambiguity.

@dataclass(frozen=True)
class Department:
    name: str           # as held in HR.net
    cost_centre: str    # 10 char, SAP kostl
    org_unit: str       # 8 char, SAP orgeh
    weight: int


DEPARTMENTS: list[Department] = [
    Department("Operations",            "CC10001000", "50000001", 28),
    Department("Engineering",           "CC10002000", "50000002", 18),
    Department("Field Services",        "CC10003000", "50000003", 15),
    Department("Finance",               "CC20001000", "50000010", 6),
    Department("Human Resources",       "CC20002000", "50000011", 4),
    Department("Information Technology","CC20003000", "50000012", 7),
    Department("Procurement",           "CC20004000", "50000013", 4),
    Department("Health and Safety",     "CC30001000", "50000020", 5),
    Department("Commercial",            "CC30002000", "50000021", 6),
    Department("Customer Services",     "CC30003000", "50000022", 7),
]


# ---------------------------------------------------------------------
# Job families
# ---------------------------------------------------------------------
# Each job carries a salary band. Generated salaries are drawn within
# the band, which produces a realistic distribution rather than a
# uniform spread, and makes the National Minimum Wage validation in
# Phase 4 meaningful.

@dataclass(frozen=True)
class Job:
    title: str
    job_key: str        # 8 char, SAP stell
    department: str     # must match a Department.name
    salary_min: int
    salary_max: int
    weight: int


JOBS: list[Job] = [
    # Operations
    Job("Operative",                 "60000001", "Operations", 23500, 28000, 14),
    Job("Senior Operative",          "60000002", "Operations", 27000, 33000, 8),
    Job("Team Leader",               "60000003", "Operations", 32000, 39000, 4),
    Job("Operations Manager",        "60000004", "Operations", 45000, 58000, 2),

    # Engineering
    Job("Maintenance Engineer",      "60000010", "Engineering", 32000, 42000, 8),
    Job("Senior Engineer",           "60000011", "Engineering", 42000, 54000, 5),
    Job("Principal Engineer",        "60000012", "Engineering", 54000, 68000, 3),
    Job("Engineering Manager",       "60000013", "Engineering", 62000, 78000, 2),

    # Field Services
    Job("Field Technician",          "60000020", "Field Services", 26000, 33000, 9),
    Job("Senior Field Technician",   "60000021", "Field Services", 32000, 40000, 4),
    Job("Field Services Supervisor", "60000022", "Field Services", 38000, 47000, 2),

    # Finance
    Job("Finance Assistant",         "60000030", "Finance", 24000, 29000, 2),
    Job("Management Accountant",     "60000031", "Finance", 38000, 48000, 2),
    Job("Finance Business Partner",  "60000032", "Finance", 50000, 62000, 1),
    Job("Head of Finance",           "60000033", "Finance", 72000, 90000, 1),

    # HR
    Job("HR Administrator",          "60000040", "Human Resources", 24000, 29000, 1),
    Job("HR Advisor",                "60000041", "Human Resources", 30000, 38000, 2),
    Job("HR Business Partner",       "60000042", "Human Resources", 45000, 56000, 1),

    # IT
    Job("IT Support Analyst",        "60000050", "Information Technology", 26000, 33000, 3),
    Job("Systems Analyst",           "60000051", "Information Technology", 38000, 48000, 2),
    Job("IT Manager",                "60000052", "Information Technology", 55000, 70000, 1),
    Job("Data Analyst",              "60000053", "Information Technology", 35000, 45000, 1),

    # Procurement
    Job("Buyer",                     "60000060", "Procurement", 28000, 36000, 2),
    Job("Senior Buyer",              "60000061", "Procurement", 36000, 46000, 1),
    Job("Procurement Manager",       "60000062", "Procurement", 50000, 62000, 1),

    # Health and Safety
    Job("HSE Advisor",               "60000070", "Health and Safety", 32000, 41000, 3),
    Job("HSE Manager",               "60000071", "Health and Safety", 48000, 60000, 2),

    # Commercial
    Job("Commercial Administrator",  "60000080", "Commercial", 25000, 31000, 2),
    Job("Quantity Surveyor",         "60000081", "Commercial", 38000, 50000, 3),
    Job("Commercial Manager",        "60000082", "Commercial", 55000, 70000, 1),

    # Customer Services
    Job("Customer Service Advisor",  "60000090", "Customer Services", 22500, 27000, 5),
    Job("Customer Service Team Leader","60000091","Customer Services", 29000, 36000, 2),
]


# ---------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------
# The weights matter. A population that is 95% permanent produces very
# few part-time or fixed-term edge cases, and those are where FTE and
# date defects concentrate.

CONTRACT_TYPES: list[tuple[str, int]] = [
    ("Permanent", 78),
    ("Fixed Term", 9),
    ("Temporary", 6),
    ("Contractor", 4),
    ("Apprentice", 3),
]


# ---------------------------------------------------------------------
# Pay scales
# ---------------------------------------------------------------------

PAY_SCALES: list[tuple[str, int]] = [
    ("BAND1", 18),
    ("BAND2", 24),
    ("BAND3", 22),
    ("BAND4", 16),
    ("BAND5", 11),
    ("BAND6", 6),
    ("SMT", 3),
]


# ---------------------------------------------------------------------
# Employment status and leaver reasons
# ---------------------------------------------------------------------

EMPLOYMENT_STATUSES: list[tuple[str, int]] = [
    ("Active", 88),
    ("Leaver", 10),
    ("Suspended", 1),
    ("On Leave", 1),
]

LEAVER_REASONS: list[tuple[str, int]] = [
    ("Resignation", 52),
    ("End of Contract", 18),
    ("Redundancy", 12),
    ("Retirement", 9),
    ("Dismissal", 7),
    ("Death in Service", 2),
]


# ---------------------------------------------------------------------
# Payroll reference data
# ---------------------------------------------------------------------

TAX_CODES: list[tuple[str, int]] = [
    ("1257L", 74),
    ("1257L W1/M1", 5),
    ("BR", 6),
    ("D0", 3),
    ("0T", 3),
    ("K475", 2),
    ("1185L", 4),
    ("NT", 1),
    ("S1257L", 2),   # Scottish taxpayer
]

NI_CATEGORIES: list[tuple[str, int]] = [
    ("A", 80),
    ("B", 4),
    ("C", 4),
    ("H", 5),
    ("J", 3),
    ("M", 4),
]

PAYMENT_METHODS: list[tuple[str, int]] = [
    ("BACS", 93),
    ("Bank Transfer", 5),
    ("Cheque", 2),
]

PENSION_SCHEMES: list[tuple[str, int]] = [
    ("AUTO-ENROL", 71),
    ("SALARY-SACRIFICE", 18),
    ("OPTED-OUT", 9),
    ("LEGACY-DB", 2),
]

# Real UK bank sort code prefixes, so generated sort codes look
# plausible rather than random six digit strings.
BANKS: list[tuple[str, str, int]] = [
    # (bank name, sort code prefix, weight)
    ("Barclays Bank",            "20", 18),
    ("Lloyds Bank",              "30", 16),
    ("HSBC UK",                  "40", 14),
    ("NatWest",                  "60", 15),
    ("Santander UK",             "09", 10),
    ("Halifax",                  "11", 8),
    ("Nationwide Building Society","07", 7),
    ("TSB Bank",                 "77", 5),
    ("Co-operative Bank",        "08", 4),
    ("Monzo Bank",               "04", 3),
]


# ---------------------------------------------------------------------
# Lookups and helpers
# ---------------------------------------------------------------------

def department_by_name(name: str) -> Department | None:
    """Return the department record for a name, or None if not found."""
    for d in DEPARTMENTS:
        if d.name == name:
            return d
    return None


def jobs_for_department(dept_name: str) -> list[Job]:
    """All jobs belonging to a department."""
    return [j for j in JOBS if j.department == dept_name]


def valid_cost_centres() -> set[str]:
    """
    The complete set of legitimate cost centres.

    Used both by the generator, to assign valid values, and by the
    profiling engine, to detect orphans. A cost centre outside this set
    is by definition a referential integrity break.
    """
    return {d.cost_centre for d in DEPARTMENTS}


def valid_org_units() -> set[str]:
    return {d.org_unit for d in DEPARTMENTS}


def valid_job_keys() -> set[str]:
    return {j.job_key for j in JOBS}


def valid_location_codes() -> set[str]:
    return {loc.code for loc in LOCATIONS}


def weighted_choice(rng, options: list[tuple]) -> object:
    """
    Choose from a list of (value, weight) tuples.

    Written explicitly rather than using random.choices so the caller
    controls the RNG instance, which is what makes the whole generator
    reproducible from a seed.
    """
    total = sum(w for _, w in options)
    r = rng.uniform(0, total)
    upto = 0.0
    for value, weight in options:
        upto += weight
        if r <= upto:
            return value
    return options[-1][0]


def weighted_choice_obj(rng, items: list, weight_attr: str = "weight"):
    """As weighted_choice, but for dataclass instances carrying a weight."""
    total = sum(getattr(i, weight_attr) for i in items)
    r = rng.uniform(0, total)
    upto = 0.0
    for item in items:
        upto += getattr(item, weight_attr)
        if r <= upto:
            return item
    return items[-1]


# ---------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------
# Reference data with an internal inconsistency would silently corrupt
# the entire population, so it validates itself on import.

def _validate() -> None:
    dept_names = {d.name for d in DEPARTMENTS}

    orphan_jobs = [j.title for j in JOBS if j.department not in dept_names]
    if orphan_jobs:
        raise ValueError(
            f"Jobs reference departments that do not exist: {orphan_jobs}"
        )

    empty_depts = [d.name for d in DEPARTMENTS if not jobs_for_department(d.name)]
    if empty_depts:
        raise ValueError(f"Departments with no jobs defined: {empty_depts}")

    if len(valid_cost_centres()) != len(DEPARTMENTS):
        raise ValueError("Cost centres are not unique across departments")

    if len(valid_job_keys()) != len(JOBS):
        raise ValueError("Job keys are not unique")

    if len(valid_location_codes()) != len(LOCATIONS):
        raise ValueError("Location codes are not unique")

    bad_bands = [j.title for j in JOBS if j.salary_min >= j.salary_max]
    if bad_bands:
        raise ValueError(f"Jobs with invalid salary bands: {bad_bands}")


_validate()
