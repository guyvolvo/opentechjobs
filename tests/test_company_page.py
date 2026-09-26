"""/company/<domain>: what the page says, and what status it answers.

Run directly, no framework:  python tests/test_company_page.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import company_page  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
ago = lambda d: (NOW - timedelta(days=d)).isoformat(timespec="seconds")

WIX = {"domain": "wix.com", "ats": "smartrecruiters", "error": None, "first_seen": ago(300),
       "company_name": "Wix", "logo_url": "https://wix.com/favicon.png"}


def job(i, title, city="Tel Aviv", **over):
    base = {"id": f"id{i:02d}", "title": title, "location": f"{city}, Israel" if city else "Remote", "department": "R&D",
            "seniority": "senior", "workplace_type": "hybrid", "posted_at": ago(i), "first_seen": ago(i),
            "country": "IL" if city else "", "city": city}
    base.update(over)
    return base


jobs = [job(1, "Senior Backend Engineer"), job(2, "Product Designer <UX>", department="Design"),
        job(3, "Data Engineer", city="Berlin", country="DE"), job(9, "QA Lead", city="")]
facts = {"total": 412, "since": "2025-11-03", "last_open": None}


def ld_of(page):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    return json.loads(m.group(1).replace("<\\/", "</")) if m else None


p = company_page.render(WIX, jobs, facts, NOW)
check("a tracked company is 200", company_page.status_for(WIX) == 200)
check("title is the query: '<name> jobs', with the open count", "<title>Wix jobs: 4 open roles | Ocean of Jobs</title>" in p)
check("canonical is the company's own page", '<link rel="canonical" href="https://oceanofjobs.com/company/wix.com" />' in p)
check("indexable while it has an open role", 'name="robots"' not in p)
check("h1 and the summary say what the page is",
      '<h1 class="job-page-title">Wix jobs</h1>' in p and "4 open roles in Tel Aviv, Berlin" in p)
check("every open role links its own page, with place, level, team and age",
      p.count('<a class="link" href="/job/id') == 4
      and '<a class="link" href="/job/id01">Senior Backend Engineer</a><span class="company-job-meta">Tel Aviv · Senior · R&amp;D · 1 day ago</span>' in p
      and "Remote · Senior · R&amp;D · 9 days ago" in p, p[p.find("<ol"):p.find("</ol>")][:600])
check("angle brackets in a title are escaped", "Product Designer &lt;UX&gt;" in p and "<UX>" not in p)
check("the facts: website (nofollow), places, teams, roles listed since",
      'href="https://wix.com" rel="nofollow noopener"' in p and ">Tel Aviv, Berlin<" in p
      and ">R&amp;D, Design<" in p and ">412 since 2025-11-03<" in p)
check("actions go to the board filtered to the company, and the company site",
      'href="/board?company=wix.com"' in p and 'href="https://wix.com" rel="nofollow noopener" target="_blank">Company site' in p)
md = re.search(r'<meta name="description" content="([^"]*)"', p).group(1)
check("meta description counts, places and names the newest roles",
      md.startswith("4 open roles at Wix in Tel Aviv, Berlin: Senior Backend Engineer, Product Designer &lt;UX&gt;, Data Engineer."), md)
ld = ld_of(p)
check("markup: a CollectionPage about the Organization, listing the job pages",
      ld and ld["@type"] == "CollectionPage" and ld["about"]["@type"] == "Organization" and ld["about"]["url"] == "https://wix.com"
      and ld["about"]["logo"] == "https://wix.com/favicon.png" and ld["mainEntity"]["numberOfItems"] == 4
      and ld["mainEntity"]["itemListElement"][0]["url"] == "https://oceanofjobs.com/job/id01", repr(ld)[:300])
check("the shared head and foot are the listing page's", 'class="job-page-body"' in p and 'href="https://oceanofjobs.com/feed.xml"' in p and "Browse the board" in p)

# Nothing open: the page stays, says so, and is not for the index.
e = company_page.render(WIX, [], {"total": 412, "since": "2025-11-03", "last_open": ago(40)}, NOW)
check("no open roles: 200, noindex, and the page says when the last one closed",
      company_page.status_for(WIX) == 200 and '<meta name="robots" content="noindex,follow" />' in e
      and "<title>Wix jobs | Ocean of Jobs</title>" in e and "The last one closed 1 month ago." in e)
check("its markup carries no empty list", "mainEntity" not in (ld_of(e) or {}))

# Aliases redirect to the one page.
alias = {"domain": "wix2.com", "ats": None, "error": "alias of wix.com (both resolve to smartrecruiters:Wix2)", "first_seen": ago(10)}
check("a demoted alias is a 301 to the real domain",
      company_page.status_for(alias) == 301 and company_page.redirect_target(alias) == "wix.com")
same = {"domain": "sentinelone.com", "ats": "greenhouse", "error": None}
check("a same_company duplicate redirects too", company_page.redirect_target(same) == "sentinellabs.io")
check("the redirect body names the target", 'href="https://oceanofjobs.com/company/wix.com"' in company_page.render_redirect("wix.com"))

# Unknown and malformed.
check("unknown is 404", company_page.status_for(None) == 404)
check("only a hostname is a page", company_page.is_domain("wix.com") and company_page.is_domain("check-point.co.il")
      and not company_page.is_domain("wix") and not company_page.is_domain("Wix.com") and not company_page.is_domain("a/b.com")
      and not company_page.is_domain("x.com/../y"))
m = company_page.render_missing("nope.example")
check("the 404 page is noindex and offers the board", 'content="noindex"' in m and 'href="/board"' in m)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
