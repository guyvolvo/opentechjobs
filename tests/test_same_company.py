"""One company on two ATSes, listed in api/same_company.py.

Wayve showed every role twice: wayve.ai on Greenhouse and wayve.fr on
Ashby, both found by guessing the token "wayve". The same-board alias
check cannot see it, because the two boards share nothing but jobs. So
the duplicate is listed by hand and the loader closes it on every load.

The ways this can go wrong, each checked below:

  the duplicate's open jobs stay open            -> it still shows twice
  an "unchanged" re-poll writes its ats back      -> it returns to known.json
  a real fetch of it re-opens jobs under it       -> it shows twice again
  the kept domain gets touched                    -> Wayve disappears
  a finished demotion bumps last_checked forever  -> it wins every merge

Run directly, no framework:  python tests/test_same_company.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

from load_to_sqlite import export_known, load_resolved, open_db  # noqa: E402
from same_company import SAME_COMPANY  # noqa: E402

TS_OLD = "2026-09-01T00:00:00+00:00"
DUP, KEEP = "wayve.fr", "wayve.ai"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def add_company(conn, domain, ats, n_jobs):
    conn.execute(
        "INSERT INTO companies (domain, ats, token, confidence, job_count, tried, first_seen, last_checked) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (domain, ats, "wayve", "verified", n_jobs, 1, TS_OLD, TS_OLD),
    )
    for i in range(n_jobs):
        conn.execute(
            "INSERT INTO jobs (id, company_domain, ats, external_id, title, confidence, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"{domain}-{i}", domain, ats, str(i), f"Engineer {i}", "verified", TS_OLD, TS_OLD),
        )


def seed(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    with conn:
        add_company(conn, KEEP, "greenhouse", 2)
        add_company(conn, DUP, "ashby", 3)
    return conn


def run_load(conn, payload, tmp: Path) -> None:
    p = tmp / "resolved.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with conn:
        load_resolved(conn, p)


def company(conn, domain):
    return conn.execute("SELECT ats, token, error, last_checked FROM companies WHERE domain = ?",
                        (domain,)).fetchone()


def open_jobs(conn, domain):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE company_domain = ? AND closed_at IS NULL",
                        (domain,)).fetchone()[0]


def main() -> int:
    check("Wayve is listed, keeping wayve.ai", SAME_COMPANY.get(DUP) == KEEP, repr(SAME_COMPANY))
    check("no kept domain is itself listed as a duplicate",
          not set(SAME_COMPANY.values()) & set(SAME_COMPANY), repr(SAME_COMPANY))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 1. A load that does not mention the duplicate at all still closes it.
        conn = seed(tmp / "a.db")
        run_load(conn, [{"domain": "other.com", "ats": None, "token": None, "jobs": []}], tmp)
        row = company(conn, DUP)
        check("the duplicate's open jobs close", open_jobs(conn, DUP) == 0, str(open_jobs(conn, DUP)))
        check("the duplicate is no longer resolved", row["ats"] is None and row["token"] is None, str(dict(row)))
        check("its error names the kept domain", KEEP in (row["error"] or ""), repr(row["error"]))
        check("the kept domain keeps its jobs", open_jobs(conn, KEEP) == 2, str(open_jobs(conn, KEEP)))
        check("the kept domain stays resolved", company(conn, KEEP)["ats"] == "greenhouse")

        # 2. Once done, a later load leaves it alone.
        stamped = company(conn, DUP)["last_checked"]
        run_load(conn, [{"domain": "other.com", "ats": None, "token": None, "jobs": []}], tmp)
        check("a finished demotion does not move last_checked again",
              company(conn, DUP)["last_checked"] == stamped, company(conn, DUP)["last_checked"])
        conn.close()

        # 3. An "unchanged" re-poll of the duplicate does not resurrect it.
        conn = seed(tmp / "b.db")
        run_load(conn, [{"domain": DUP, "ats": "ashby", "token": "wayve", "unchanged": True, "jobs": []}], tmp)
        check("unchanged does not write its ats back", company(conn, DUP)["ats"] is None,
              str(dict(company(conn, DUP))))
        conn.close()

        # 4. Nor does a real fetch that carries jobs.
        conn = seed(tmp / "c.db")
        run_load(conn, [{"domain": DUP, "ats": "ashby", "token": "wayve", "job_count": 1,
                         "jobs": [{"ats": "ashby", "external_id": "new", "title": "Engineer"}]}], tmp)
        check("a fetch of the duplicate opens no jobs under it", open_jobs(conn, DUP) == 0, str(open_jobs(conn, DUP)))
        check("and leaves it unresolved", company(conn, DUP)["ats"] is None, str(dict(company(conn, DUP))))

        # 5. known.json leaves it out even while its row still says resolved.
        with conn:
            conn.execute("UPDATE companies SET ats = 'ashby', token = 'wayve' WHERE domain = ?", (DUP,))
        out = tmp / "known.json"
        export_known(conn, out)
        domains = {k["domain"] for k in json.loads(out.read_text(encoding="utf-8"))}
        check("known.json skips the duplicate", DUP not in domains and KEEP in domains, repr(domains))
        conn.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
