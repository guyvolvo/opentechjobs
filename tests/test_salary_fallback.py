"""The order salary sources fall back in, end to end through probe.py.

Order is the whole point. A disclosed range must never be overwritten by
anything we computed, the Israeli table must only ever speak about
Israel, and the learned model must only ever speak about markets whose
currency we can actually name. Getting any of those backwards puts a
number in front of a reader under a claim it cannot support.

Run directly, no framework:  python tests/test_salary_fallback.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import probe  # noqa: E402
from salary_model import MIN_ROWS, SalaryModel  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def job(title="Backend Engineer", location="San Francisco", seniority="senior",
        department="Engineering", salary=None, source=None):
    return probe.Job(
        ats="ashby", token="acme", external_id="1", title=title, location=location,
        url="https://x/1", department=department, seniority=seniority,
        salary_text=salary, salary_is_estimate=bool(salary and source != "disclosed"),
        salary_source=source,
    )


def install_matrix():
    """A tiny published matrix, in place of the real S3 download."""
    rows = [({"company_domain": "acme.com", "location": "San Francisco",
              "seniority": "senior", "department": "Engineering"}, 180_000 + i * 5000)
            for i in range(MIN_ROWS + 3)]
    probe._salary_matrix = {"$": SalaryModel.build(rows, "$")}
    probe._salary_matrix_loaded = True


install_matrix()

# A US listing with no disclosed figure gets the learned band.
out = probe._fill_classifications([job()], "acme.com")[0]
check("a US listing gets a learned estimate", out.salary_source, "estimated")
check("and is flagged an estimate", out.salary_is_estimate, True)
check("formatted in thousands, like the ranges it learned from",
      out.salary_text, "$182K - $213K")

# A disclosed figure is never touched by anything downstream.
out = probe._fill_classifications([job(salary="$200K - $240K", source="disclosed")], "acme.com")[0]
check("a disclosed range survives untouched", out.salary_text, "$200K - $240K")
check("and keeps its source", out.salary_source, "disclosed")

# Israel goes to the table, never the matrix. There are no Israeli
# disclosed ranges, so the matrix has nothing to say about Tel Aviv.
out = probe._fill_classifications([job(location="Tel Aviv, Israel")], "acme.com")[0]
check("an Israeli listing uses the table", out.salary_source, "table")
check("and gets a shekel figure", out.salary_text.startswith("₪"), True)

# An Israeli listing the table cannot place gets nothing, rather than
# falling through to a model that knows nothing about that market.
out = probe._fill_classifications(
    [job(title="Warehouse Night Associate", location="Tel Aviv, Israel", seniority=None)],
    "acme.com")[0]
check("an unpriceable Israeli listing gets nothing, not a dollar figure",
      out.salary_text, None)

# A location we cannot place has no known currency, and a figure in the
# wrong currency is not a smaller error than no figure.
out = probe._fill_classifications([job(location="Shenzhen HQ")], "acme.com")[0]
check("an unplaceable location gets no estimate", out.salary_source, None)
check("and no text", out.salary_text, None)

# A market with no model in the matrix stays quiet too.
out = probe._fill_classifications([job(location="London")], "acme.com")[0]
check("a market absent from the matrix gets nothing", out.salary_text, None)

# The switch, because this changes what a reader sees on most of the
# board and should be reversible faster than a deploy.
saved = probe._LEARNED_ESTIMATES_ON
try:
    probe._LEARNED_ESTIMATES_ON = False
    probe._salary_matrix, probe._salary_matrix_loaded = None, False
    check("the off switch stops the download", probe._load_salary_matrix(), {})
finally:
    probe._LEARNED_ESTIMATES_ON = saved
    install_matrix()

# A missing matrix is not an error. It is the behaviour from before any
# of this existed.
probe._salary_matrix, probe._salary_matrix_loaded = {}, True
out = probe._fill_classifications([job()], "acme.com")[0]
check("no matrix means no estimate, not a failure", out.salary_text, None)
install_matrix()

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
