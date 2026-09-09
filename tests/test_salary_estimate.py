"""What the salary estimator is allowed to say, and when it must stay quiet.

Every case here comes from a real listing on the board that the previous
version got wrong, so these are regressions, not hypotheticals. Run:

    python tests/test_salary_estimate.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from probe import (  # noqa: E402
    _MAX_ESTIMATE_REL_WIDTH,
    _IL_SALARY_TABLE_KNIS,
    _classify_seniority,
    _estimate_salary,
)

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n      got  {got!r}\n      want {want!r}")


def check_true(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}{('  ' + detail) if detail else ''}")


def width(rng):
    lo, hi = rng
    return (hi - lo) / lo


# An unstated level is a level, not an absence of one. This used to span
# every band the role had, which is where the flat 20K-100K came from.
est = _estimate_salary("Fullstack Engineer", None, None)
check("unstated level uses the 3-5y band", est, _IL_SALARY_TABLE_KNIS[("fullstack", "3-5")])
check_true("unstated level is not the whole ladder", width(est) < 1.0, f"width {width(est):.2f}")

# A stated level still wins over the unstated default.
check("stated senior beats the default",
      _estimate_salary("Senior Fullstack Engineer", None, "senior"),
      _IL_SALARY_TABLE_KNIS[("fullstack", "6-10")])

# The width gate. Nothing this wide is worth showing.
for title, seniority in [("Motion, Video & Brand Designer", None),
                         ("GRC Operations Specialist", None),
                         ("Domain Consultant - Idira", None)]:
    est = _estimate_salary(title, None, seniority)
    check_true(f"no estimate, or a usable one, for {title!r}",
               est is None or width(est) <= _MAX_ESTIMATE_REL_WIDTH,
               f"got {est}")

# The one that mattered most: a description describes the company as
# much as the role. Every listing at a security vendor says "security".
security_boilerplate = (
    "We are a leading cyber security company. Our security platform "
    "protects enterprises from cyber threats."
)
check("a designer at a security vendor is not a security expert",
      _estimate_salary("Motion, Video & Brand Designer", security_boilerplate, None),
      None)
check("nor is a GRC specialist",
      _estimate_salary("GRC Operations Specialist", security_boilerplate, None),
      None)

# But the reason description matching exists still has to work: a
# catch-all title whose body names the actual language.
check("a generic title is refined by the language in its body",
      _estimate_salary("Software Engineer", "Backend work, preferably in Go (Golang).", "senior"),
      _IL_SALARY_TABLE_KNIS[("go", "6-10")])
check("a specific title is not overridden by its body",
      _estimate_salary("Senior Python Developer", "Our stack also uses Go (Golang).", "senior"),
      _IL_SALARY_TABLE_KNIS[("python", "6-10")])

# Levels the source has no rows for at all.
check("director gets nothing", _estimate_salary("Director of Engineering", None, "director"), None)
check("exec gets nothing", _estimate_salary("VP R&D", None, "exec"), None)

# QA and automation rows stop at 6-10y, so a QA manager has no
# management column. Nearest band, not every band.
est = _estimate_salary("QA Manager", None, "manager")
check_true("a QA manager falls back to the nearest band it has",
           est == _IL_SALARY_TABLE_KNIS[("qa_generic", "6-10")], f"got {est}")

# No title, nothing to go on.
check("no title, no estimate", _estimate_salary(None, "a description", "senior"), None)
check("an unmatched title gets no estimate",
      _estimate_salary("Warehouse Night Shift Associate", None, None), None)

# Seniority: \b made "team lead" fail against "Team Leader".
check("Team Leader reads as lead", _classify_seniority("DevOps Team Leader"), "lead")
check("Group Leader reads as lead", _classify_seniority("Group Leader, Platform"), "lead")
check("Account Executive is still not an internship",
      _classify_seniority("International Account Executive"), None)

# Nothing the table can produce may exceed the gate.
for (role, band), rng in _IL_SALARY_TABLE_KNIS.items():
    check_true(f"source row {role}/{band} is usable on its own",
               width(rng) <= _MAX_ESTIMATE_REL_WIDTH * 3,
               f"width {width(rng):.2f}")

if FAILS:
    print(f"{len(FAILS)} failed:\n")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all salary estimator checks passed")
