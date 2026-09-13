"""Saved jobs: the ids filter that feeds the Saved view, and the rows
behind it.

Stars used to live in the reader's own localStorage. That failed in two
ways nobody could work around: a star did not follow you to a phone, and
the Saved view could only show the stars that happened to be among the
50 rows the board had loaded, so anything older simply was not there.
The fix has two halves. Stars are rows in the same DynamoDB table as the
alerts, and the board reads them back with /api/jobs?ids=...

So the tests here are about the seams between those halves. Does an ids
request come back with exactly those jobs. Does a star for a job that
has since closed still come back, which is most of the reason people
star one at all. And the one that would be silent if it broke: a saved
row lives in the alerts partition under a sentinel sort key, so the
alert list has to skip it, or a reader who stars forty jobs opens their
alerts page and finds forty things they never created.

Run directly, no framework:  python tests/test_saved_jobs.py
"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

os.environ.setdefault("ALERTS_TABLE", "test-alerts")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import handler  # noqa: E402
import job_filters  # noqa: E402
import profile  # noqa: E402
import saved  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# Ids as loader/load_to_sqlite.py actually writes them: an md5 hexdigest
# cut to 16 characters. Anything else in this file is deliberately not
# one.
JOBS = [
    ("0123456789abcdef", "Platform Engineer", None),
    ("fedcba9876543210", "Backend Engineer", None),
    ("00112233445566aa", "Frontend Engineer", None),
    # Starred in March, closed in April. The row is still there.
    ("aabbccddeeff0011", "Security Engineer", "2026-04-01T00:00:00Z"),
]

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
        url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
        last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT,
        salary_text TEXT, salary_is_estimate INT
    )
""")
for i, (jid, title, closed_at) in enumerate(JOBS):
    conn.execute(
        "INSERT INTO jobs (id, company_domain, ats, title, url, posted_at, confidence,"
        " first_seen, last_seen, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, "x.com", "greenhouse", title, "http://x", f"2026-09-{10 - i:02d}T00:00:00Z",
         "verified", "2026-09-01T00:00:00Z", "2026-09-12T00:00:00Z", closed_at),
    )
conn.commit()
job_filters.register_functions(conn)

# route_jobs opens its own connection from S3, and there is no companies
# table here, which is the deploy-skew case _has_company_column already
# degrades to NULL for.
handler.get_connection = lambda: conn


def run(params):
    return handler.route_jobs(params)


OPEN_IDS = [j[0] for j in JOBS if j[2] is None]

res = run({"ids": ",".join(OPEN_IDS[:2])})
ids = [j["id"] for j in res["jobs"]]
check("an ids request returns exactly those jobs", sorted(ids) == sorted(OPEN_IDS[:2]), repr(ids))
check("and the count agrees with the rows", res["total"] == 2, repr(res["total"]))

# These arrive from stale localStorage and shared links, which outlive
# any given id scheme. Narrowing is the right failure here, erroring is
# not.
mixed = run({"ids": OPEN_IDS[0] + ",NOT-AN-ID,../etc/passwd,00112233445566ZZ"})
check("a malformed id is dropped rather than fatal",
      [j["id"] for j in mixed["jobs"]] == [OPEN_IDS[0]], repr([j["id"] for j in mixed["jobs"]]))

unknown = run({"ids": OPEN_IDS[0] + "," + "9" * 16})
check("a well-formed id for a job we do not have just misses",
      [j["id"] for j in unknown["jobs"]] == [OPEN_IDS[0]], repr(unknown["total"]))

# The important half of dropping it: no valid id means no clause at all,
# not `id IN ()`, and certainly not an empty board.
everything = run({})["total"]
check("an ids param with nothing usable leaves the board unfiltered",
      run({"ids": "NOPE,,-1"})["total"] == everything, repr(run({"ids": "NOPE"})["total"]))
check("and so does an empty one", run({"ids": ""})["total"] == everything)

wanted = job_filters.wanted_ids({"ids": ",".join("%016x" % i for i in range(300))})
check("the list is capped at 200", len(wanted) == job_filters.MAX_IDS_FILTER, str(len(wanted)))

dupes = job_filters.wanted_ids({"ids": ",".join([OPEN_IDS[1], OPEN_IDS[0], OPEN_IDS[1]])})
check("a repeat is dropped and the order is kept",
      dupes == [OPEN_IDS[1], OPEN_IDS[0]], repr(dupes))

# Nothing is interpolated: ids reach SQLite as bound parameters, and one
# carrying SQL does not survive validation to begin with.
sneaky = run({"ids": "0123456789abcdef'); DROP TABLE jobs; --"})
check("an id carrying SQL is dropped, and the table is still there",
      sneaky["total"] == everything
      and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == len(JOBS))

