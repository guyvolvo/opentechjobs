"""Microsoft, Google and Apple from their own careers sites, Lever's EU
host, and the pins for companies guessing could not reach.

The guard comes first, for the reason tests/test_amazon.py gives: every
fetcher in FETCHERS is called with every guessed token, so a fetcher for
one company's site must refuse anything that is not its pinned country
code, before it makes a request.

Run directly, no framework:  python tests/test_big_tech.py
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
    def __init__(self, status=200, text="", body=None, headers=None):
        self.status_code, self.text, self._body, self.headers = status, text, body, headers or {}

    def json(self):
        return self._body


class Sess:
    """Records every request; answers GETs and POSTs from queues."""

    def __init__(self, gets=(), posts=()):
        self.gets, self.posts, self.calls = list(gets), list(posts), []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.gets.pop(0) if self.gets else Resp(404)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.posts.pop(0) if self.posts else Resp(404)


def with_get_json(answer, fn):
    urls = []
    orig = probe.get_json

    def fake(sess, url):
        urls.append(url)
        return answer(url)
    probe.get_json = fake
    try:
        return fn(), urls
    finally:
        probe.get_json = orig


# The guard.
for name, fn in [("microsoft", probe.f_microsoft), ("google", probe.f_google), ("apple", probe.f_apple)]:
    for bad in ["microsoft", "google", "apple", "isr", "IL", "ISRX", "", "monday"]:
        sess = Sess()
        jobs, urls = with_get_json(lambda u: {"data": {"positions": [], "count": 0}}, lambda: fn(sess, bad))
        check(f"{name}: a guessed token {bad!r} makes no request",
              jobs is None and not urls and not sess.calls, repr((urls, sess.calls)))

# Microsoft.
MS_ROWS = [
    {"id": 1, "name": "Principal Silicon Engineer", "department": "Silicon Engineering",
     "locations": ["Israel, Multiple Locations, Multiple Locations", "Israel, Tel Aviv, Herzliya"],
     "standardizedLocations": ["IL", "Herzliya, Tel Aviv District, IL"], "postedTs": 1788258419,
     "positionUrl": "/careers/job/1", "workLocationOption": "onsite"},
    {"id": 2, "name": "Somewhere Else", "locations": ["United States, Washington, Redmond"],
     "standardizedLocations": ["Redmond, WA, US"], "postedTs": 1788258419, "positionUrl": "/careers/job/2"},
    {"id": 3, "name": "Undecided City", "locations": ["Israel, Multiple Locations, Multiple Locations"],
     "standardizedLocations": ["IL"], "postedTs": 1788258419, "positionUrl": "/careers/job/3"},
]


def ms_answer(url):
    if "position_details" in url:
        return {"data": {"jobDescription": "<p>Kubernetes and <b>C++</b></p>"}}
    start = int(url.rsplit("start=", 1)[1])
    return {"data": {"count": 12, "positions": (MS_ROWS * 4)[start:start + 10]}}


probe.FETCH_FULL_DESCRIPTIONS = True
jobs, urls = with_get_json(ms_answer, lambda: probe.f_microsoft(None, "ISR"))
first = jobs[0]
check("microsoft: pages until count is reached",
      [u for u in urls if "search" in u][-1].endswith("start=10"), repr(urls))
check("microsoft: roles outside the country are dropped", all(j.external_id != "2" for j in jobs))
check("microsoft: the place reads city first", first.location == "Herzliya, Tel Aviv, Israel", first.location)
check("microsoft: an undecided city still counts as the country",
      any(j.external_id == "3" and j.location == "Israel" for j in jobs), repr([(j.external_id, j.location) for j in jobs]))
check("microsoft: the url is absolute", first.url == "https://apply.careers.microsoft.com/careers/job/1", first.url)
check("microsoft: the date is UTC", first.posted_at and first.posted_at.endswith("+00:00"), repr(first.posted_at))
check("microsoft: description comes from the detail call", "Kubernetes" in (first.description or ""), repr(first.description))
check("microsoft: workplace maps across", first.workplace_type == "onsite", repr(first.workplace_type))
probe.FETCH_FULL_DESCRIPTIONS = False
jobs, urls = with_get_json(ms_answer, lambda: probe.f_microsoft(None, "ISR"))
check("microsoft: descriptions come on the fast path too, which is the only one the board sees",
      all(j.description for j in jobs), repr([j.description for j in jobs]))
jobs, _ = with_get_json(lambda u: None, lambda: probe.f_microsoft(None, "ISR"))
check("microsoft: nothing on page one is no board", jobs is None)


# Google.
def g_row(jid, title, places):
    row = [jid, title, "https://signin", [None, "<ul><li>Evaluate PDN</li></ul>"],
           [None, "<h3>Minimum</h3><ul><li>Python</li></ul>"], "projects/x", None, "Google", "en-US",
           places, [None, "<p>About the TPU team</p>"], [2, 3], [1787231375, 8000000]]
    return row


IL1 = ["Tel Aviv, Israel", ["Yigal Alon St 98"], "Tel Aviv-Yafo", None, "Tel Aviv District", "IL"]
IL2 = ["Haifa, Israel", ["Haifa"], "Haifa", None, "Haifa District", "IL"]
US = ["Mountain View, CA, USA", ["x"], "Mountain View", None, "CA", "US"]


def g_page(rows, total):
    data = json.dumps([rows, None, total, 20])
    return Resp(200, text=f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{data}, sideChannel: {{}}}});</script>")


sess = Sess(gets=[g_page([g_row("11", "Signal/Power Integrity Engineer, PhD Graduate", [IL1, IL2]),
                          g_row("12", "US Only", [US])], 22),
                  g_page([g_row("13", "Second Page", [IL1]), g_row("11", "Repeat", [IL1])], 22)])
jobs = probe.f_google(sess, "ISR")
ids = [j.external_id for j in jobs]
check("google: reads every page up to the total", len(sess.calls) == 2 and "page=2" in sess.calls[1][1], repr(sess.calls))
check("google: roles outside the country are dropped, repeats too", ids == ["11", "13"], repr(ids))
g = jobs[0]
check("google: every place in the country is kept", g.location == "Tel Aviv, Israel; Haifa, Israel", g.location)
check("google: the slug matches Google's own links",
      g.url.endswith("/11-signalpower-integrity-engineer-phd-graduate"), g.url)
check("google: description joins about, responsibilities and qualifications",
      all(k in (g.description or "") for k in ("TPU", "PDN", "Python")), repr(g.description))
check("google: a browser user agent is sent", "Mozilla" in sess.calls[0][2]["headers"]["User-Agent"])
check("google: a page with no data is no board", probe.f_google(Sess(gets=[Resp(200, text="<html>")]), "ISR") is None)

# Apple.
A_ROWS = [
    {"positionId": "200611225", "postingTitle": "Embedded FW Engineer", "transformedPostingTitle": "embedded-fw-engineer",
     "postDateInGMT": "2026-07-19T10:34:25.928Z", "team": {"teamName": "Software and Services"},
     "jobSummary": "Firmware on Apple SoCs.", "locations": [{"name": "Herzliya", "countryName": "Israel"}]},
    {"positionId": "1", "postingTitle": "Cupertino Role", "transformedPostingTitle": "c",
     "locations": [{"name": "Cupertino", "countryName": "United States"}]},
]
csrf = Resp(200, headers={"X-Apple-CSRF-Token": "tok"})
sess = Sess(gets=[csrf], posts=[Resp(200, body={"res": {"searchResults": A_ROWS, "totalRecords": 2}})])
jobs = probe.f_apple(sess, "ISR")
post = [c for c in sess.calls if c[0] == "POST"][0]
check("apple: the CSRF token is sent with the search", post[2]["headers"].get("X-Apple-CSRF-Token") == "tok", repr(post[2]["headers"]))
check("apple: the format block is sent, or results come back empty", "format" in post[2]["json"], repr(post[2]["json"]))
check("apple: filtered to the country's post location", post[2]["json"]["filters"]["locations"] == ["postLocation-ISR"])
check("apple: roles outside the country are dropped", [j.external_id for j in jobs] == ["200611225"], repr(jobs))
a = jobs[0]
check("apple: place, url, date and team", a.location == "Herzliya, Israel"
      and a.url == "https://jobs.apple.com/en-il/details/200611225/embedded-fw-engineer"
      and a.posted_at.startswith("2026-07-19") and a.department == "Software and Services", repr(a))
check("apple: no CSRF token means no board", probe.f_apple(Sess(gets=[Resp(200)]), "ISR") is None)
check("apple: a failed detail call falls back to the summary", a.description == "Firmware on Apple SoCs.", repr(a.description))
detail = Resp(200, body={"res": {"jobSummary": "About Apple.", "description": "Build C++ tools.",
                                 "responsibilities": "Design backend features.",
                                 "minimumQualifications": "2+ years of Python.", "preferredQualifications": ""}})
sess = Sess(gets=[csrf, detail], posts=[Resp(200, body={"res": {"searchResults": A_ROWS[:1], "totalRecords": 1}})])
full = probe.f_apple(sess, "ISR")[0].description or ""
check("apple: the description is the whole posting, not the summary",
      all(k in full for k in ("About Apple.", "Build C++ tools.", "Responsibilities", "Design backend features.",
                              "Minimum qualifications", "2+ years of Python.")), repr(full))
check("apple: an empty section adds no heading", "Preferred qualifications" not in full, repr(full))

# Lever's EU host.
jobs, urls = with_get_json(lambda u: [] if "api.eu.lever.co" in u else {"ok": False, "error": "Document not found"},
                           lambda: probe.f_lever(None, "mobileye"))
check("lever: a miss on the US host tries the EU one", jobs == [] and "api.eu.lever.co" in urls[-1], repr(urls))
jobs, urls = with_get_json(lambda u: [], lambda: probe.f_lever(None, "palantir"))
check("lever: a hit on the US host asks nothing more", len(urls) == 1, repr(urls))

# Global reads.
MS_WORLD = [dict(MS_ROWS[0]), dict(MS_ROWS[1])]


def ms_world(url):
    if "position_details" in url:
        return {"data": {"jobDescription": "<p>Kubernetes</p>"}}
    start = int(url.rsplit("start=", 1)[1])
    return {"data": {"count": 25, "positions": ([dict(MS_ROWS[0], id=100 + i) for i in range(25)])[start:start + 10]}}


jobs, urls = with_get_json(ms_world, lambda: probe.f_microsoft(None, "ALL"))
check("microsoft ALL: no location filter, every page read",
      all("location=&" in u for u in urls if "search" in u) and len(jobs) == 25, repr((len(jobs), urls[:2])))
check("microsoft ALL: with no budget given, no description calls at all",
      not any("position_details" in u for u in urls), repr([u for u in urls if "position_details" in u][:2]))
jobs, urls = with_get_json(ms_world, lambda: probe.f_microsoft(None, "ALL", known_ids={"100", "101"}, detail_budget=5))
detail_ids = sorted(u.split("position_id=")[1].split("&")[0] for u in urls if "position_details" in u)
check("microsoft ALL: descriptions only for jobs not already known, up to the budget",
      detail_ids == ["102", "103", "104", "105", "106"], repr(detail_ids))
world = [dict(MS_ROWS[1], id=7)]
jobs, _ = with_get_json(lambda u: {"data": {"count": 1, "positions": world}}, lambda: probe.f_microsoft(None, "ALL"))
check("microsoft ALL: a role outside Israel is kept, place read city first",
      [j.location for j in jobs] == ["Redmond, Washington, United States"], repr([j.location for j in jobs]))
probe.time.sleep = lambda s: None
jobs, _ = with_get_json(lambda u: None if "start=10" in u else ms_world(u), lambda: probe.f_microsoft(None, "ALL"))
check("microsoft ALL: a page that keeps failing fails the read", jobs is None)

sess = Sess(gets=[g_page([g_row("11", "Tel Aviv Role", [IL1]), g_row("12", "US Only", [US])], 2)])
jobs = probe.f_google(sess, "ALL")
check("google ALL: no location in the URL, roles everywhere kept",
      "location=" not in sess.calls[0][1] and [j.external_id for j in jobs] == ["11", "12"], repr((sess.calls[0][1], jobs)))
sess = Sess(gets=[g_page([g_row("11", "Known", [IL1]), g_row("12", "New", [US])], 2)])
jobs = probe.f_google(sess, "ALL", known_ids={"11"})
check("google ALL: a known job comes without its description",
      jobs[0].description is None and jobs[1].description, repr([j.description for j in jobs]))

A_WORLD = [dict(A_ROWS[0]), dict(A_ROWS[1])]
sess = Sess(gets=[csrf], posts=[Resp(200, body={"res": {"searchResults": A_WORLD, "totalRecords": 2}})])
jobs = probe.f_apple(sess, "ALL", detail_budget=0)
post = [c for c in sess.calls if c[0] == "POST"][0]
check("apple ALL: no location filter, roles everywhere kept",
      post[2]["json"]["filters"] == {} and sorted(j.external_id for j in jobs) == ["1", "200611225"], repr(post[2]["json"]))
check("apple ALL: Cupertino reads as Cupertino, United States",
      any(j.location == "Cupertino, United States" for j in jobs), repr([j.location for j in jobs]))
check("apple ALL: with no budget left, no summary is stored in place of the posting",
      all(j.description is None for j in jobs), repr([j.description for j in jobs]))

check("the fast sweep leaves every big-tech board to the hourly Lambda",
      {"workday", "amazon", "microsoft", "google", "apple"} <= probe.SLOW_BOARD_ATS)

# Lever's whole posting.
posting = {"id": "p1", "text": "Senior SOTIF Analyst", "categories": {"location": "Jerusalem, Israel", "team": "Software"},
           "hostedUrl": "https://jobs.eu.lever.co/mobileye/p1", "createdAt": 1788258419000,
           "descriptionPlain": "Mobileye is looking for an analyst.",
           "lists": [{"text": "What will your job look like:", "content": "<li>Define safety KPIs</li>"},
                     {"text": "All you need is:", "content": "<li>5 years in functional safety</li>"}],
           "additionalPlain": "Equal opportunity employer."}
jobs, _ = with_get_json(lambda u: [posting], lambda: probe.f_lever(None, "mobileye"))
body = jobs[0].description or ""
check("lever: the description carries the lists and the closing text, not just the intro",
      all(k in body for k in ("looking for an analyst", "What will your job look like:", "Define safety KPIs",
                              "All you need is:", "functional safety", "Equal opportunity")), repr(body))
check("lever: description_chars counts the whole of it", jobs[0].description_chars == len(body))

# Registration and pins.
for name in ("microsoft", "google", "apple"):
    check(f"{name} is a registered fetcher", probe.FETCHERS.get(name) is getattr(probe, f"f_{name}"))
pins = probe.load_pins()
expect = {
    ("microsoft", "microsoft.com"): "ALL", ("google", "google.com"): "ALL", ("apple", "apple.com"): "ALL",
    ("lever", "mobileye.com"): "mobileye", ("ashby", "monday.com"): "monday.com",
    ("greenhouse", "navan.com"): "tripactions",
}
for (ats, domain), token in expect.items():
    got = pins.get(ats, {}).get(domain, {}).get("token")
    check(f"{domain} is pinned to {ats}:{token}", got == token, repr(got))
snyk = pins.get("workday", {}).get("snyk.io", {})
check("snyk.io is pinned to its Workday board",
      (snyk.get("tenant"), snyk.get("wd"), snyk.get("site")) == ("snyk", "wd103", "External"), repr(snyk))
domains = set((ROOT / "domains.txt").read_text(encoding="utf-8").split())
check("every pinned domain is in the sweep", {d for _, d in expect} | {"snyk.io"} <= domains)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
