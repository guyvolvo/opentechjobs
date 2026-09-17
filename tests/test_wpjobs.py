"""WP Job Openings sites (probe.f_wpjobs), first used for NSO Group.

The markup is trimmed from nsogroup.com/jobs/ and one of its role pages
on 2026-09-17.

Run directly, no framework:  python tests/test_wpjobs.py
"""

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
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class Sess:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.pages.get(url, Resp(404))


BASE = "https://www.nsogroup.com"


def card(slug, title, category):
    return (f'<div class="awsm-job-listing-item awsm-list-item" id="awsm-list-item-1"><div class="awsm-job-item">'
            f'<div class="awsm-list-left-col"><h2 class="awsm-job-post-title"> '
            f'<a href="{BASE}/job/{slug}/">{title}</a></h2></div>'
            f'<div class="awsm-job-specification-item awsm-job-specification-job-category">'
            f'<span class="awsm-job-specification-term">{category}</span></div></div></div>')


def role_page(title, date, address='"Israel"'):
    return ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
            '{"@type":"WebPage"},'
            f'{{"@type":"JobPosting","title":"{title}","datePosted":"{date}",'
            f'"description":"<p>Build things.</p><ul><li>Ship</li></ul>",'
            f'"jobLocation":{{"@type":"Place","address":{address}}}}}]}}</script>')


LISTING = ('<div class="awsm-job-listings awsm-lists" data-listings="30">'
           + card("ai-engineer", "AI Engineer", "Research &amp; Development")
           + card("hr-partner", "HR Partner", "People") + "</div>")
PAGES = {
    f"{BASE}/jobs/": Resp(200, LISTING),
    f"{BASE}/job/ai-engineer/": Resp(200, role_page("AI Engineer", "2026-08-24T09:35:11+03:00")),
    f"{BASE}/job/hr-partner/": Resp(200, role_page("HR Partner", "2026-09-01T10:00:00+03:00")),
}

for bad in ["nsogroup", "nso", "", None, "jobs/"]:
    sess = Sess(PAGES)
    check(f"a guessed token {bad!r} makes no request", probe.f_wpjobs(sess, bad) is None and not sess.calls)

jobs = probe.f_wpjobs(Sess(PAGES), "www.nsogroup.com/jobs/")
check("reads every role", jobs is not None and [j.external_id for j in jobs] == ["ai-engineer", "hr-partner"],
      repr(jobs and [j.external_id for j in jobs]))
j = jobs[0]
check("title", j.title == "AI Engineer", j.title)
check("a plain-text address is the location", j.location == "Israel", repr(j.location))
check("the date comes from the role page", (j.posted_at or "").startswith("2026-08-24"), repr(j.posted_at))
check("department comes from the listing card, unescaped", j.department == "Research & Development",
      repr(j.department))
check("the link is the role page", j.url == f"{BASE}/job/ai-engineer/", j.url)
check("the full text is read", "Build things." in (j.description or "") and j.description_chars > 0,
      repr(j.description))
check("the country resolves", probe._fill_classifications(jobs, "nsogroup.com")[0].country == "IL")

more = dict(PAGES)
more[f"{BASE}/jobs/"] = Resp(200, LISTING + '<div class="awsm-load-more-main"><a class="awsm-load-more">More</a></div>')
check("a load-more button is a partial read, refused",
      probe.f_wpjobs(Sess(more), "www.nsogroup.com/jobs/") is None)

broken = dict(PAGES)
broken[f"{BASE}/job/hr-partner/"] = Resp(500)
check("one role page failing fails the read", probe.f_wpjobs(Sess(broken), "www.nsogroup.com/jobs/") is None)

check("a 403 listing fails the read",
      probe.f_wpjobs(Sess({f"{BASE}/jobs/": Resp(403)}), "www.nsogroup.com/jobs/") is None)
check("an empty listing fails the read",
      probe.f_wpjobs(Sess({f"{BASE}/jobs/": Resp(200, "<div></div>")}), "www.nsogroup.com/jobs/") is None)

check("registered as a fetcher", probe.FETCHERS.get("wpjobs") is probe.f_wpjobs)
check("polls hourly, not in the sweep", "wpjobs" in probe.SLOW_BOARD_ATS)
jobs = probe.f_wpjobs(Sess(PAGES), "www.nsogroup.com/jobs/", known_ids={"ai-engineer"}, description_budget=5)
check("takes the hourly Lambda's keywords", jobs is not None and len(jobs) == 2)
import os  # noqa: E402
os.environ.setdefault("DATA_BUCKET", "unused-in-this-test")
import scrape_workday_handler  # noqa: E402
check("the hourly Lambda polls it", "wpjobs" in scrape_workday_handler.BIG_TECH_ATS)
pin = probe.load_pins().get("wpjobs", {}).get("nsogroup.com", {})
check("nsogroup.com is pinned", pin.get("token") == "www.nsogroup.com/jobs/", repr(pin))
check("nsogroup.com is in the sweep",
      "nsogroup.com" in (ROOT / "domains.txt").read_text(encoding="utf-8").split())

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
