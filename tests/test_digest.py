"""The alert digest: what it says, in both parts, and how it holds up
with Hebrew titles, estimates and empty fields.

Run directly, no framework:  python tests/test_digest.py
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

# alerts.py builds its DynamoDB and SES clients at import time; nothing
# here calls them, but botocore still wants a region and keys to build.
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import alerts  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
ALERT = {"alert_id": "a1", "user_id": "u1", "email": "x@example.com",
         "filter": {"country": "IL", "department": "Product", "seniority": "senior", "search": ""}}


def job(**over):
    base = {"id": "abc", "title": "Senior Product Manager", "company_domain": "wix.com", "company_name": "Wix",
            "location": "Tel Aviv, Israel", "url": "https://boards.greenhouse.io/wix/jobs/1",
            "posted_at": (NOW - timedelta(hours=2)).isoformat(), "first_seen": (NOW - timedelta(hours=1)).isoformat(),
            "seniority": "senior", "workplace_type": "hybrid", "salary_text": None, "salary_is_estimate": 0, "salary_source": None,
            "logo_url": "https://wix.com/favicon.png"}
    base.update(over)
    return base


matches = [
    job(),
    job(id="heb", title="ראש/ת מנהל תקשורת שיווקית וקשרי לקוחות", company_domain="iai.co.il", company_name="IAI",
        location='נתב"ג, Israel', url="https://jobs.iai.co.il/job/76050151", posted_at=None,
        first_seen=(NOW - timedelta(days=3)).isoformat(), seniority=None, workplace_type=None),
    job(id="est", title="Data Engineer", salary_text="₪30K – ₪40K", salary_source="model", company_name=None, logo_url=None),
    job(id="disc", title="Backend <Lead>", salary_text="$150K – $200K", salary_source="disclosed"),
]

h = alerts._digest_html(len(matches), matches, ALERT, NOW)
t = alerts._digest_text(len(matches), matches, ALERT, NOW)

# Structure and brand
check("a compact header: the site's own mark, the wordmark, a 2px ink rule",
      "OpenTechJobs</td>" in h and 'src="https://opentechjobs.org/favicon-32.png"' in h and "border-bottom:2px solid #40513b" in h)
check("green is on actions only: Apply buttons and the board links, not the rule or the footer",
      "border-bottom:2px solid #609966" not in h and 'class="otj-ink" style="color:#40513b; text-decoration:underline;">Unsubscribe</a>' in h)
check("only DESIGN.md colours, both modes", all(c in h for c in ("#f2f0ef", "#40513b", "#3f6f45", "#d2cfcb", "#edf0ec", "#17181c", "#ededec", "#2fae60", "#23252b"))
      and "#ffffff" not in h and "#dce8d4" not in h and "#609966" not in h)
check("the summary is one calm line, no panel", ">4 new roles matching your alert</div>" in h and "background:#dce8d4" not in h)
check("the summary names the filter", "Israel &middot; Product &middot; Senior &middot; since your last alert" in h,
      re.search(r"since your last alert", h) and h[h.find("Israel") - 10:h.find("Israel") + 80])
view_all = alerts.board_url(ALERT)
check("view-all goes to the board with the alert's own filters, empty ones dropped",
      view_all == "https://opentechjobs.org/board?country=IL&department=Product&seniority=senior", view_all)
import html as _html
check("both calls to action use it, escaped as an attribute", h.count(f'href="{_html.escape(view_all)}"') == 2)
check("the calls to action are links, not buttons", "View all 4 matches &rarr;" in h and "View all matches &rarr;" in h)
check("the footer says why and how to stop it",
      "because you saved an alert" in h and h.count("/account") == 3 and "Unsubscribe" in h and "Pause alert" in h)
check("a dark-mode rule exists as an enhancement only", "prefers-color-scheme: dark" in h and "#3f6f45" in h)
check("fixed 600px table layout, inline styles, system fonts", 'width="600"' in h and "-apple-system" in h and "var(--" not in h)

# Rows
check("the title is the link, in ink, and Apply is the board's own button",
      'font-weight:700; color:#40513b; text-decoration:none;">Senior Product Manager</a>' in h
      and 'style="background:#3f6f45; border-radius:4px;">' in h and 'color:#f2f0ef; text-decoration:none; white-space:nowrap;">Apply &#8599;</a>' in h)
check("in dark mode the button is the site's dark green with the dark tint as its text",
      ".otj-btn { background: #2fae60 !important; }" in h and ".otj-btn-text { color: #23252b !important; }" in h)
check("a company mark in a band the height of the row, or a lettered square of the same size",
      'width="52" align="center" valign="middle"' in h and 'src="https://wix.com/favicon.png" width="44" height="44"' in h
      and 'font-size:18px; font-weight:700; color:#40513b;">W</td>' in h)
check("a ten-office listing is one place and a count",
      alerts._place({"location": "Bordeaux, France; Grenoble, France; Tel Aviv, Israel; Paris, France"}, ALERT) == "Tel Aviv, Israel + 3 locations"
      and alerts._place({"location": "Paris, France; Berlin, Germany"}, {"filter": {}}) == "Paris, France + 1 location"
      and alerts._place({"location": "Tel Aviv, Israel"}, ALERT) == "Tel Aviv, Israel")
check("company name over domain, with the place", "Wix &middot; Tel Aviv, Israel" in h)
check("posted age, level and workplace on one line", "Posted 2h ago &middot; Senior &middot; Hybrid" in h)
check("a Hebrew title is laid out right to left, its metadata left to right",
      'dir="rtl" style="text-align:right;">' in h and '<a href="https://jobs.iai.co.il/job/76050151" dir="rtl"' in h
      and 'dir="ltr" class="otj-ink" style="font-family' in h)
check("a listing with no date uses first seen, coarsely", "Posted 3d ago" in h)
check("a company with no name falls back to the domain", "wix.com &middot; Tel Aviv, Israel" in h)
check("an estimate is marked as one", "Est. ₪30K – ₪40K" in h and "market estimate" in h)
check("disclosed pay is not", "$150K – $200K" in h and "Est. $150K" not in h)
check("angle brackets in a title are escaped", "Backend &lt;Lead&gt;" in h and "<Lead>" not in h)
check("every listing has an Apply button", h.count("Apply &#8599;") == 4)
check("hairline under each row, no bar, no box; the only radius is the button's",
      'width="4"' not in h and h.count("border-bottom:1px solid #d2cfcb") >= 5 and h.count("border-radius") == 4)

# Plain-text part says the same things
check("text part: count, filter, view-all, each listing with apply and the footer",
      t.startswith("4 new roles matching your alert\nIsrael · Product · Senior · since your last alert")
      and f"View all matches: {view_all}" in t and t.count("Apply: https://") == 4
      and "Manage, pause or delete it: https://opentechjobs.org/account" in t, t[:300])
check("text part marks the estimate", "Est. ₪30K – ₪40K" in t)

# Singular and an alert with no filter
one = alerts._digest_html(1, matches[:1], {"filter": {}}, NOW)
check("singular reads right", ">1 new role matching your alert</div>" in one and "View all 1 match &rarr;" in one)
check("no filter means a bare board link and only the timing in the summary",
      f'href="https://opentechjobs.org/board"' in one and "&middot; since your last alert" not in one and "since your last alert" in one)
check("israel_only still reads as Israel", alerts._filter_summary({"filter": {"israel_only": True}}) == ["Israel"])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
