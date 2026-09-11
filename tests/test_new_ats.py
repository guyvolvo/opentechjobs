"""BambooHR, Breezy, and Workable's own directory as a discovery source.

The BambooHR guard is the whole reason this file exists. Any unknown
subdomain on BambooHR answers 200 with the same four-job demo tenant, so
google.bamboohr.com is indistinguishable from a real customer by status
code alone. Five of seven apparent hits across our unresolved domains
were that. This project has already shipped one false board for exactly
this class of bug (hibob.com on Workable, zero jobs), so the guard gets
a test rather than a comment.

Run directly, no framework:  python tests/test_new_ats.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import discover_companies  # noqa: E402
import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


class FakeSession:
    """Answers one canned payload, whatever is asked."""

    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass

    status_code = 200
    headers: dict = {}


def fetch(ats, token, payload):
    sess = FakeSession(payload)
    orig = probe.get_json
    probe.get_json = lambda s, url: payload
    try:
        return probe.FETCHERS[ats](sess, token)
    finally:
        probe.get_json = orig


DEMO = {"meta": {"totalCount": 4}, "result": [
    {"id": "15", "jobOpeningName": "IT Security Engineer", "departmentLabel": "IT",
     "location": {"city": "Mayfair", "state": "London, City of"}},
    {"id": "20", "jobOpeningName": "Software Engineer", "departmentLabel": "Product",
     "location": {"city": "Berlin", "state": "Brandenburg"}},
    {"id": "25", "jobOpeningName": "Sales Development Representative", "departmentLabel": "Sales",
     "location": {"city": "Berlin", "state": None}},
    {"id": "30", "jobOpeningName": "Marketing Manager", "departmentLabel": "Marketing",
     "location": {"city": "Dublin", "state": None}},
]}

REAL = {"meta": {"totalCount": 2}, "result": [
    {"id": "21", "jobOpeningName": "Mechanical Designer", "departmentLabel": "Hardware",
     "location": {"city": "Yehud", "state": "Merkaz"}, "isRemote": None},
    {"id": "22", "jobOpeningName": "RF Engineer", "departmentLabel": "Hardware",
     "location": {"city": "Yehud", "state": "Merkaz"}, "isRemote": True},
]}

check("BambooHR's demo tenant is not mistaken for a board",
      fetch("bamboohr", "google", DEMO) is None)

jobs = fetch("bamboohr", "vayyar", REAL)
check("a real BambooHR board comes through", jobs is not None and len(jobs) == 2,
      repr(jobs))
check("with location joined from city and state",
      jobs[0].location == "Yehud, Merkaz", jobs[0].location)
check("and a job URL built from the token and id",
      jobs[0].url == "https://vayyar.bamboohr.com/careers/21", jobs[0].url)
check("isRemote becomes a workplace type",
      jobs[1].workplace_type == "remote" and jobs[0].workplace_type is None)

# A real board that happens to post one job whose title matches the demo
# set must still count. The guard is "every title is a demo title", not
# "any title is".
PARTIAL = {"result": [
    {"id": "9", "jobOpeningName": "Software Engineer", "departmentLabel": "R&D",
     "location": {"city": "Tel Aviv", "state": None}},
    {"id": "10", "jobOpeningName": "Embedded Developer", "departmentLabel": "R&D",
     "location": {"city": "Tel Aviv", "state": None}},
]}
check("a real board sharing one title with the demo still counts",
      (fetch("bamboohr", "realco", PARTIAL) or []) and len(fetch("bamboohr", "realco", PARTIAL)) == 2)

check("an empty BambooHR response is not a board",
      fetch("bamboohr", "nobody", {"result": []}) is None)

BREEZY = [{
    "id": "1236787b2179", "name": "IT/IS Specialist",
    "url": "https://veev.breezy.hr/p/1236787b2179-it-is-specialist",
    "published_date": "2026-09-10T22:16:24.441Z", "department": "IT/IS",
    "location": {"country": {"name": "Israel", "id": "IL"},
                 "state": {"id": "TA", "name": "Tel Aviv"}, "city": "Tel Aviv"},
}]
jobs = fetch("breezy", "veev", BREEZY)
check("a Breezy board comes through", jobs is not None and len(jobs) == 1)
check("with city, state and country joined",
      jobs[0].location == "Tel Aviv, Tel Aviv, Israel", jobs[0].location)
check("and the published date normalized to UTC",
      (jobs[0].posted_at or "").startswith("2026-09-10T22:16:24"), repr(jobs[0].posted_at))
check("an empty Breezy response is not a board", fetch("breezy", "nobody", []) is None)

# The Workable directory hands back the employer's own website, so the
# domain is read rather than guessed.
slug = discover_companies._workable_slug
check("the account token is read out of the company URL",
      slug("https://jobs.workable.com/company/5X6uZ/jobs-at-humanz") == "humanz")
check("a hyphenated slug survives",
      slug("https://jobs.workable.com/company/tE2sn/jobs-at-tomax-think-academy")
      == "tomax-think-academy")
check("a URL with no slug returns nothing", slug("https://jobs.workable.com/") is None)
check("and so does an empty one", slug("") is None)

check("a company website becomes a bare domain",
      discover_companies._domain_of("https://www.humanz.com/") == "humanz.com")
check("a site with no www is unchanged",
      discover_companies._domain_of("http://tomax.io") == "tomax.io")
check("an empty website is an empty domain", discover_companies._domain_of("") == "")

# Niloosoft/Hunter. The token carries the board host and the action slug
# because neither is derivable from the other: Iscar's board is
# iscar-fr.hunterhrms.com and posts to actions-iscar-hamah.
NILOO = [
    {"jobId": 4658, "jobTitle": "Software Engineer", "openDate": "2026-09-06T04:50:00",
     "locationAddress": None, "area": "North", "status": 1,
     "description": "<b>Build things</b>", "requirements": "C++",
     "employerName": "a production line, not a department"},
    {"jobId": 4659, "jobTitle": "QA Engineer", "openDate": "2026-09-01T10:00:00",
     "locationAddress": "Tel Aviv", "area": "Center", "status": 1,
     "description": "", "requirements": "", "employerName": "Dror Mizrahi"},
    {"jobId": 4660, "jobTitle": "Closed Role", "openDate": "2025-01-01T10:00:00",
     "locationAddress": None, "area": "South", "status": 7,
     "description": "", "requirements": "", "employerName": ""},
]


def fetch_post(token, payload, ok=None):
    sess = FakeSession(payload)
    orig = probe.get_json_post
    probe.get_json_post = lambda s, url, body, ok_statuses=(200,), timeout=None: payload
    try:
        return probe.FETCHERS["niloosoft"](sess, token)
    finally:
        probe.get_json_post = orig


jobs = fetch_post("iscar-fr.hunterhrms.com:iscar-hamah", NILOO)
check("a Hunter board comes through", jobs is not None and len(jobs) == 2,
      repr(jobs and len(jobs)))
# Every location is tagged with the country. These boards carry Hebrew
# place names and the site's israel_only filter matches Latin keywords,
# so without this the jobs land and then cannot be found.
check("a job with no address falls back to its region",
      jobs[0].location == "North, Israel", jobs[0].location)
check("and a real address wins over the region",
      jobs[1].location == "Tel Aviv, Israel", jobs[1].location)
check("a job with neither still says Israel",
      (fetch_post("x.hunterhrms.com:x", [dict(NILOO[0], locationAddress=None, area="")])
       or [None])[0].location == "Israel")
check("anything not status 1 is dropped",
      all(j.title != "Closed Role" for j in jobs))
check("the job URL is built from the board host, not the API host",
      jobs[0].url == "https://iscar-fr.hunterhrms.com/?jobId=4658", jobs[0].url)
check("employerName is not treated as a department",
      all(j.department is None for j in jobs))
check("description and requirements are joined",
      "Build things" in (jobs[0].description or "") and "C++" in (jobs[0].description or ""),
      repr(jobs[0].description))

check("a token with no slug is refused outright",
      fetch_post("iscar-fr.hunterhrms.com", NILOO) is None)
check("an empty board is not a board",
      fetch_post("x.hunterhrms.com:x", []) is None)
check("a board where every job is closed is not a board",
      fetch_post("x.hunterhrms.com:x", [NILOO[2]]) is None)

# companies.yml pins, and the YAML trap underneath them. Every pin value
# is an opaque id, but YAML 1.1 reads a bare `yes` as the boolean true,
# which turned yes.co.il's board into a request for /actions-True. It
# 404'd, resolve() fell through to guessing, and landed on a Greenhouse
# board belonging to an electrical contractor in North Dakota.
import textwrap  # noqa: E402
import tempfile  # noqa: E402

pins_yaml = textwrap.dedent("""
    niloosoft:
      - domain: yes.co.il
        host: yes-fbf.hunterhrms.com
        slug: yes
        jobs_seen: 13
      - domain: example.com
        host: e.hunterhrms.com
        slug: 01
        jobs_seen: 2
""")
with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8") as f:
    f.write(pins_yaml)
    pins_path = Path(f.name)

pins = probe.load_pins(pins_path)
check("an unquoted yes stays the string it was written as",
      pins["niloosoft"]["yes.co.il"]["slug"] == "yes",
      repr(pins["niloosoft"]["yes.co.il"]["slug"]))
check("and a leading-zero slug keeps its zero",
      pins["niloosoft"]["example.com"]["slug"] == "01",
      repr(pins["niloosoft"]["example.com"]["slug"]))
check("jobs_seen stays a number",
      pins["niloosoft"]["yes.co.il"]["jobs_seen"] == 13)
pins_path.unlink()

# The real file has to survive the same trap.
real = probe.load_pins()
check("every pinned Hunter board has a string host and slug",
      all(isinstance(p["host"], str) and isinstance(p["slug"], str)
          for p in real.get("niloosoft", {}).values()),
      repr(real.get("niloosoft")))
check("and yes.co.il is one of them",
      real.get("niloosoft", {}).get("yes.co.il", {}).get("slug") == "yes")

check("the North Dakota electrician is blacklisted",
      ("greenhouse", "yes") in probe.KNOWN_FALSE_POSITIVES)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
