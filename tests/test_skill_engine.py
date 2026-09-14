"""The skill matcher (api/skills.py) against the labelled cases.

tests/fixtures/skill_cases.json is shared with the browser engine; see
tests/test_cv_skills.mjs, which also checks the two engines agree. This file
covers the Python side and the shape of the rules themselves.

Run directly, no framework:  python tests/test_skill_engine.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.stdout.reconfigure(encoding="utf-8")

import skills  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# The rules are data both engines read, so their shape is checked here.
labels = set(skills.LABELS)
check("every rule label is a known label",
      all(l in labels for r in skills.RULES for l in r["labels"]),
      repr([l for r in skills.RULES for l in r["labels"] if l not in labels]))
check("every label has a rule of its own",
      all(any(r["labels"][0] == l for r in skills.RULES) for l in skills.LABELS),
      repr([l for l in skills.LABELS if not any(r["labels"][0] == l for r in skills.RULES)]))
check("ci needles are lowercase", all(n == n.lower() for r in skills.RULES for n in r.get("ci", [])))
check("whole is a plain flag where it appears", all(r.get("whole") is True for r in skills.RULES if "whole" in r))
check("context applies only to rules with case-sensitive spellings",
      all(r.get("cs") for r in skills.RULES if r.get("context")))
check("the spec round-trips through JSON", json.loads(json.dumps(skills.spec()))["rules"] == skills.RULES)
for key, src in skills.CUES.items():
    try:
        re.compile(src)
        ok = True
    except re.error:
        ok = False
    check(f"cue {key} compiles", ok)

# Normalisation, which both engines apply before anything else.
check("ligatures fold", skills.normalize("conﬁg") == "config")
check("a word hyphenated across a line joins", skills.normalize("Kuber-\nnetes") == "Kubernetes")
check("letter-spaced words close up", skills.normalize("P Y T H O N, D O C K E R") == "PYTHON, DOCKER")
check("typographic dashes become hyphens", skills.normalize("go–to") == "go-to")
check("single letters in prose are left alone", skills.normalize("a plan B") == "a plan B")

# The labelled cases.
cases = json.loads((ROOT / "tests" / "fixtures" / "skill_cases.json").read_text(encoding="utf-8"))["cases"]
expected_total = hits = forbidden_hits = 0
for case in cases:
    found = set(skills.extract_labels(case["text"]))
    expect, forbid = set(case.get("expect", [])), set(case.get("forbid", []))
    missing, wrong = expect - found, forbid & found
    expected_total += len(expect)
    hits += len(expect & found)
    forbidden_hits += len(wrong)
    check(f"case {case['id']}", not missing and not wrong,
          f"missing {sorted(missing)} wrongly found {sorted(wrong)} (found {sorted(found)})")

# Held-out full CVs, written after the engine and not tuned against it.
docs = json.loads((ROOT / "tests" / "fixtures" / "cv_documents.json").read_text(encoding="utf-8"))["documents"]
for doc in docs:
    found = set(skills.extract_labels(doc["text"]))
    expect, forbid = set(doc.get("expect", [])), set(doc.get("forbid", []))
    missing, wrong = expect - found, forbid & found
    expected_total += len(expect)
    hits += len(expect & found)
    forbidden_hits += len(wrong)
    check(f"cv {doc['id']}", not missing and not wrong,
          f"missing {sorted(missing)} wrongly found {sorted(wrong)} (extra {sorted(found - expect)})")

recall = hits / expected_total if expected_total else 1.0
print(f"\nrecall {hits}/{expected_total} = {recall:.1%}, forbidden labels found: {forbidden_hits}")

# Job tagging keeps its cap and its order.
tags = skills.extract_labels("Senior Engineer: Python, Go, Docker, Kubernetes, AWS, Terraform, Kafka", limit=5)
check("job tags are capped at the limit, in order of appearance",
      tags == ["Python", "Go", "Docker", "Kubernetes", "AWS"], repr(tags))
counted = {d["label"]: d["count"] for d in skills.extract("Python here, Python there, and Docker")}
check("occurrences are counted", counted.get("Python") == 2 and counted.get("Docker") == 1, repr(counted))
check("empty text finds nothing", skills.extract("") == [] and skills.extract(None) == [])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
