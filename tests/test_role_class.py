"""The tech-role verdict, measured against five hundred hand-labelled
open roles (tests/fixtures/role_gold.json) and pinned on the cases the
rules exist for.

The numbers are the contract. A change that lifts recall by loosening
a rule shows up here as lost precision, and precision is what the
default view rides on: a nurse on the tech board is the failure, a
missed consultant is a smaller one.

Run directly, no framework:  python tests/test_role_class.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from role_class import classify_role  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


gold = json.load(open(ROOT / "tests" / "fixtures" / "role_gold.json", encoding="utf-8"))
conf = {}
for g in gold:
    v, _, _ = classify_role(g["title"], g["department"], g["skills"])
    conf[(g["label"], v)] = conf.get((g["label"], v), 0) + 1
tech_tp = conf.get(("tech", "tech"), 0)
tech_fp = conf.get(("adjacent", "tech"), 0) + conf.get(("non-tech", "tech"), 0)
tech_fn = sum(conf.get(("tech", v), 0) for v in ("adjacent", "non-tech", "unknown"))
unknown = sum(conf.get((g, "unknown"), 0) for g in ("tech", "adjacent", "non-tech"))
precision = tech_tp / (tech_tp + tech_fp)
recall = tech_tp / (tech_tp + tech_fn)
print(f"gold set: tech precision {precision:.3f}, recall {recall:.3f}, unknown {unknown}/{len(gold)}")
check("no more than 1 in 50 roles shown as tech is not tech", precision >= 0.98, f"{precision:.3f}")
check("at least 9 in 10 tech roles are found", recall >= 0.90, f"{recall:.3f}")
check("fewer than 1 in 20 roles are left unknown", unknown <= len(gold) * 0.05, str(unknown))
non_tech_as_tech = conf.get(("non-tech", "tech"), 0)
check("no nurse, cashier, driver or lawyer on the tech board", non_tech_as_tech == 0, str(non_tech_as_tech))

# The shapes the rules were written for, each one a verdict on its own.
cases = [
    ("Senior Software Engineer", "Engineering", "Python,AWS", "tech"),
    ("DFIR Analyst", "13100 DFIR", "Incident Response,EDR", "tech"),
    ("Servicedesk Consultant", "Qobee", "ERP", "tech"),
    ("Information System Security Officer", "", "", "tech"),
    ("Data Center Technician", "Operations, IT, & Support", "AWS,Linux", "tech"),
    ("Product Manager", "Product", "", "tech"),
    ("Hardware Systems Signal Integrity Engineer - iPhone", "Hardware", "", "tech"),
    ("Senior Account Executive (AI & Strategic Acquisitions)", "Direct Field Sales", "Machine Learning,AWS", "adjacent"),
    ("Junior Sales Manager (m/w/d) – B2B-Vertrieb SAP", "", "", "adjacent"),
    ("Sr. Technical Recruiter", "Human Resources", "", "adjacent"),
    ("Customer Support Specialist", "Engineering", "", "adjacent"),
    ("Igbo - AI Product Evaluator", "Developmental Projects", "", "adjacent"),
    ("Open Application", "", "", "adjacent"),
    ("Nurse Practitioner", "", "AWS,EMR", "non-tech"),
    ("Retail Sales Merchandiser", "Retail", "", "non-tech"),
    ("HGV Driver Class 2", "Supply Chain and Operations", "", "non-tech"),
    ("Security and Loss Prevention Specialist, NA", "Investigation & Loss Prevention", "Excel", "non-tech"),
    ("Scientist II, Antibody Discovery (Phage Display)", "Drug Discovery", "Machine Learning,Python", "non-tech"),
    ("Senior Geotechnical Engineer", "", "", "non-tech"),
    ("Senior Design Project Manager (Architect/Interior Design)", "Program Design", "PMP", "non-tech"),
    ("Full Charge Bookkeeper", "", "", "adjacent"),
    ("Vendeuse / Vendeur - F/H", "Retail", "", "non-tech"),
    ("מהנדס/ת תוכנה", "פיתוח", "", "tech"),
    ("אח/ות מוסמך/ת", "סיעוד", "", "non-tech"),
]
for title, team, skills, want in cases:
    v, score, evidence = classify_role(title, team, skills)
    check(f"{title[:48]!r} is {want}", v == want, f"got {v} ({score:+.2f}; {evidence})")

# The company's own mix decides what the title cannot.
bare = classify_role("Project Manager", "", "")
check("a bare project manager is adjacent, and stays so at a tech company", bare[0] == "adjacent" and classify_role("Project Manager", "", "", 0.9)[0] in ("adjacent", "unknown"))
check("evidence names what decided it", "title:tech" in classify_role("Backend Engineer", "R&D", "Go")[2] and "skills:go" in classify_role("Backend Engineer", "R&D", "Go")[2])
check("an empty title is unknown, not a verdict", classify_role("", "", "")[0] == "unknown")

# The API side: roles=tech is one clause, anything else is nothing.
from job_filters import build_jobs_where  # noqa: E402
tech_sql = build_jobs_where({"roles": "tech"}, False)[0]
check("roles=tech filters on the verdict column, and roles=all does not",
      "role_class = 'tech'" in tech_sql and "role_class" not in build_jobs_where({"roles": "all"}, False)[0]
      and "role_class" not in build_jobs_where({}, False)[0], tech_sql)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