# The whole point of storing stars server-side: you starred it, it
# closed, and you want to be told that rather than have it vanish.
closed_id = JOBS[3][0]
check("a saved job that has closed is hidden by default",
      closed_id not in [j["id"] for j in run({"ids": closed_id})["jobs"]])
reopened = run({"ids": closed_id, "include_closed": "1"})
check("but comes back when the Saved view asks for closed ones",
      [j["id"] for j in reopened["jobs"]] == [closed_id], repr(reopened["total"]))
check("and says when it closed",
      reopened["jobs"][0]["closed_at"] == "2026-04-01T00:00:00Z",
      repr(reopened["jobs"][0].get("closed_at")))


class FakeTable:
    """The alerts partition, holding all three kinds of row at once: a
    profile, a real alert, and whatever gets starred below.
    """

    def __init__(self):
        self.items = {
            profile.PROFILE_ID: {"user_id": "u", "alert_id": profile.PROFILE_ID, "skills": ["Python"]},
            "abc-123": {"user_id": "u", "alert_id": "abc-123", "filter": {"q": "devops"}, "active": True},
        }

    def query(self, **kw):
        return {"Items": list(self.items.values())}

    def put_item(self, Item=None):
        self.items[Item["alert_id"]] = Item

    def delete_item(self, Key=None):
        self.items.pop(Key["alert_id"], None)


table = FakeTable()
handler._alerts_table = table

check("a saved sort key cannot collide with a uuid4 alert id",
      saved.SAVED_PREFIX.startswith("#"), saved.SAVED_PREFIX)

handler.route_save_job("u", OPEN_IDS[0])
handler.route_save_job("u", OPEN_IDS[1])
got = handler.route_list_saved("u")["saved"]
check("a saved job round-trips", sorted(s["job_id"] for s in got) == sorted(OPEN_IDS[:2]), repr(got))
check("with the time it was saved", all(s["saved_at"] for s in got), repr(got))

# Stamped by hand rather than trusting two saves a microsecond apart to
# be distinguishable. The order is the claim being tested, not the
# clock's resolution.
table.items[saved.saved_id(OPEN_IDS[0])]["saved_at"] = "2026-03-01T00:00:00+00:00"
table.items[saved.saved_id(OPEN_IDS[1])]["saved_at"] = "2026-09-01T00:00:00+00:00"
check("newest first",
      [s["job_id"] for s in handler.route_list_saved("u")["saved"]] == [OPEN_IDS[1], OPEN_IDS[0]],
      repr([s["job_id"] for s in handler.route_list_saved("u")["saved"]]))

before = len(table.items)
handler.route_save_job("u", OPEN_IDS[0])
check("saving twice is not an error and leaves one row, not two",
      len(table.items) == before, "%d -> %d" % (before, len(table.items)))

handler.route_unsave_job("u", OPEN_IDS[0])
check("unsaving removes it",
      [s["job_id"] for s in handler.route_list_saved("u")["saved"]] == [OPEN_IDS[1]])
handler.route_unsave_job("u", OPEN_IDS[0])
check("and unsaving something that was never saved is fine",
      [s["job_id"] for s in handler.route_list_saved("u")["saved"]] == [OPEN_IDS[1]])


def rejects(fn, job_id):
    try:
        fn("u", job_id)
    except ValueError:
        return True
    return False


# A 400, not a row. The sort key is built from this string, so an
# unchecked one would park arbitrary text in the same partition as the
# alerts, under a key the alert list then has to make sense of.
check("a bad job id is rejected on save", rejects(handler.route_save_job, "#profile"))
check("so is one that is merely the wrong shape", rejects(handler.route_save_job, "abc"))
check("and an empty one", rejects(handler.route_save_job, ""))
check("delete checks it too", rejects(handler.route_unsave_job, "../../etc"))
check("nothing junk reached the table",
      all(k in ("abc-123", profile.PROFILE_ID) or saved.is_saved_id(k) for k in table.items),
      repr(sorted(table.items)))

# The silent one. Both sentinel rows share the partition the alert list
# queries, and neither is an alert.
listed = handler.route_list_alerts("u")["alerts"]
check("neither the profile nor a saved row is returned as an alert",
      [a["alert_id"] for a in listed] == ["abc-123"], repr([a["alert_id"] for a in listed]))

# An alert may be saved with a filter of ids, so the evaluator's own
# validation has to recognise the key.
check("ids is a filter key an alert may carry", "ids" in handler._ALLOWED_FILTER_KEYS)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
