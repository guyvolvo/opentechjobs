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

import html as _html  # noqa: E402
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

# What the mail is not, which is most of what changed
check("no mark, no wordmark, no ink rule over the headline",
      "favicon-32.png" not in h and ">OpenTechJobs<" not in h and "border-bottom:2px solid" not in h)
check("no view-all links and no per-row Apply buttons", "View all" not in h and "Apply" not in h)
check("no standing explanation and no pause link",
      "because you saved an alert" not in h and "Pause alert" not in h)
check("never says a salary is undisclosed", "Undisclosed" not in h)

# The headline block
check("the headline names the alert and counts the jobs",
      "4 new jobs for &ldquo;Israel&rdquo;" in h, h[h.find("new jobs") - 60:h.find("new jobs") + 60])
check("Arial bold 22px in ink, 20px on a phone",
      "font-size:22px; line-height:1.25; font-weight:700; color:#40513b" in h
      and ".otj-head { font-size: 20px !important; }" in h)
check("the subline is 14px with 14px under it", ">Since your last alert</div>" in h and "padding:6px 0 14px 0" in h)
check("the preheader is hidden and says how many, where", "4 new roles in Israel" in h and "max-height:0" in h)

# Rows
check("each row has a hairline over it and 16px of vertical padding",
      h.count("padding:16px 0; border-top:1px solid #d2cfcb") == 4)
check("the whole row is one link to the job",
      '<a href="https://boards.greenhouse.io/wix/jobs/1" style="font-family:Arial,Helvetica,sans-serif; color:#40513b; text-decoration:none; display:block;">' in h)
check("48px logo tile: white, hairline, 4px radius",
      'width="48" align="center" valign="middle" style="width:48px; height:48px; background:#ffffff; border:1px solid #d2cfcb; border-radius:4px;' in h
      and h.count('padding-left:14px') == 4)
check("a company with no logo gets a lettered tile the same size, lettered from the company not the title",
      'border-radius:4px; font-family:Arial,Helvetica,sans-serif; font-size:18px; font-weight:700; color:#40513b;">W</td>' in h)
check("the title is bold 16px in green text", 'font-size:16px; line-height:1.3; font-weight:700; color:#3f6f45;">Senior Product Manager</div>' in h)
check("company name over domain, with the place", "Wix &middot; Tel Aviv, Israel" in h)
check("a bare domain has its dots broken so Gmail leaves it alone",
      "wix⁠.⁠com" in h and ">wix.com &middot;" not in h)
check("a real company name is not mangled", "Wix" in h and "W⁠i" not in h)
check("the meta line is the estimate then the age, nothing else",
      "Est. ₪30K – ₪40K &middot; 2h ago" in h and "Senior &middot; Hybrid" not in h)
check("disclosed pay is not marked as an estimate", "$150K – $200K &middot; 2h ago" in h and "Est. $150K" not in h)
check("a listing with no date falls back to first seen", "3d ago" in h)
check("angle brackets in a title are escaped", "Backend &lt;Lead&gt;" in h and "<Lead>" not in h)

# Direction
check("title and company lines size themselves to the script, the meta line does not",
      h.count('dir="auto"') == 9 and h.count('dir="ltr"') == 4 and 'dir="rtl"' not in h)

# The one button, and the footer
board = alerts.board_url(ALERT)
check("the board link carries the alert's own filters, empty ones dropped",
      board == "https://opentechjobs.org/board?country=IL&department=Product&seniority=senior", board)
check("one button, ink on paper, and it is the only radius besides the tiles",
      h.count(f'href="{_html.escape(board)}"') == 1 and "background:#40513b; border-radius:4px" in h
      and ">See all jobs</a>" in h and "padding:11px 20px" in h)
check("the footer is two underlined links, 32px down",
      "padding-top:32px" in h and ">Edit alert</a>" in h and ">Unsubscribe</a>" in h and h.count("/account") == 2)

# The shell
check("600px, 32px of padding, 20px on a phone, Arial, no web fonts, no variables",
      'width="600"' in h and 'class="otj-pad" style="padding:32px;"' in h
      and ".otj-pad { padding: 20px !important; }" in h
      and "Arial,Helvetica,sans-serif" in h and "-apple-system" not in h
      and "fonts.googleapis" not in h and "var(--" not in h)
check("paper background on the body and the outer table", h.count("background:#f2f0ef") >= 2)

# Five rows at most, and the button carries the rest
many = [job(id=f"j{i}", title=f"Role {i}") for i in range(9)]
big = alerts._digest_html(9, many, ALERT, NOW)
check("at most five rows are shown", big.count("padding:16px 0; border-top:1px solid #d2cfcb") == 5
      and "Role 4" in big and "Role 5" not in big)
check("the button says how many there are in total", ">See all 9 jobs</a>" in big)

# Plain text says the same things
check("text part: headline, subline, rows with their links, then the board",
      t.startswith('4 new jobs for "Israel"\nSince your last alert')
      and t.count("https://") == 6 and f"See all jobs: {board}" in t
      and f"Edit alert or unsubscribe: {alerts.SITE_ORIGIN}/account" in t, t[:200])
check("text part marks the estimate and drops the rest", "Est. ₪30K – ₪40K · 2h ago" in t and "Hybrid" not in t)

# Singular, and an alert with no filter at all
one = alerts._digest_html(1, matches[:1], {"filter": {}}, NOW)
check("singular reads right", "1 new job for &ldquo;your alert&rdquo;" in one and "1 new role in your alert" in one)
check("no filter means a bare board link", 'href="https://opentechjobs.org/board"' in one)
check("israel_only still reads as Israel", alerts._filter_summary({"filter": {"israel_only": True}}) == ["Israel"]
      and alerts.alert_name({"filter": {"israel_only": True}}) == "Israel")

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
