"""GET /company/{domain}: one employer as a real HTML page.

A job page lives about a month. A company page is the stable address:
"Wix jobs" is what people type, and the page that answers it should
be the same URL next year. It lists what the company has open right
now, links every listing's own page, and says how long the board has
watched the company and how many roles it has listed in all, which is
the one thing a careers site never tells you.

The rendering is a function of one company row, its open listings and
the clock. handler.py fetches those and picks the status; this module
says what the page looks like and what the status should be.

Status policy, which loader/sitemap.py mirrors:
  a company the board tracks       200. Indexable while it has an open
                                   role; noindex when it has none, so a
                                   thousand empty pages do not go into
                                   the index, but the URL still answers
                                   and says when it last had one
  an alias of another domain       301 to that domain's page (the
                                   loader demotes a second domain for
                                   the same board; api/same_company.py
                                   names the ones it cannot catch)
  unknown domain                   404
"""

import html
import json
import re
from collections import Counter
from datetime import datetime, timezone

from job_page import FOOT, SENIORITY_LABELS, SITE, TOPBAR, _head, _parse
from same_company import SAME_COMPANY

# A hostname with at least one dot, lowercase, no scheme or path. What
# the companies table holds; anything else is not a page we have.
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
MAX_LISTED = 300


def is_domain(s: str) -> bool:
    return bool(s) and bool(DOMAIN_RE.match(s))


def canonical_url(domain: str) -> str:
    return f"{SITE}/company/{domain}"


def company_label(company) -> str:
    return (company.get("company_name") or company.get("domain") or "").strip()


def redirect_target(company) -> str | None:
    """The domain this one is a second name for, if it is one."""
    domain = company.get("domain") or ""
    if domain in SAME_COMPANY:
        return SAME_COMPANY[domain]
    m = re.match(r"alias of ([a-z0-9.-]+) ", company.get("error") or "")
    return m.group(1) if m else None


def status_for(company) -> int:
    """404, 301 or 200. See the module docstring for the policy."""
    if not company:
        return 404
    return 301 if redirect_target(company) else 200


