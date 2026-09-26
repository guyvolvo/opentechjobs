"""/job/{id}: the page says what the listing says, and no more.

The six shapes named when this was planned: an Israeli job, a remote
job, disclosed salary, estimated salary, a closed job, a missing job.
Plus the ones that bite in markup: a title with an angle bracket, and a
description carrying "</script>".

Run directly, no framework:  python tests/test_job_page.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import job_page  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def job(**over):
    base = {
        "id": "b561172d4d0ff1d6", "company_domain": "wix.com", "company_name": "Wix", "ats": "greenhouse",
        "external_id": "4599111", "title": "Senior Software Engineer", "location": "Tel Aviv, Israel",
        "department": "Engineering", "category": "Software Engineering", "seniority": "senior",
        "workplace_type": "hybrid", "url": "https://boards.greenhouse.io/wix/jobs/4599111",
        "posted_at": "2026-09-17T10:00:00+00:00", "first_seen": "2026-09-17T10:05:00+00:00",
        "closed_at": None, "description": "Build things.\n\n- Ship\n- Measure\n\nBe kind.",
        "salary_text": None, "salary_is_estimate": 0, "salary_source": None,
        "country": "IL", "city": "Tel Aviv", "logo_url": "https://wix.com/favicon.png",
    }
    base.update(over)
    return base


def ld_of(page):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    return json.loads(m.group(1).replace("<\\/", "</")) if m else None


# An open Israeli job.
p = job_page.render(job(), NOW)
check("open job is 200", job_page.status_for(job(), NOW) == 200)
check("title names the job, the company and the city", "<title>Senior Software Engineer at Wix, Tel Aviv | Ocean of Jobs</title>" in p)
check("canonical is the job's own page", '<link rel="canonical" href="https://oceanofjobs.com/job/b561172d4d0ff1d6" />' in p)
check("no robots restriction on an open job", 'name="robots"' not in p)
check("the visible page has the title, company, place and apply link",
      all(s in p for s in ("<h1 class=\"job-page-title\">Senior Software Engineer</h1>", ">Wix<", "Tel Aviv, Israel",
                           'href="https://boards.greenhouse.io/wix/jobs/4599111"')))
check("description became paragraphs and a list",
      "<p>Build things.</p><ul><li>Ship</li><li>Measure</li></ul><p>Be kind.</p>" in p, p[p.find("Description"):][:300])
check("the board link keeps the old deep link working", 'href="/board?job=b561172d4d0ff1d6"' in p)
h = job_page.render(job(description="Intro.\n\n## What You'll Do:\n- Ship\n\n### Who you are\nYou."), NOW)
check("markdown headings left in the text become headings, without the trailing colon",
      "<h3>What You'll Do</h3><ul><li>Ship</li></ul><h3>Who you are</h3><p>You.</p>" in h.replace("&#x27;", "'"), h[h.find("Intro"):][:200])
ld = ld_of(p)
check("JobPosting markup is present and parses", ld is not None and ld.get("@type") == "JobPosting", repr(ld)[:120])
check("markup says the same title, org, date and url as the page",
      ld["title"] == "Senior Software Engineer" and ld["hiringOrganization"]["name"] == "Wix"
      and ld["datePosted"] == "2026-09-17" and ld["url"] == "https://oceanofjobs.com/job/b561172d4d0ff1d6", repr(ld))
check("location is the derived city and country",
      ld["jobLocation"]["address"] == {"@type": "PostalAddress", "addressLocality": "Tel Aviv", "addressCountry": "IL"},
      repr(ld.get("jobLocation")))
check("no salary in markup when none is given", "baseSalary" not in ld)
check("the RSS feed is announced", 'type="application/rss+xml"' in p)
check("the preview card is the site card", "og-hills.jpg" in p)

# Remote, no place at all.
r = job_page.render(job(location="Remote", country="", city="", workplace_type="remote"), NOW)
ld = ld_of(r)
check("a remote job is marked TELECOMMUTE with no invented place",
      ld.get("jobLocationType") == "TELECOMMUTE" and "jobLocation" not in ld and "applicantLocationRequirements" not in ld, repr(ld))
r = job_page.render(job(location="Remote, Israel", country="IL", city="", workplace_type="remote"), NOW)
ld = ld_of(r)
check("a remote job that names a country says applicants may be there",
      ld.get("jobLocationType") == "TELECOMMUTE" and ld.get("applicantLocationRequirements") == {"@type": "Country", "name": "Israel"}, repr(ld.get("applicantLocationRequirements")))
r = job_page.render(job(location="Remote (US or Canada)", country="US,CA", city="", workplace_type="remote"), NOW)
ld = ld_of(r)
check("two countries are a list",
      [c["name"] for c in ld.get("applicantLocationRequirements", [])] == ["United States", "Canada"], repr(ld.get("applicantLocationRequirements")))

# Several countries: one Place per country, no city guessing.
m = job_page.json_ld(job(location="Paris, France; Tel Aviv, Israel", country="FR,IL", city="Paris,Tel Aviv"))
check("a multi-country listing gets one Place per country",
      [pl["address"]["addressCountry"] for pl in m["jobLocation"]] == ["FR", "IL"], repr(m.get("jobLocation")))

# Salary, disclosed and estimated.
d = job_page.render(job(salary_text="$150K – $200K", salary_source="disclosed"), NOW)
check("disclosed salary is shown as salary", ">Salary<" in d and "$150K – $200K" in d and "estimate" not in d.lower())
e = job_page.render(job(salary_text="₪30K – ₪40K", salary_source="model"), NOW)
check("an estimate is labelled as one, in words", ">Estimated salary<" in e and "market estimate" in e)
check("and never appears in the markup", "baseSalary" not in (ld_of(e) or {}))
check("a disclosed range is baseSalary, min and max, yearly by default",
      ld_of(d)["baseSalary"] == {"@type": "MonetaryAmount", "currency": "USD",
                                 "value": {"@type": "QuantitativeValue", "unitText": "YEAR", "minValue": 150000.0, "maxValue": 200000.0}},
      repr(ld_of(d).get("baseSalary")))
bs = lambda text: job_page.base_salary(job(salary_text=text, salary_source="disclosed"))
check("the employer's own shapes read right",
      bs("$165,000 - $216,562")["value"] == {"@type": "QuantitativeValue", "unitText": "YEAR", "minValue": 165000.0, "maxValue": 216562.0}
      and bs("$40 per hour")["value"] == {"@type": "QuantitativeValue", "unitText": "HOUR", "value": 40.0}
      and bs("CA$90K - CA$115K")["currency"] == "CAD" and bs("CA$90K - CA$115K")["value"]["maxValue"] == 115000.0
      and bs("€60K - €70K")["currency"] == "EUR" and bs("₪30K – ₪40K")["currency"] == "ILS"
      and bs("$40 – $45 per hour · $2,500 sign-on bonus")["value"] == {"@type": "QuantitativeValue", "unitText": "HOUR", "minValue": 40.0, "maxValue": 45.0},
      repr((bs("$165,000 - $216,562"), bs("$40 per hour"), bs("CA$90K - CA$115K"), bs("$40 – $45 per hour · $2,500 sign-on bonus"))))
check("more than one range, or no currency, is left out rather than half-read",
      bs("$600 – $2,000 per month · Multiple Ranges") is None and bs("Competitive") is None and bs("30K - 40K") is None)

# Region: only a spelled-out US state code, never a guess.
r = job_page.json_ld(job(location="Boston, MA", country="US", city="Boston"))
check("Boston, MA carries addressRegion MA", r["jobLocation"]["address"].get("addressRegion") == "MA", repr(r["jobLocation"]))
r2 = job_page.json_ld(job(location="Cambridge, Massachusetts", country="US", city="Cambridge"))
r3 = job_page.json_ld(job(location="Tel Aviv-Yafo, Tel Aviv District, Israel", country="IL", city="Tel Aviv"))
check("a spelled-out state or a non-US listing gets none",
      "addressRegion" not in r2["jobLocation"]["address"] and "addressRegion" not in r3["jobLocation"]["address"])
check("validThrough is not invented", "validThrough" not in job_page.json_ld(job()))
old = job_page.render(job(salary_text="₪30K", salary_source=None, salary_is_estimate=1), NOW)
check("the old boolean still reads as an estimate", ">Estimated salary<" in old)

# Closed: recently, and long ago.
recent = job(closed_at=(NOW - timedelta(days=3)).isoformat())
c = job_page.render(recent, NOW)
check("a job closed three days ago is still a page", job_page.status_for(recent, NOW) == 200)
check("but says so, and asks not to be indexed",
      "This listing closed on 2026-09-15" in c and 'name="robots" content="noindex,follow"' in c)
check("with no JobPosting markup and no apply link", ld_of(c) is None and "greenhouse.io" not in c)
check("and points at the company's open roles", 'href="/board?company=wix.com"' in c)
check("a job closed six weeks ago is gone", job_page.status_for(job(closed_at=(NOW - timedelta(days=45)).isoformat()), NOW) == 410)
check("an unknown job is not found", job_page.status_for(None, NOW) == 404)
g = job_page.render_missing(410, "deadbeef")
check("the gone page is noindex and says why", 'content="noindex"' in g and "retired" in g)

# Markup safety.
s = job_page.render(job(title="C++ <Senior> Engineer", description='Close </script><b>x</b> early'), NOW)
check("angle brackets in the title are escaped in HTML", "C++ &lt;Senior&gt; Engineer" in s and "<Senior>" not in s.split("<script")[0])
check("a description cannot end the JSON-LD script early",
      "</script><b>" not in s and ld_of(s)["description"].startswith("Close </script>"))

# Meta description is bounded and readable.
long = job_page.render(job(description="word " * 100), NOW)
md = re.search(r'<meta name="description" content="([^"]*)"', long).group(1)
check("meta description is trimmed to a sentence-ish length", 120 <= len(md) <= 160 and md.startswith("Senior Software Engineer at Wix, Tel Aviv, Israel."), md)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
