"""The learned estimator: what it parses, where it backs off, when it stays quiet.

The parser gets the most attention here because it is the part that can
quietly poison everything downstream. A model built on mis-parsed pay
looks perfectly healthy: the cells fill, the medians compute, the
backtest prints a number. The first version of this read "$17 - $24 per
hour, $2K - $10K commission" as a ten-million-dollar job, and nothing but
an eyeball on the extremes caught it.

Run directly, no framework:  python tests/test_salary_model.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from salary_model import (  # noqa: E402
    MAX_REL_SPREAD,
    MIN_ROWS,
    SalaryModel,
    metro_of,
    parse_disclosed,
)

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}{'  ' + detail if detail else ''}")


# Every shape that actually occurs in the data, by frequency.
check("plain annual range", parse_disclosed("$180K - $220K"), ("$", 180_000.0, 220_000.0))
check("comma thousands", parse_disclosed("$112,000 - $185,000"), ("$", 112_000.0, 185_000.0))
check("single figure", parse_disclosed("$70K"), ("$", 70_000.0, 70_000.0))
check("hourly annualised", parse_disclosed("$50 – $60 per hour"), ("$", 104_000.0, 124_800.0))
check("monthly annualised", parse_disclosed("$8,000 – $9,000 per month"), ("$", 96_000.0, 108_000.0))
check("sterling stays sterling", parse_disclosed("£130K - £180K"), ("£", 130_000.0, 180_000.0))
check("canadian dollars are their own currency",
      parse_disclosed("CA$120K - CA$150K"), ("CA$", 120_000.0, 150_000.0))

# The one that produced a ten-million-dollar job. Only the first clause
# is the salary; commission and bonus are not, and annualising them as
# an hourly rate is catastrophic rather than merely wrong.
check("commission after the bullet is ignored",
      parse_disclosed("$17 – $24 per hour • $2K – $10K Commission"),
      ("$", 35_360.0, 49_920.0))
check("equity after the bullet is ignored",
      parse_disclosed("$24 per hour • Offers Equity"), ("$", 49_920.0, 49_920.0))

# R$ is Brazilian real. Reading it as USD put a 1M-dollar row in the
# US cells.
check("R$ is not a dollar sign", parse_disclosed("R$920,000 - R$1,150,000"), None)
check("mixed currencies are refused, not guessed",
      parse_disclosed("$100K - £150K"), None)
check("no money at all", parse_disclosed("Competitive"), None)
check("below the sane floor is dropped", parse_disclosed("$0 – $1 per hour"), None)

# Metros, because raw location text gives one cell per listing.
check("bay area suburb", metro_of("San Carlos  - Hybrid"), "us-sf")
check("new york phrasing", metro_of("US - New York"), "us-nyc")
check("israel", metro_of("Israel, Tel Aviv"), "il")
check("US remote is its own market", metro_of("United States, Remote"), "us-remote")
# "Remote" alone says nothing about which country's pay scale applies.
# Reported live: "Remote - Singapore" was being priced from US cells in
# US dollars because the pattern matched the word and stopped looking.
check("remote elsewhere is not the US market", metro_of("Remote - Singapore"), None)
check("nor is bare remote", metro_of("Remote"), None)
check("a named place beats remote", metro_of("Remote - London"), "uk")
# Two-letter state codes collide with country codes: IN is Indiana and
# India, DE is Delaware and Germany.
check("an ambiguous country code is not a US state", metro_of("Remote - Delhi, IN"), None)
check("unplaceable location", metro_of("Shenzhen HQ"), None)
check("no location", metro_of(None), None)


def job(company="acme.com", location="San Francisco", seniority="senior", department="Engineering"):
    return {"company_domain": company, "location": location,
            "seniority": seniority, "department": department}


# The tightest cell answers when it has the rows for it.
rows = [(job(), 190_000 + i * 1000) for i in range(MIN_ROWS + 3)]
model = SalaryModel.build(rows, "$")
got = model.predict(job())
check_true("the tightest cell answers when it has MIN_ROWS",
           got is not None and got["level"] == "company+metro+seniority", str(got))

# One row short, so it must back off rather than quote a cell of four.
thin = [(job(company="thin.com"), 190_000 + i * 1000) for i in range(MIN_ROWS - 1)]
padding = [(job(company=f"other{i}.com"), 150_000 + i * 2000) for i in range(MIN_ROWS + 2)]
model = SalaryModel.build(thin + padding, "$")
got = model.predict(job(company="thin.com"))
check_true("a cell below MIN_ROWS backs off to a broader one",
           got is None or got["level"] != "company+metro+seniority", str(got))

# A company nobody has ever disclosed for still gets an answer from the
# market it sits in. This is the coverage the whole exercise is for.
got = model.predict(job(company="never-seen-before.com"))
check_true("an unseen company still lands on a market cell",
           got is None or not got["level"].startswith("company"), str(got))

# A cell whose middle half is still absurdly wide says nothing at all.
# This is the ₪20K-100K lesson, enforced rather than hoped for.
spread = [(job(company="wide.com"), pay) for pay in
          (30_000, 40_000, 60_000, 200_000, 400_000, 900_000, 1_200_000)]
model = SalaryModel.build(spread, "$")
got = model.predict(job(company="wide.com"))
check_true("a cell too wide to inform anyone is suppressed",
           got is None or (got["high"] - got["low"]) / got["low"] <= MAX_REL_SPREAD,
           str(got))

# A row missing the field a level keys on must not pool with every other
# row missing it. "None seniority at acme" is not a cell.
rows = [(job(seniority=None), 100_000 + i * 1000) for i in range(MIN_ROWS + 2)]
model = SalaryModel.build(rows, "$")
got = model.predict(job(seniority=None))
check_true("a missing key part is skipped, not grouped as itself",
           got is None or "seniority" not in got["level"], str(got))

# Survives a round trip, since the model ships as JSON and its keys are
# tuples, which JSON has no notion of.
rows = [(job(), 190_000 + i * 1000) for i in range(MIN_ROWS + 3)]
original = SalaryModel.build(rows, "$")
restored = SalaryModel.from_json(original.to_json())
check("a round trip through JSON predicts identically",
      restored.predict(job()), original.predict(job()))

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
