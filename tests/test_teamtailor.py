"""Teamtailor boards (probe.f_teamtailor), after the HTML scrape died.

The scrape matched on `<div class="mt-1 text-md">`. Teamtailor shipped a
CSS refactor that flipped the class order and changed the margin to
`text-md mt-4`, and the regex stopped matching. Confirmed live
2026-09-22 against the two tenants the old code comment named: cigames
has 6 open jobs, sessions has 4, and the fetcher returned [] for both.

That is the part worth a test. [] is not "no answer", it is "this board
has nothing open", so the loader closed every Teamtailor listing we had
and nothing raised. A board that cannot be read has to come back None.

The replacement reads the JSON Feed at /jobs.json, which carries a
published date and the full description that the HTML list never had.
The location is still only on the jobs page, so that page is read once
per board, best-effort: if it fails the jobs still land without a
location rather than the board disappearing.

Run directly, no framework:  python tests/test_teamtailor.py
"""

import json
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


TOKEN = "cigames"
BASE = "https://cigames.teamtailor.com"
JOB_URL = BASE + "/jobs/8413283-senior-technical-animator-freelance"


def item(url=JOB_URL, title="Senior Technical Animator - Freelance",
         date="2026-09-21T08:35:44+01:00", body="<p>Join CI Games.</p><h2>Key Responsibilities:</h2>"
                                                "<ul><li>Unreal Engine animation systems</li></ul>"):
    return {"id": "dbaa83ad-a84e-47e2-a702-f7283a95579a", "title": title, "url": url,
            "date_published": date, "content_html": body}


# The markup as it is served today: class order flipped, margin changed.
# The old regex wanted class="mt-1 text-md" and this is class="text-md mt-4".
JOBS_HTML = """
<ul id="jobs_list_container" class="company-links"><li class="w-full">
  <div class="relative flex flex-col items-center py-24 text-center">
    <a class="@sm:line-clamp-2 flex" data-turbo="false" href="%s">
      <span class="absolute inset-0"></span>
      Senior Technical Animator - Freelance
</a>
    <div class="text-md mt-4">
<span>Animation</span>
  <span class="mx-[2px]">&middot;</span>
    <span>Europe</span>
    <span class="mx-[2px]">&middot;</span>
  <span class="inline-flex items-center gap-x-8">
    Fully Remote
</span></div>
</div>
</li></ul>
""" % JOB_URL


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = {"Content-Type": "application/json" if payload is not None else "text/html"}
        self.url = BASE + "/jobs"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Sess:
    """Answers /jobs.json and /jobs separately. feed=None makes the feed
    unreadable; page_status drives the best-effort location read."""

    def __init__(self, feed=None, page=JOBS_HTML, feed_status=200, page_status=200):
        self.feed, self.page = feed, page
        self.feed_status, self.page_status = feed_status, page_status
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if url.endswith("/jobs.json"):
            return Resp(self.feed_status, self.feed, "")
        return Resp(self.page_status, None, self.page)


print("-- the feed --")
sess = Sess({"version": "https://jsonfeed.org/version/1", "title": "CI Games",
             "items": [item()]})
jobs = probe.f_teamtailor(sess, TOKEN)
check("reads the board", jobs is not None and len(jobs) == 1, repr(jobs))
j = jobs[0]
check("keyed on the numeric id from the url, not the feed's uuid",
      j.external_id == "8413283", j.external_id)
check("title", j.title == "Senior Technical Animator - Freelance", j.title)
check("url", j.url == JOB_URL, j.url)
check("a real published date, which the HTML list never carried",
      (j.posted_at or "").startswith("2026-09-21"), repr(j.posted_at))
check("the description comes with it, which the HTML list never carried",
      "Unreal Engine animation systems" in (j.description or ""), repr(j.description))
check("and the headings survive _clean_text", "## Key Responsibilities:" in (j.description or ""))
check("description_chars counts what is stored", j.description_chars == len(j.description or ""))

print()
print("-- the location, read off the jobs page --")
check("segments joined rather than split into fields that would be wrong",
      j.location == "Animation, Europe, Fully Remote", repr(j.location))
check("so the workplace type still resolves",
      probe._fill_classifications(jobs, "cigames.com")[0].workplace_type == "remote")
check("both pages are fetched, feed first",
      sess.urls == [BASE + "/jobs.json", BASE + "/jobs"], repr(sess.urls))

print()
print("-- what the dead regex did, and must not do again --")
# The whole point. A board that cannot be read is None, never [], or the
# loader reads it as "nothing open here" and closes real listings.
check("an unreadable feed is None, not an empty board",
      probe.f_teamtailor(Sess(None, feed_status=404), TOKEN) is None)
check("and so is a 200 that is not a feed",
      probe.f_teamtailor(Sess({"jobs": []}), TOKEN) is None)
check("a genuinely empty feed is [], which is a real answer",
      probe.f_teamtailor(Sess({"items": []}), TOKEN) == [])

print()
print("-- the jobs page is best-effort --")
no_page = Sess({"items": [item()]}, page_status=500)
out = probe.f_teamtailor(no_page, TOKEN)
check("a jobs page that fails does not lose the jobs", len(out or []) == 1, repr(out))
check("they just have no location", out[0].location == "", repr(out[0].location))
restyled = Sess({"items": [item()]}, page='<a href="%s">x</a><section>nope</section>' % JOB_URL)
out = probe.f_teamtailor(restyled, TOKEN)
check("and a page whose markup moved again costs the location, not the board",
      len(out or []) == 1 and out[0].location == "", repr(out and out[0].location))

print()
print("-- an empty feed skips the second request entirely --")
quiet = Sess({"items": []})
probe.f_teamtailor(quiet, TOKEN)
check("nothing to attach a location to, so the page is not fetched",
      quiet.urls == [BASE + "/jobs.json"], repr(quiet.urls))

print()
print("-- discovery, which never asked Common Crawl about this host --")
check("teamtailor is in the crawl patterns now",
      discover_companies.CC_URL_PATTERNS.get("teamtailor") == "teamtailor.com")
check("as a domain match, because the tenant is the subdomain",
      "teamtailor" in discover_companies.CC_DOMAIN_MATCH)
for url, want in [("https://cigames.teamtailor.com/jobs", "cigames"),
                  ("https://foo.bamboohr.com/careers/list", "foo"),
                  ("https://acme.jobs.personio.de/xml", "acme"),
                  ("https://acme.jobs.personio.com/xml", "acme")]:
    ats = next(a for a in ("teamtailor", "bamboohr", "personio") if a in url)
    m = discover_companies._TOKEN_PATTERNS[ats].search(url)
    check("%s reads the tenant out of %s" % (ats, url.split("/")[2]),
          m and m.group(1) == want, repr(m and m.group(1)))
check("personio's embed pattern covers the .com host too, not only .de",
      any(a == "personio" and p.search("https://acme.jobs.personio.com/x")
          for a, p in probe.EMBED_ATS_PATTERNS))
check("lever is discovered again, through the Wayback index",
      discover_companies.CC_URL_PATTERNS.get("lever") == "jobs.lever.co/*")

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
