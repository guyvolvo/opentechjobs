"""What the applier answers ahead of time, and when the API must not use it.

Two failure modes matter more than the speed this buys.

The first is serving a precomputed answer to a question it does not
answer. Facets are counted with the caller's OTHER active filters
applied, so there is one precomputable case (no filters) and an infinite
number of others. handler.py decides by comparing the generated WHERE
against the unfiltered one rather than by listing parameter names, so a
filter added later cannot quietly start getting a stale global count.

The second is assuming the file is there. Code deploys in seconds and
the snapshot only changes when the merge next runs, which has taken this
API down once already. A missing precomputed file has to mean "compute
it here", not an error.

Run directly, no framework:  python tests/test_precompute.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

from job_filters import build_jobs_where  # noqa: E402
from load_to_sqlite import load_resolved, open_db  # noqa: E402
import precompute  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def job(n: int, location: str, dept: str) -> dict:
    return {"external_id": str(n), "ats": "greenhouse", "title": f"Backend Engineer {n}",
            "url": f"https://acme.com/{n}", "location": location, "department": dept,
            "description": "python postgres"}


def seed(tmp: Path) -> Path:
    db = tmp / "snap.db"
    conn = open_db(db)
    jobs = ([job(i, "Tel Aviv, Israel", "Engineering") for i in range(6)]
            + [job(100 + i, "New York, NY", "Sales") for i in range(4)])
    payload = [{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                "confidence": "verified", "job_count": len(jobs), "jobs": jobs}]
    path = tmp / "resolved.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    load_resolved(conn, path, False)
    conn.commit()
    conn.close()
    return db


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    built = precompute.build(seed(tmp))

    check("both artifacts are produced",
          sorted(built) == ["facets.json", "stats.json"], str(sorted(built)))

    stats = built["stats.json"]
    check("stats carries the headline totals",
          stats["totals"]["open_jobs"] == 10, str(stats["totals"]))
    check("stats carries the 14-day series",
          len(stats["open_jobs_history"]) == 14, str(len(stats["open_jobs_history"])))

    # israel_only changes exactly one field, so both versions of it ship
    # together rather than as a second near-identical file.
    check("the israel-scoped locations ride along",
          "top_locations_israel" in stats, str(sorted(stats)))
    il = {r["location"] for r in stats["top_locations_israel"]}
    check("israel-scoped locations exclude the rest of the world",
          il == {"Tel Aviv, Israel"}, str(il))
    everywhere = {r["location"] for r in stats["top_locations"]}
    check("the unscoped locations do not",
          "New York, NY" in everywhere, str(everywhere))

    facets = built["facets.json"]
    check("facets carry all three lists",
          sorted(facets) == ["categories", "companies", "locations"], str(sorted(facets)))
    check("facet counts are the unfiltered ones",
          sum(r["n"] for r in facets["locations"]) == 10, str(facets["locations"]))

    # It has to serialise: it is written as JSON and read back by the API.
    for name, payload in built.items():
        try:
            json.loads(json.dumps(payload, default=str))
            ok, why = True, ""
        except Exception as e:
            ok, why = False, repr(e)
        check(f"{name} survives a JSON round trip", ok, why)

    # publish() is called from a merge that has already pushed a snapshot.
    # It must never raise, whatever S3 or the file does.
    check("publish on a missing database returns empty rather than raising",
          precompute.publish("some-bucket", tmp / "does-not-exist.db") == [])
    check("publish with no bucket configured is a no-op",
          precompute.publish("", tmp / "snap.db") == [])

# The mechanism handler.py uses to decide whether the precomputed answer
# applies. Comparing generated SQL, not a hand-kept list of parameters.
unfiltered = build_jobs_where({}, True)
check("an empty request matches the precomputed question",
      build_jobs_where({}, True) == unfiltered)
check("a default-valued request still matches",
      build_jobs_where({"q": "", "company": "", "location": ""}, True) == unfiltered)
for param, value in (("company", "acme.com"), ("location", "Tel Aviv, Israel"),
                     ("department", "Engineering"), ("q", "python"),
                     ("israel_only", "1"), ("max_age_days", "7"),
                     ("seniority", "senior"), ("include_closed", "1")):
    check(f"a request filtered by {param} does not",
          build_jobs_where({param: value}, True) != unfiltered)

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