def _age(ts, now) -> str:
    d = _parse(ts)
    if not d:
        return ""
    days = (now - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return "1 month ago" if months == 1 else f"{months} months ago"


def _places(jobs) -> list[str]:
    """Cities most of the open roles are in, most common first."""
    c = Counter()
    for j in jobs:
        for city in (j.get("city") or "").split(","):
            if city.strip():
                c[city.strip()] += 1
    return [p for p, _ in c.most_common()]


def _departments(jobs) -> list[str]:
    c = Counter((j.get("department") or "").strip() for j in jobs)
    c.pop("", None)
    return [d for d, _ in c.most_common()]


def _join(items: list[str], limit: int) -> str:
    shown = items[:limit]
    rest = len(items) - len(shown)
    text = ", ".join(shown)
    return text + (f" and {rest} more" if rest > 0 else "")


def meta_description(company, jobs, facts) -> str:
    name = company_label(company)
    n = len(jobs)
    if not n:
        return f"{name} has no open roles on OpenTechJobs right now. The board has listed {facts['total']} of its roles since {facts['since']}."
    places = _places(jobs)
    where = f" in {_join(places, 3)}" if places else ""
    titles = _join([j.get("title", "").strip() for j in jobs[:3] if j.get("title")], 3)
    role = "open role" if n == 1 else "open roles"
    text = f"{n} {role} at {name}{where}: {titles}. Every listing links to the company's own application page."
    return text if len(text) <= 300 else text[:297].rsplit(" ", 1)[0] + "..."


def json_ld(company, jobs) -> dict:
    """schema.org Organization, with the open listings as an ItemList of
    the pages that carry their JobPosting markup. Nothing here is a
    claim the page does not show."""
    domain = company.get("domain") or ""
    org = {"@context": "https://schema.org", "@type": "Organization", "name": company_label(company),
           "url": f"https://{domain}"}
    if company.get("logo_url"):
        org["logo"] = company["logo_url"]
    data = {"@context": "https://schema.org", "@type": "CollectionPage", "url": canonical_url(domain),
            "name": f"{company_label(company)} jobs", "about": org}
    if jobs:
        data["mainEntity"] = {
            "@type": "ItemList",
            "numberOfItems": len(jobs),
            "itemListElement": [
                {"@type": "ListItem", "position": i, "url": f"{SITE}/job/{j['id']}", "name": j.get("title", "")}
                for i, j in enumerate(jobs[:MAX_LISTED], 1)
            ],
        }
    return data


def render(company, jobs, facts, now=None) -> str:
    """The page for a company the database knows.

    facts: {"total": roles listed all time, "since": first listing seen
    (ISO date), "last_open": when the last open role was seen, for a
    company with none open now}.
    """
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    name = company_label(company)
    domain = company.get("domain") or ""
    n = len(jobs)
    title = (f"{name} jobs: {n} open {'role' if n == 1 else 'roles'} | OpenTechJobs" if n
             else f"{name} jobs | OpenTechJobs")
    desc = meta_description(company, jobs, facts)
    head = _head(title, desc, canonical_url(domain), robots=None if n else "noindex,follow",
                 ld=json_ld(company, jobs), og_type="website")

    logo = ""
    if company.get("logo_url"):
        logo = f'<img class="job-page-logo" src="{esc(company["logo_url"])}" alt="" width="48" height="48" loading="lazy" />'

    places, departments = _places(jobs), _departments(jobs)
    rows = [("Website", f'<a class="link" href="https://{esc(domain)}" rel="nofollow noopener" target="_blank">{esc(domain)}</a>')]
    if places:
        rows.append(("Hiring in", esc(_join(places, 5))))
    if departments:
        rows.append(("Teams", esc(_join(departments, 5))))
    rows.append(("Roles listed", f"{facts['total']} since {esc(facts['since'])}"))
    if company.get("ats"):
        rows.append(("Applications via", esc(company["ats"])))
    meta = "".join(
        f'<div class="job-detail-meta-row"><span class="label">{esc(k)}</span><span class="value">{v}</span></div>'
        for k, v in rows)

    if n:
        items = []
        for j in jobs[:MAX_LISTED]:
            bits = []
            city = (j.get("city") or "").split(",")[0].strip()
            if city:
                bits.append(esc(city))
            elif j.get("location"):
                bits.append(esc(j["location"]))
            if j.get("seniority"):
                bits.append(esc(SENIORITY_LABELS.get(j["seniority"], j["seniority"])))
            if j.get("department"):
                bits.append(esc(j["department"]))
            when = _age(j.get("posted_at") or j.get("first_seen"), now)
            if when:
                bits.append(esc(when))
            items.append(f'<li><a class="link" href="/job/{esc(j["id"])}">{esc(j.get("title", ""))}</a>'
                         f'<span class="company-job-meta">{" · ".join(bits)}</span></li>')
        more = (f'<p class="company-more"><a class="link" href="/board?company={esc(domain)}">All {n} on the board</a></p>'
                if n > MAX_LISTED else "")
        listing = (f'<h2 class="job-detail-description-title">{n} open {"role" if n == 1 else "roles"}</h2>'
                   f'<ol class="company-jobs">{"".join(items)}</ol>{more}')
        summary = f"{n} open {'role' if n == 1 else 'roles'}" + (f" in {esc(_join(places, 3))}" if places else "")
    else:
        last = facts.get("last_open")
        seen = f" The last one closed {esc(_age(last, now))}." if last else ""
        listing = (f'<h2 class="job-detail-description-title">No open roles right now</h2>'
                   f'<p class="job-detail-description empty">Nothing is open at {esc(name)} at the moment.{seen} '
                   f'The board checks its careers page on every run and lists what appears.</p>')
        summary = "No open roles right now"

    body = f"""{TOPBAR}
  <main class="workspace">
    <section class="section">
      <article class="container job-page company-page">
        <div class="job-page-company">{logo}<span>{esc(name)}</span></div>
        <h1 class="job-page-title">{esc(name)} jobs</h1>
        <p class="company-summary">{summary}</p>
        <div class="job-page-actions"><a class="btn job-detail-apply" href="/board?company={esc(domain)}">Open on the board</a>
          <a class="btn ghost" href="https://{esc(domain)}" rel="nofollow noopener" target="_blank">Company site ↗</a></div>
        <div class="job-detail-meta">{meta}</div>
        {listing}
      </article>
    </section>
  </main>
{FOOT}"""
    return head + body


def render_redirect(target: str) -> str:
    """The body behind a 301, for a client that shows it."""
    url = canonical_url(target)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8" /><title>Moved</title>'
            f'<meta http-equiv="refresh" content="0; url={html.escape(url, quote=True)}" /></head>'
            f'<body><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></body></html>')


def render_missing(domain: str) -> str:
    title = "Company not found | OpenTechJobs"
    what = "The board does not track a company at this address. It may be spelled differently, or it may not have a careers page we can read."
    head = _head(title, what, canonical_url(domain), robots="noindex", og_type="website")
    return head + f"""{TOPBAR}
  <main class="workspace">
    <section class="section">
      <div class="container job-page">
        <h1 class="job-page-title">Company not found</h1>
        <p>{html.escape(what)}</p>
        <p><a class="btn" href="/board">Browse open listings</a></p>
      </div>
    </section>
  </main>
{FOOT}"""
