"""The "unchanged" result state, which is the one part of conditional-GET
polling that can destroy live data if it's wrong.

Background: probe.py's --known re-poll answers per company. Adding
conditional GET means a third answer beyond "here are the jobs" and "that
failed" -- namely "the board is byte-for-byte what you already have, so I
didn't fetch it." The danger is that an unchanged company looks exactly
like a company whose board went empty: both arrive with jobs == [].
load_to_sqlite.close_missing_jobs closes every open job for any domain it
sees with a job list, so conflating the two would mark every listing at
that company closed. On a shard where most companies legitimately return
304, that's a mass close-out of live jobs.

These tests pin the distinction down in all four directions, because
three of them are behaviours we must NOT break while adding the fourth:

  unchanged      -> keep the jobs, keep ats/token/job_count, bump last_checked
  empty board    -> close the jobs (a real board that really went empty)
  retryable miss -> keep the jobs, keep ats/token (a transient blip)
  normal fetch   -> close only the jobs that actually disappeared

Run directly, no framework:  python tests/test_unchanged_state.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

from load_to_sqlite import load_resolved, open_db  # noqa: E402

TS_OLD = "2026-09-01T00:00:00+00:00"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def seed(db_path: Path, domain: str = "acme.com", n_jobs: int = 3) -> sqlite3.Connection:
    """A company already resolved on a previous run, with open jobs."""
    conn = open_db(db_path)
    with conn:
        conn.execute(
            "INSERT INTO companies (domain, ats, token, confidence, job_count, tried, first_seen, last_checked) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (domain, "greenhouse", "acme", "verified", n_jobs, 1, TS_OLD, TS_OLD),
        )
        for i in range(n_jobs):
            conn.execute(
                "INSERT INTO jobs (id, company_domain, ats, external_id, title, confidence, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"acme-{i}", domain, "greenhouse", str(i), f"Engineer {i}", "verified", TS_OLD, TS_OLD),
            )
    return conn


def run_load(conn: sqlite3.Connection, payload: list[dict], tmp: Path) -> None:
    p = tmp / "resolved.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with conn:
        load_resolved(conn, p)


def state(conn: sqlite3.Connection, domain: str = "acme.com") -> dict:
    row = conn.execute(
        "SELECT ats, token, job_count, last_checked FROM companies WHERE domain = ?", (domain,)
    ).fetchone()
    open_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE company_domain = ? AND closed_at IS NULL", (domain,)
    ).fetchone()[0]
    return {"ats": row["ats"], "token": row["token"], "job_count": row["job_count"],
            "last_checked": row["last_checked"], "open_jobs": open_jobs}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 1. The whole point. An unchanged board carries no jobs, and must
        #    leave the ones already stored completely alone.
        conn = seed(tmp / "a.db")
        run_load(conn, [{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                         "unchanged": True, "jobs": []}], tmp)
        s = state(conn)
        check("unchanged keeps every open job", s["open_jobs"] == 3, f"open_jobs={s['open_jobs']}")
        check("unchanged preserves ats/token", s["ats"] == "greenhouse" and s["token"] == "acme", str(s))
        check("unchanged preserves job_count", s["job_count"] == 3, f"job_count={s['job_count']}")
        check("unchanged still bumps last_checked", s["last_checked"] != TS_OLD, s["last_checked"])
        conn.close()

        # 2. The control. Identical payload minus the flag is exactly the
        #    mass-closure hazard, and it must still behave that way --
        #    a board that genuinely went empty really has closed its jobs.
        conn = seed(tmp / "b.db")
        run_load(conn, [{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                         "job_count": 0, "jobs": []}], tmp)
        s = state(conn)
        check("a genuinely empty board still closes its jobs", s["open_jobs"] == 0, f"open_jobs={s['open_jobs']}")
        conn.close()

        # 3. Pre-existing behaviour that must survive the change: a
        #    transient re-poll failure is inconclusive, not evidence.
        conn = seed(tmp / "c.db")
        run_load(conn, [{"domain": "acme.com", "ats": None, "token": None,
                         "retryable": True, "error": "timeout", "jobs": []}], tmp)
        s = state(conn)
        check("retryable miss keeps jobs", s["open_jobs"] == 3, f"open_jobs={s['open_jobs']}")
        check("retryable miss keeps ats", s["ats"] == "greenhouse", str(s["ats"]))
        conn.close()

        # 4. Pre-existing behaviour that must survive the change: a real
        #    fetch closes exactly the jobs that vanished, no more.
        conn = seed(tmp / "d.db")
        run_load(conn, [{"domain": "acme.com", "ats": "greenhouse", "token": "acme", "job_count": 2,
                         "jobs": [
                             {"ats": "greenhouse", "external_id": "0", "title": "Engineer 0"},
                             {"ats": "greenhouse", "external_id": "1", "title": "Engineer 1"},
                         ]}], tmp)
        s = state(conn)
        check("a real fetch closes only what disappeared", s["open_jobs"] == 2, f"open_jobs={s['open_jobs']}")
        conn.close()

        # 5. A shard is a mix. One unchanged company must not be collateral
        #    damage from a different company in the same payload changing.
        conn = seed(tmp / "e.db")
        with conn:
            conn.execute(
                "INSERT INTO companies (domain, ats, token, confidence, job_count, tried, first_seen, last_checked) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("other.com", "lever", "other", "verified", 1, 1, TS_OLD, TS_OLD),
            )
            conn.execute(
                "INSERT INTO jobs (id, company_domain, ats, external_id, title, confidence, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("other-0", "other.com", "lever", "0", "Designer", "verified", TS_OLD, TS_OLD),
            )
        run_load(conn, [
            {"domain": "acme.com", "ats": "greenhouse", "token": "acme", "unchanged": True, "jobs": []},
            {"domain": "other.com", "ats": "lever", "token": "other", "job_count": 0, "jobs": []},
        ], tmp)
        check("mixed shard: unchanged company untouched", state(conn)["open_jobs"] == 3,
              str(state(conn)["open_jobs"]))
        check("mixed shard: changed company still closes",
              state(conn, "other.com")["open_jobs"] == 0, str(state(conn, "other.com")["open_jobs"]))
        conn.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
