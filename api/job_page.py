"""GET /job/{id}: one listing as a real HTML page.

Every listing on the board used to exist only as /board?job=<id>, which
is one JavaScript-rendered page whose title and description are the same
for every job. A crawler asked for 271,000 of those sees 271,000 copies
of the board. This is the page a crawler, a link preview, or a browser
with scripts off gets instead: the listing's own title, company, place,
date, salary, description and apply link in the HTML, a canonical URL,
and JobPosting structured data that says the same things the page shows.

The rendering is a function of one job row plus the clock, and nothing
else, so it is testable without a database. handler.py fetches the row
and picks the status; this module only says what the page looks like
for a given job, and what the status should be.

Status policy, which the sitemap generator (loader/sitemap.py) mirrors:
  open                  200, indexable, with JobPosting markup
  closed within 30 days 200, "closed" banner, noindex, no JobPosting:
                        a saved link still lands somewhere useful, and
                        the markup goes because Google asks for it to
                        go when the role is no longer available
  closed longer ago     410 Gone
  unknown id            404

Salary is shown but never marked up. Disclosed pay arrives as free text
("$150K – $200K") that would have to be parsed into a currency and a
range to fit baseSalary, and a wrong number in structured data is worse
than none. Estimates are labelled as estimates on the page, in words,
which is the honest version of the same rule.
"""

import html
import json
import re
from datetime import datetime, timedelta, timezone

from countries import label_for

SITE = "https://opentechjobs.org"
CARD = f"{SITE}/og-hills.jpg"
EXPIRED_KEEP_DAYS = 30

SENIORITY_LABELS = {
    "intern": "Intern", "junior": "Junior", "mid": "Mid-level", "senior": "Senior", "staff": "Staff",
    "principal": "Principal", "lead": "Lead", "manager": "Manager", "director": "Director", "exec": "Executive",
}
WORKPLACE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def status_for(job, now=None) -> int:
    """404, 410 or 200. See the module docstring for the policy."""
    if not job:
        return 404
    closed = _parse(job.get("closed_at"))
    if closed is None:
        return 200
    now = now or datetime.now(timezone.utc)
    return 410 if now - closed > timedelta(days=EXPIRED_KEEP_DAYS) else 200


def company_label(job) -> str:
    return (job.get("company_name") or job.get("company_domain") or "").strip()


def canonical_url(job_id: str) -> str:
    return f"{SITE}/job/{job_id}"


def _salary_line(job):
    """(text, is_estimate) or None. Mirrors the board's own fallback: a
    row written before salary_source existed still carries the flag."""
    text = (job.get("salary_text") or "").strip()
    if not text:
        return None
    source = job.get("salary_source") or ("table" if job.get("salary_is_estimate") else "disclosed")
    return text, source != "disclosed"


def _description_html(text: str) -> str:
    """Plain text to paragraphs and lists. The stored description is
    cleaned plain text; lines that start like bullets become list
    items, blank lines separate paragraphs."""
    out, para, items = [], [], []

    def flush():
        nonlocal para, items
        if items:
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
            items = []
        if para:
            out.append("<p>" + "<br />".join(html.escape(p) for p in para) + "</p>")
            para = []

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^#{1,6}\s+(.*?)\s*:?\s*$", line)
        if heading:
            # Some sources keep markdown headings in their plain text.
            flush()
            out.append(f"<h3>{html.escape(heading.group(1))}</h3>")
            continue
        m = re.match(r"^[-*•·▪]\s+(.*)", line)
        if m:
            if para:
                flush()
            items.append(m.group(1))
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "".join(out)


def _meta_description(job) -> str:
    where = (job.get("location") or "").split(";")[0].strip()
    head = f"{job.get('title', '').strip()} at {company_label(job)}"
    if where:
        head += f", {where}"
    body = re.sub(r"\s+", " ", job.get("description") or "").strip()
    text = f"{head}. {body}" if body else head
    return text[:157].rstrip() + ("…" if len(text) > 157 else "")


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def _us_region(job, city):
    """The state code when a US listing spells it: "Boston, MA" gives MA.
    Only the two-letter form right after the city is trusted; a listing
    that says "Cambridge, Massachusetts" or just "Austin" gets no region
    rather than a guessed one."""
    if (job.get("country") or "") != "US":
        return None
    m = re.search(re.escape(city) + r"\s*,\s*([A-Z]{2})(?:\s*,|\s*$|\s*;)", job.get("location") or "")
    return m.group(1) if m and m.group(1) in US_STATES else None


