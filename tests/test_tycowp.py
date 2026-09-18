"""The tyco-wp theme's jobs.json (probe.f_tycowp), first used for IAI.

The record shape is what jobs.iai.co.il's file held on 2026-09-18: the
theme's abbreviated keys, Hebrew throughout, a city with no country and
no date of any kind.

Run directly, no framework:  python tests/test_tycowp.py
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
        self.answers, self.gets = answers, []
        self.headers = {}

    def get(self, url, **kw):
        self.gets.append(url)
        return self.answers.get(url, Resp(404))


FILE = "https://jobs.iai.co.il/wp-content/themes/tyco-wp/assets/json/jobs.json"
ROWS = [
    {"id": "76050151", "tl": "ראש/ת מנהל תקשורת שיווקית ", "cd": "147227", "dc": "קצת על התפקיד\n\nלארגון <b>השיווק</b> דרוש/ה",
     "ct": "נתב\"ג", "tp": "משרה מלאה", "jc": "ניהול", "oc": [613], "pr": [43], "ht": False, "sn": False},
    {"id": "76046951", "tl": "סטודנט/ית לתוכנה", "cd": "147001", "dc": "", "ct": "אשדוד", "tp": "משרת סטודנט", "jc": "סטודנטים",
     "oc": [580], "pr": [46], "ht": True, "sn": False},
    {"id": "", "tl": "no id", "ct": "יהוד"},
    {"id": "1", "tl": "", "ct": "יהוד"},
]

for bad in ["iai", "iai.co.il/jobs", "", None, "jobs.iai.co.il/x"]:
    sess = Sess({FILE: Resp(200, ROWS)})
    check(f"a guessed token {bad!r} makes no request", probe.f_tycowp(sess, bad) is None and not sess.gets)

sess = Sess({FILE: Resp(200, ROWS)})
jobs = probe.f_tycowp(sess, "jobs.iai.co.il")
check("one request, for the theme's file", sess.gets == [FILE], repr(sess.gets))
check("reads the rows that have an id and a title", jobs is not None and [j.external_id for j in jobs] == ["76050151", "76046951"],
      repr(jobs and [j.external_id for j in jobs]))
j = jobs[0]
check("title is trimmed", j.title == "ראש/ת מנהל תקשורת שיווקית", repr(j.title))
check("the city is the location and the country is named", j.location == 'נתב"ג, Israel', repr(j.location))
check("the job page is the url", j.url == "https://jobs.iai.co.il/job/76050151", j.url)
check("no posting date is invented", j.posted_at is None)
check("the field is the department", j.department == "ניהול", repr(j.department))
check("the description is cleaned text", "השיווק" in (j.description or "") and "<b>" not in (j.description or ""), repr(j.description))
check("a student post reads as an internship", jobs[1].seniority == "intern", repr(jobs[1].seniority))
check("an empty description stays empty", jobs[1].description is None, repr(jobs[1].description))
check("classified as Israel", all(x.country == "IL" for x in probe._fill_classifications(jobs, "iai.co.il")))
check("a non-200 is no answer", probe.f_tycowp(Sess({FILE: Resp(500)}), "jobs.iai.co.il") is None)
check("a body that is not a list is no answer", probe.f_tycowp(Sess({FILE: Resp(200, {"jobs": ROWS})}), "jobs.iai.co.il") is None)
check("an empty list is an empty board", probe.f_tycowp(Sess({FILE: Resp(200, [])}), "jobs.iai.co.il") == [])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
