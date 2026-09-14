"""A Greenhouse board that posts one job twice shows it once.

Fireblocks published "Information Security Engineer" as two postings of
the same internal job, same title, same city, same text, and the board
listed both. The other half of this test is the case that must not
collapse: one job posted separately for each city it hires in.

Run directly, no framework:  python tests/test_greenhouse_dupes.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import probe  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


TLV = "Tel Aviv-Yafo, Tel Aviv District, Israel"


def posting(pid, internal, title, city, published):
    return {"id": pid, "internal_job_id": internal, "title": title, "location": {"name": city},
            "absolute_url": f"https://example.com/{pid}", "updated_at": "2026-09-14T06:35:58-04:00",
            "first_published": published, "departments": [{"name": "Security"}],
            "content": "<p>Same text</p>"}


BOARD = {"jobs": [
    # Fireblocks' pair, the later-published one first in the payload.
    posting(4701826006, 4557810006, "Information Security Engineer", TLV, "2026-08-03T08:43:51-04:00"),
    posting(4701824006, 4557810006, "Information Security Engineer", TLV, "2026-08-03T08:43:09-04:00"),
    # One job hiring in two cities: two real listings.
    posting(900, 77, "Backend Engineer", TLV, "2026-08-01T00:00:00Z"),
    posting(901, 77, "Backend Engineer", "Haifa, Israel", "2026-08-01T00:00:00Z"),
    # No internal id at all: never merged with anything.
    posting(950, None, "Designer", TLV, "2026-08-01T00:00:00Z"),
    posting(951, None, "Designer", TLV, "2026-08-01T00:00:00Z"),
]}

probe.get_json = lambda sess, url: BOARD
jobs = probe.f_greenhouse(None, "fireblocks")
ids = [j.external_id for j in jobs]

check("the twice-posted job appears once",
      sum(1 for j in jobs if j.title == "Information Security Engineer") == 1, repr(ids))
check("the earlier-published posting is the one kept",
      "4701824006" in ids and "4701826006" not in ids, repr(ids))
check("one job in two cities stays two listings", "900" in ids and "901" in ids, repr(ids))
check("postings without an internal id are left alone", "950" in ids and "951" in ids, repr(ids))
check("the board's own order is kept", ids == ["4701824006", "900", "901", "950", "951"], repr(ids))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