def _places(job):
    """JobPosting jobLocation entries from the derived city and country
    columns, which are what the board's own filters trust. One Place per
    city when the countries are unambiguous, else one per country. No
    street or postal code: the listings do not carry one, and Search
    Console's note about them is a suggestion, not something to invent."""
    countries = [c for c in (job.get("country") or "").split(",") if c]
    cities = [c for c in (job.get("city") or "").split(",") if c]
    places = []
    if cities and len(countries) <= 1:
        for city in cities:
            addr = {"@type": "PostalAddress", "addressLocality": city}
            region = _us_region(job, city)
            if region:
                addr["addressRegion"] = region
            if countries:
                addr["addressCountry"] = countries[0]
            places.append({"@type": "Place", "address": addr})
    else:
        for code in countries:
            places.append({"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": code}})
    return places


_CURRENCIES = [
    # longest prefixes first, so CA$ is read before $
    ("CA$", "CAD"), ("C$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"), ("NZ$", "NZD"), ("US$", "USD"), ("S$", "SGD"),
    ("HK$", "HKD"), ("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP"), ("ILS", "ILS"), ("NIS", "ILS"), ("CAD", "CAD"),
    ("AUD", "AUD"), ("CHF", "CHF"), ("INR", "INR"), ("SGD", "SGD"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
    ("₪", "ILS"), ("₹", "INR"), ("¥", "JPY"),
]
_UNITS = [("per hour", "HOUR"), ("/hour", "HOUR"), ("/hr", "HOUR"), ("hourly", "HOUR"), ("per day", "DAY"),
          ("per week", "WEEK"), ("per month", "MONTH"), ("/month", "MONTH"), ("monthly", "MONTH"),
          ("per year", "YEAR"), ("/year", "YEAR"), ("annually", "YEAR"), ("a year", "YEAR")]
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _amounts(text):
    out = []
    for num, k in _AMOUNT.findall(text):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(v * 1000 if k else v)
    return out


def base_salary(job):
    """schema.org baseSalary, and only from a figure the employer gave.
    An estimate never goes into the markup, whatever the page says next
    to it. The text is the employer's own string, so a range reads as
    min and max, a single figure as a value, and anything with more than
    one range in it ("$600 – $2,000 per month · Multiple Ranges") is
    left out rather than half-read."""
    text = (job.get("salary_text") or "").strip()
    source = job.get("salary_source") or ("table" if job.get("salary_is_estimate") else "disclosed")
    if not text or source != "disclosed":
        return None
    # the first segment is the figure; anything after a separator is a
    # note (sign-on bonus, commission, "Multiple Ranges")
    head = re.split(r"\s[·•|]\s", text)[0]
    lower = head.lower()
    currency = next((code for sym, code in _CURRENCIES if sym.lower() in lower), None)
    if not currency:
        return None
    unit = next((u for word, u in _UNITS if word in lower), "YEAR")
    # strip currency words so "CA$90K" does not read its CA as a number
    figures = _amounts(re.sub(r"[A-Za-z]{2,3}\$", "$", head))
    if not figures or len(figures) > 2 or "multiple" in text.lower():
        return None
    value = {"@type": "QuantitativeValue", "unitText": unit}
    if len(figures) == 2:
        lo, hi = sorted(figures)
        value["minValue"], value["maxValue"] = lo, hi
    else:
        value["value"] = figures[0]
    return {"@type": "MonetaryAmount", "currency": currency, "value": value}


def json_ld(job) -> dict:
    """schema.org JobPosting for an open listing. Every value here is
    also visible on the page, which is Google's rule for this markup.

    No validThrough: the listings carry no closing date, and Google's
    own guidance is to leave the field out rather than invent one. A
    listing that closes is served 410 with the markup gone, which is
    the signal that matters."""
    posted = _parse(job.get("posted_at")) or _parse(job.get("first_seen"))
    org = {"@type": "Organization", "name": company_label(job)}
    domain = job.get("company_domain") or ""
    if "." in domain and not domain.endswith(".invalid"):
        org["sameAs"] = f"https://{domain}"
    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": job.get("title", ""),
        "description": job.get("description") or job.get("title", ""),
        "datePosted": posted.date().isoformat() if posted else None,
        "hiringOrganization": org,
        "url": canonical_url(job["id"]),
    }
    places = _places(job)
    if places:
        data["jobLocation"] = places if len(places) > 1 else places[0]
    if job.get("workplace_type") == "remote":
        data["jobLocationType"] = "TELECOMMUTE"
        # Google wants a remote listing to say where applicants may be,
        # and reads one with neither this nor a jobLocation as invalid.
        # The countries the listing itself names are that answer; a
        # remote listing that names none is left as it is.
        wanted = [{"@type": "Country", "name": label_for(c)} for c in (job.get("country") or "").split(",") if c]
        if wanted:
            data["applicantLocationRequirements"] = wanted if len(wanted) > 1 else wanted[0]
    if job.get("external_id"):
        data["identifier"] = {"@type": "PropertyValue", "name": job.get("ats") or "ats", "value": str(job["external_id"])}
    if job.get("department"):
        data["occupationalCategory"] = job["department"]
    salary = base_salary(job)
    if salary:
        data["baseSalary"] = salary
    return {k: v for k, v in data.items() if v is not None}


