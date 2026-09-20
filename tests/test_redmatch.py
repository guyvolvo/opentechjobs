"""RedMatch boards (probe.f_redmatch), first used for Clalit.

The record shape is what jobs.clalitapps.co.il answered on 2026-09-18:
559 positions in one POST, Hebrew titles and cities, an activation
date, and an apply page addressed by compPositionID.

Run directly, no framework:  python tests/test_redmatch.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return json.loads(self.text)


class Sess:
    def __init__(self, answers):
        self.answers, self.posts, self.gets = answers, [], []
        self.headers = {}

    def post(self, url, **kw):
        self.posts.append((url, kw.get("json")))
        return self.answers.get(url, Resp(404))

    def get(self, url, **kw):
        self.gets.append(url)
        return Resp(404)


def position(pid, title, city, region="מרכז", date="2026-09-17T13:57:13.123", field="אחים ואחיות", active=True):
    return {"compPositionID": pid, "jobTitleText": title, "activationDate": date,
            "description": "<p>למרפאה <strong>יועצת</strong> דרוש/ה</p><ul><li>משרה מלאה</li></ul>",
            "shortDescription": "למרפאה יועצת", "location": region, "displayLocation": city,
            "fieldDesc": field, "isActivePosition": active, "affiliateDisplayName": "מחוז מרכז"}


TOKEN = "jobs.clalitapps.co.il/clalit:9E6C0368-A39E-4D83-803E-CF2AF0BA28DD"
API = "https://jobs.clalitapps.co.il/CandidateAPI/api//position/Search/9E6C0368-A39E-4D83-803E-CF2AF0BA28DD"
ANSWER = [position(50242, "אח/ות מוסמך/ת ", "ראשון לציון"),
          position(49365, "רוקח/ת", None, region="דרום", field="רוקחות"),
          position(11111, "משרה שנסגרה", "חיפה", active=False)]

for bad in ["clalit", "clalit.co.il", "jobs.clalitapps.co.il", "", None, ":guid", "host:"]:
    sess = Sess({API: Resp(200, ANSWER)})
    check(f"a guessed token {bad!r} makes no request", probe.f_redmatch(sess, bad) is None and not sess.posts)

sess = Sess({API: Resp(200, ANSWER)})
jobs = probe.f_redmatch(sess, TOKEN)
check("one POST, to the affiliate's search route, asking for everything",
      len(sess.posts) == 1 and sess.posts[0][0] == API and sess.posts[0][1] == probe.REDMATCH_SEARCH_BODY,
      repr(sess.posts))
check("reads the open positions and skips the inactive one",
      jobs is not None and [j.external_id for j in jobs] == ["50242", "49365"],
      repr(jobs and [j.external_id for j in jobs]))
j = jobs[0]
check("title is trimmed", j.title == "אח/ות מוסמך/ת", repr(j.title))
check("the city is the location, and the country is named",
      j.location == "ראשון לציון, Israel", repr(j.location))
check("a position with no city falls back to its region",
      jobs[1].location == "דרום, Israel", repr(jobs[1].location))
check("the apply page is the url",
      j.url == "https://jobs.clalitapps.co.il/clalit/redmatch-apply/redmatch.apply.html?compPositionID=50242", j.url)
check("activation date is the posting date, read as Israel time and stored in UTC",
      j.posted_at == "2026-09-17T10:57:13+00:00", repr(j.posted_at))
conv = probe.israel_local_to_utc
check("winter is two hours behind, summer three, and the change falls on Israel's own dates",
      conv("2026-01-10T09:00:00") == "2026-01-10T07:00:00+00:00"
      and conv("2026-03-27T01:59:59") == "2026-03-26T23:59:59+00:00"   # last hour of winter time
      and conv("2026-03-27T03:00:00") == "2026-03-27T00:00:00+00:00"   # first hour of summer time
      and conv("2026-10-25T01:00:00") == "2026-10-24T22:00:00+00:00"   # last hour of summer time
      and conv("2026-10-25T03:00:00") == "2026-10-25T01:00:00+00:00",  # back on winter time
      repr([conv(x) for x in ("2026-03-27T01:59:59", "2026-03-27T03:00:00", "2026-10-25T01:00:00", "2026-10-25T03:00:00")]))
check("a stamp that already has an offset, or is not a date, is left alone",
      conv("2026-09-17T13:57:13+00:00") == "2026-09-17T13:57:13+00:00" and conv("soon") == "soon" and conv("") == "")

# Rows written before the conversion existed are repaired by the loader's
# migration, which every merge runs. Other platforms' bare stamps are not
# its business.
import sqlite3, tempfile  # noqa: E402
sys.path.insert(0, str(ROOT / "loader"))
import load_to_sqlite as lts  # noqa: E402
conn = sqlite3.connect(Path(tempfile.mkdtemp()) / "jobs.db")
conn.row_factory = sqlite3.Row
conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
conn.execute("INSERT INTO companies (domain, ats, first_seen, last_checked) VALUES ('clalit.co.il','redmatch','2026-09-18','2026-09-20'), ('other.com','greenhouse','2026-09-18','2026-09-20')")
conn.execute("INSERT INTO jobs (id, company_domain, ats, title, url, posted_at, first_seen, last_seen, confidence) VALUES"
             " ('r1','clalit.co.il','redmatch','x','u','2026-09-20T08:56:47.187','2026-09-18','2026-09-20','verified'),"
             " ('r2','clalit.co.il','redmatch','y','u','2026-09-17T10:57:13+00:00','2026-09-18','2026-09-20','verified'),"
             " ('g1','other.com','greenhouse','z','u','2026-09-20T08:56:47.187','2026-09-18','2026-09-20','verified')")
lts._migrate(conn)
after = {r["id"]: r["posted_at"] for r in conn.execute("SELECT id, posted_at FROM jobs")}
check("the migration converts bare RedMatch stamps and leaves converted ones and other platforms alone",
      after == {"r1": "2026-09-20T05:56:47+00:00", "r2": "2026-09-17T10:57:13+00:00", "g1": "2026-09-20T08:56:47.187"}, repr(after))
check("the professional field is the department", j.department == "אחים ואחיות", repr(j.department))
check("classified as Israel", all(x.country == "IL" for x in probe._fill_classifications(jobs, "clalit.co.il")))
check("description is the cleaned text", "יועצת" in (j.description or "") and "<" not in (j.description or ""),
      repr(j.description)[:80])

# The API answering anything but a list of positions is no answer, not
# an empty board: an empty board would close every listing.
check("a non-200 is no answer", probe.f_redmatch(Sess({API: Resp(500, {"error": "x"})}), TOKEN) is None)
check("a malformed body is no answer", probe.f_redmatch(Sess({API: Resp(200, {"unexpected": 1})}), TOKEN) is None)
check("an empty list is an empty board", probe.f_redmatch(Sess({API: Resp(200, [])}), TOKEN) == [])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
