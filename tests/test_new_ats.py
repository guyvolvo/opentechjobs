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

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