def _head(title, description, canonical, robots=None, ld=None, og_type="article"):
    """The head every server-rendered page shares; company_page.py uses
    it too. canonical is the page's own absolute URL."""
    esc = html.escape
    ld_tag = ""
    if ld is not None:
        # "<" inside a script element could open a tag; JSON is happy to
        # carry it as <, and every parser reads it back as "<".
        ld_tag = ('  <script type="application/ld+json">'
                  + json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c")
                  + "</script>\n")
    robots_tag = f'  <meta name="robots" content="{robots}" />\n' if robots else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(description)}" />
  <link rel="canonical" href="{canonical}" />
{robots_tag}  <meta property="og:type" content="{og_type}" />
  <meta property="og:url" content="{canonical}" />
  <meta property="og:title" content="{esc(title)}" />
  <meta property="og:description" content="{esc(description)}" />
  <meta property="og:image" content="{CARD}" />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{esc(title)}" />
  <meta name="twitter:description" content="{esc(description)}" />
  <meta name="twitter:image" content="{CARD}" />
  <link rel="alternate" type="application/rss+xml" title="OpenTechJobs newest listings" href="{SITE}/feed.xml" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png" />
  <link rel="apple-touch-icon" sizes="180x180" href="/favicon-180.png" />
  <link rel="stylesheet" href="/style.css" />
{ld_tag}  <script>
    if (localStorage.getItem("iljobs_theme") === "dark") {{
      document.documentElement.setAttribute("data-theme", "dark");
    }}
  </script>
</head>
"""


TOPBAR = """<body class="job-page-body">
  <div class="topbar">
    <div class="container">
      <nav class="topbar-nav"><a class="link" href="/board">Browse all jobs</a></nav>
    </div>
  </div>
"""

FOOT = """  <footer class="job-page-footer container">
    <a class="link" href="/board">Browse the board</a> · <a class="link" href="/api/help">API</a> · <a class="link" href="/privacy">Privacy</a>
  </footer>
</body>
</html>
"""


def render(job, now=None) -> str:
    """The page for a job the database knows. Status is status_for()."""
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    company = company_label(job)
    # The city goes in the title when there is one: "at Wix, Tel Aviv" is
    # what someone searching for the role types, and the title is the
    # one line of ours a search result shows.
    city = (job.get("city") or "").split(",")[0].strip()
    where = f", {city}" if city else ""
    title = f"{job.get('title', '').strip()} at {company}{where} | OpenTechJobs"
    closed = _parse(job.get("closed_at"))
    posted = _parse(job.get("posted_at")) or _parse(job.get("first_seen"))
    open_ = closed is None
    desc = _meta_description(job)
    head = _head(title, desc, canonical_url(job["id"]), robots=None if open_ else "noindex,follow", ld=json_ld(job) if open_ else None)

    badges = []
    if job.get("seniority"):
        badges.append(f'<span class="badge seniority">{esc(SENIORITY_LABELS.get(job["seniority"], job["seniority"]))}</span>')
    if job.get("workplace_type"):
        badges.append(f'<span class="badge workplace">{esc(WORKPLACE_LABELS.get(job["workplace_type"], job["workplace_type"]))}</span>')
    if not open_:
        badges.append('<span class="badge">Closed</span>')

    rows = [("Company", company), ("Location", job.get("location") or "-"), ("Category", job.get("category") or "-")]
    if job.get("department"):
        rows.append(("Department", job["department"]))
    rows.append(("Seniority", SENIORITY_LABELS.get(job.get("seniority") or "", job.get("seniority")) or "-"))
    rows.append(("Workplace", WORKPLACE_LABELS.get(job.get("workplace_type") or "", job.get("workplace_type")) or "-"))
    rows.append(("Posted", posted.date().isoformat() if posted else "-"))
    salary = _salary_line(job)
    if salary:
        text, estimate = salary
        rows.append(("Estimated salary" if estimate else "Salary", text + (" (a market estimate, not the employer's figure)" if estimate else "")))
    rows.append(("Via", job.get("ats") or "-"))
    meta = "".join(
        f'<div class="job-detail-meta-row"><span class="label">{esc(k)}</span><span class="value">{esc(str(v))}</span></div>'
        for k, v in rows)

    logo = ""
    if job.get("logo_url"):
        logo = f'<img class="job-page-logo" src="{esc(job["logo_url"])}" alt="" width="48" height="48" loading="lazy" />'

    if open_:
        notice = ""
        apply = (f'<a class="btn job-detail-apply" href="{esc(job.get("url") or "#")}" target="_blank" rel="noopener nofollow">Apply on the company site ↗</a>'
                 f' <a class="btn ghost" href="/board?job={esc(job["id"])}">Open on the board</a>')
    else:
        notice = (f'<div class="job-page-notice">This listing closed on {closed.date().isoformat()}. '
                  f'<a class="link" href="/board?company={esc(job.get("company_domain") or "")}">See what {esc(company)} is hiring for now</a>.</div>')
        apply = f'<a class="btn ghost" href="/board">Browse open listings</a>'

    description = _description_html(job.get("description") or "")
    if not description:
        description = '<p class="job-detail-description empty">No description was provided by this listing. The apply link has the full posting.</p>'

    body = f"""{TOPBAR}
  <main class="workspace">
    <section class="section">
      <article class="container job-page">
        {notice}
        <div class="job-page-company">{logo}<a class="link" href="/company/{esc(job.get("company_domain") or "")}">{esc(company)}</a></div>
        <h1 class="job-page-title">{esc(job.get("title", ""))}</h1>
        <div class="job-detail-badges">{"".join(badges)}</div>
        <div class="job-page-actions">{apply}</div>
        <div class="job-detail-meta">{meta}</div>
        <h2 class="job-detail-description-title">Description</h2>
        <div class="job-detail-description job-page-description">{description}</div>
      </article>
    </section>
  </main>
{FOOT}"""
    return head + body


def render_missing(status: int, job_id: str) -> str:
    """The 404 and 410 pages: short, honest, noindex."""
    gone = status == 410
    title = "Listing no longer available | OpenTechJobs" if gone else "Listing not found | OpenTechJobs"
    what = ("This listing closed a while ago and the page has been retired."
            if gone else "There is no listing with this id. It may have been removed, or the link may be wrong.")
    head = _head(title, what, canonical_url(job_id), robots="noindex", og_type="website")
    return head + f"""{TOPBAR}
  <main class="workspace">
    <section class="section">
      <div class="container job-page">
        <h1 class="job-page-title">{html.escape(title.split(" | ")[0])}</h1>
        <p>{html.escape(what)}</p>
        <p><a class="btn" href="/board">Browse open listings</a></p>
      </div>
    </section>
  </main>
{FOOT}"""
