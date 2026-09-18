"""Alert evaluation: matches each active alert's filter against jobs
first-seen since its own watermark, sends one digest email per alert
with any new matches, advances the watermark. Called once per fast-poll
cycle (scrape_handler.py, right after the loader step, while jobs.db is
already fresh on /tmp -- no separate download needed here).

Uses job_filters.build_jobs_where() for the actual matching, the same
function /api/jobs itself uses (api/handler.py) -- an alert matches
exactly what its owner would see applying those filters on the live
board, not a second, independently-drifting approximation of it.
"""

import html
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import boto3
from boto3.dynamodb.conditions import Attr

from countries import label_for
from job_filters import build_jobs_where, has_fts_index, has_places, register_functions, salary_source_select

ALERTS_TABLE = os.environ.get("ALERTS_TABLE")
FROM_EMAIL = os.environ.get("ALERTS_FROM_EMAIL", "alerts@guyvoloshin.com")
SITE_ORIGIN = os.environ.get("SITE_ORIGIN", "https://opentechjobs.org")

_dynamodb = boto3.resource("dynamodb")
_ses = boto3.client("sesv2")


def evaluate_alerts(jobs_db_path: Path) -> dict:
    if not ALERTS_TABLE:
        # Not every environment running scrape_handler.py needs this
        # (local testing, a future non-alerts deployment) -- absence
        # means "don't evaluate," not an error.
        return {"skipped": "ALERTS_TABLE not set"}

    table = _dynamodb.Table(ALERTS_TABLE)
    alerts = _scan_active_alerts(table)

    # Read-only, and the loader step just finished writing this same
    # file moments ago in the same invocation -- no reason to hold a
    # write lock or risk racing it.
    conn = sqlite3.connect(f"file:{jobs_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    register_functions(conn)

    sent = 0
    errors = []
    for alert in alerts:
        try:
            matches = _find_new_matches(conn, alert)
            if matches:
                _send_digest(alert, matches)
                sent += 1
            table.update_item(
                Key={"user_id": alert["user_id"], "alert_id": alert["alert_id"]},
                UpdateExpression="SET last_notified_at = :t",
                ExpressionAttributeValues={":t": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as e:
            # One user's bad filter or bounced address shouldn't stop
            # every other alert from being checked.
            errors.append(f"{alert['user_id']}/{alert['alert_id']}: {e}")

    conn.close()
    return {"alerts_checked": len(alerts), "digests_sent": sent, "errors": errors}


def _scan_active_alerts(table) -> list[dict]:
    # No GSI on active (see infra/dynamodb.tf) -- full scan, filtered
    # client-side, cheap at this project's expected alert volume.
    items = []
    resp = table.scan(FilterExpression=Attr("active").eq(True))
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(FilterExpression=Attr("active").eq(True), ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _find_new_matches(conn: sqlite3.Connection, alert: dict) -> list[dict]:
    filter_params = dict(alert.get("filter") or {})
    where_sql, args = build_jobs_where(filter_params, has_fts_index(conn), has_places(conn))
    # Always present: route_create_alert (api/handler.py) sets this to
    # created_at at creation time specifically so a brand-new alert's
    # first evaluation only picks up genuinely new postings, not every
    # already-open job that happened to match on day one.
    watermark = alert["last_notified_at"]
    where_sql += " AND first_seen > ?"
    args = [*args, watermark]

    # The digest shows more than a title and a domain now: the company's
    # own name, when it closed or opened, level, workplace and pay. Each
    # guarded the way api/handler.py guards them, because this runs
    # against whatever snapshot is on disk and a column can be a merge
    # away from existing.
    try:
        has_name = any(r[1] == "company_name" for r in conn.execute("PRAGMA table_info(companies)"))
    except sqlite3.Error:
        has_name = False
    name_sql = ("(SELECT company_name FROM companies WHERE domain = jobs.company_domain) AS company_name"
                if has_name else "NULL AS company_name")
    rows = conn.execute(
        f"SELECT id, title, company_domain, {name_sql}, location, url, posted_at, first_seen, "
        f"seniority, workplace_type, salary_text, salary_is_estimate, {salary_source_select(conn)} FROM jobs "
        f"WHERE {where_sql} ORDER BY first_seen DESC LIMIT 50",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def _send_digest(alert: dict, matches: list[dict]) -> None:
    to_email = alert.get("email")
    if not to_email:
        return
    n = len(matches)
    # Requested live: a plain "N new jobs on OpenTechJobs" subject looked
    # identical across every digest in an inbox, no way to tell them
    # apart at a glance without opening each one. UTC, not the site's own
    # display timezone -- there isn't one consistent "local" time for an
    # arbitrary subscriber, and an unlabeled time is worse than an exact,
    # honestly-labeled one.
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"{n} new job{'s' if n != 1 else ''} on OpenTechJobs.org [{timestamp}]"

    _ses.send_email(
        FromEmailAddress=FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Content={
            "Simple": {
                "Subject": {"Data": subject},
                # Both parts of one multipart/alternative message, not two
                # separate sends -- an HTML-capable client (virtually all
                # of them, Gmail included) renders Html and ignores Text
                # entirely. Reported live: without an Html part, Gmail's
                # plain-text autolinker was turning the bare company
                # domain into its own (wrong-destination) link on top of
                # the real job URL printed below it, so every listing
                # showed two separate, differently-colored links. The
                # title is the only link in the Html version, pointed at
                # the real URL, so that duplication can't happen there.
                "Body": {
                    "Html": {"Data": _digest_html(n, matches, alert)},
                    "Text": {"Data": _digest_text(n, matches, alert)},
                },
            }
        },
    )


# Labels the board uses, kept here rather than imported from the
# frontend, which is JavaScript. Short on purpose: a digest row has one
# line for all of them.
_SENIORITY = {"intern": "Intern", "junior": "Junior", "mid": "Mid-level", "senior": "Senior", "staff": "Staff",
              "principal": "Principal", "lead": "Lead", "manager": "Manager", "director": "Director", "exec": "Executive"}
_WORKPLACE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}

# Hebrew and Arabic script. A title in either is laid out right to left
# on its own line; the metadata under it stays left to right, because a
# domain, a salary range and a time are left-to-right things and forcing
# a whole row RTL mangles them.
_RTL_RE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF]")


def _is_rtl(text: str) -> bool:
    return bool(_RTL_RE.search(text or ""))


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age(job: dict, now: datetime) -> str:
    """"Posted 2h ago", from the source's date or, failing that, when the
    board first saw it. Coarse on purpose: a digest is not a clock."""
    when = _parse(job.get("posted_at")) or _parse(job.get("first_seen"))
    if not when:
        return "Posted recently"
    mins = max(0, int((now - when).total_seconds() // 60))
    if mins < 60:
        return "Posted just now" if mins < 5 else f"Posted {mins}m ago"
    if mins < 60 * 48:
        return f"Posted {mins // 60}h ago"
    return f"Posted {mins // (60 * 24)}d ago"


def _salary(job: dict):
    """(text, is_estimate) or None. The same fallback the board makes for
    a row written before salary_source existed."""
    text = (job.get("salary_text") or "").strip()
    if not text:
        return None
    source = job.get("salary_source") or ("table" if job.get("salary_is_estimate") else "disclosed")
    return text, source != "disclosed"


def _company(job: dict) -> str:
    return (job.get("company_name") or job.get("company_domain") or "").strip()


def board_url(alert: dict) -> str:
    """The board with this alert's own filters applied. The filter keys
    are the board's query parameters (route_create_alert allows only
    those), so this is a straight encoding."""
    params = {k: v for k, v in (alert.get("filter") or {}).items() if v not in (None, "", [], False)}
    return f"{SITE_ORIGIN}/board" + (f"?{urlencode(params, doseq=True)}" if params else "")


def _filter_summary(alert: dict) -> list[str]:
    """Up to three words for the summary block: where, what, how senior.
    Reads the same keys the board's own alert panel describes."""
    f = alert.get("filter") or {}
    out = []
    countries = f.get("country") or ("IL" if f.get("israel_only") else "")
    if countries:
        out.append(", ".join(label_for(c) for c in str(countries).split(",") if c))
    for key in ("city", "department", "search", "q", "keywords", "seniority", "workplace"):
        v = f.get(key)
        if v:
            v = str(v)
            if key == "seniority":
                v = ", ".join(_SENIORITY.get(x, x) for x in v.split(","))
            elif key == "workplace":
                v = ", ".join(_WORKPLACE.get(x, x) for x in v.split(","))
            elif key in ("search", "q", "keywords"):
                v = f"\u201c{v}\u201d"
            out.append(v)
        if len(out) >= 3:
            break
    return out


def _digest_text(n: int, matches: list[dict], alert: dict | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    alert = alert or {}
    head = f"{n} new role{'s' if n != 1 else ''} matching your alert"
    summary = " · ".join(_filter_summary(alert) + ["since your last alert"])
    lines = [head, summary, "", f"View all matches: {board_url(alert)}", ""]
    for j in matches:
        meta = [_company(j), j.get("location") or "Location unknown"]
        lines.append(f"{j['title']}")
        lines.append(f"  {' · '.join(x for x in meta if x)}")
        bits = [_age(j, now)]
        if j.get("seniority"):
            bits.append(_SENIORITY.get(j["seniority"], j["seniority"]))
        if j.get("workplace_type"):
            bits.append(_WORKPLACE.get(j["workplace_type"], j["workplace_type"]))
        sal = _salary(j)
        if sal:
            bits.append(("Est. " if sal[1] else "") + sal[0])
        lines.append(f"  {' · '.join(bits)}")
        lines.append(f"  Apply: {j['url']}")
        lines.append("")
    lines.append("You're receiving this because you saved an alert on OpenTechJobs.")
    lines.append(f"Manage, pause or delete it: {SITE_ORIGIN}/account")
    return "\n".join(lines)


# The site's own light-mode tokens (frontend/style.css, DESIGN.md), as
# literal values because an email client has no CSS variables, and the
# light ones because a client renders in its own chrome and never sees
# the site's theme toggle. --black for ink, --green-text for the one
# colour that means "click here", --green for the accent rule, --paper
# and --grey-line for surfaces and dividers, --row-selected for the
# summary panel's tint.
_INK = "#40513b"
_LINK = "#3f6f45"
_ACCENT = "#609966"
_PAPER = "#f2f0ef"
_LINE = "#d2cfcb"
_PANEL = "#dce8d4"
_WHITE = "#ffffff"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _row_html(j: dict, now: datetime) -> str:
    """One listing: title as the link, one line of who and where, one
    line of when, level, workplace, pay, and Apply. Left green accent
    and a neutral divider instead of a box around each."""
    esc = html.escape
    rtl = _is_rtl(j.get("title") or "")
    title_dir = ' dir="rtl"' if rtl else ""
    title_align = "right" if rtl else "left"
    bits = [esc(_age(j, now))]
    if j.get("seniority"):
        bits.append(esc(_SENIORITY.get(j["seniority"], j["seniority"])))
    if j.get("workplace_type"):
        bits.append(esc(_WORKPLACE.get(j["workplace_type"], j["workplace_type"])))
    sal = _salary(j)
    if sal:
        bits.append(f'<span title="A market estimate, not the employer\'s figure">Est. {esc(sal[0])}</span>' if sal[1] else esc(sal[0]))
    who = " &middot; ".join(esc(x) for x in (_company(j), j.get("location") or "Location unknown") if x)
    return f"""
          <tr>
            <td style="padding:0 0 10px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_WHITE}; border-bottom:1px solid {_LINE};">
                <tr>
                  <td width="4" style="width:4px; background:{_ACCENT};"></td>
                  <td style="padding:12px 14px 12px 14px;">
                    <div{title_dir} style="text-align:{title_align};">
                      <a href="{esc(j['url'])}"{title_dir} style="font-family:{_FONT}; font-size:16px; line-height:1.3; font-weight:700; color:{_LINK}; text-decoration:none;">{esc(j['title'])}</a>
                    </div>
                    <div dir="ltr" style="font-family:{_FONT}; font-size:13px; line-height:1.4; color:{_INK}; margin-top:4px;">{who}</div>
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">
                      <tr>
                        <td dir="ltr" style="font-family:{_FONT}; font-size:12px; line-height:1.4; color:{_INK};">{" &middot; ".join(bits)}</td>
                        <td dir="ltr" align="right" style="font-family:{_FONT}; font-size:13px; white-space:nowrap; padding-left:12px;">
                          <a href="{esc(j['url'])}" style="color:{_LINK}; font-weight:700; text-decoration:none;">Apply &rarr;</a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""


def _digest_html(n: int, matches: list[dict], alert: dict | None = None, now: datetime | None = None) -> str:
    """The digest as a compact branded page: header strip, a summary
    block that says the number and the filter, the listings, one call to
    action back to the board with the same filters, and a footer that
    says why this arrived and where to stop it.

    Tables and inline styles throughout, a system font stack, no images
    and nothing external: the mark is a green square drawn by a table
    cell, so the header looks like the site in a client that blocks
    remote content, which is most of them by default. Fixed at 600px.
    The dark-mode rule at the top is an enhancement for the clients that
    honour it; the light values are the contract.
    """
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    alert = alert or {}
    view_all = board_url(alert)
    summary = _filter_summary(alert) + ["since your last alert"]
    rows = "".join(_row_html(j, now) for j in matches)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="color-scheme" content="light dark" />
  <title>{n} new role{"s" if n != 1 else ""} on OpenTechJobs</title>
  <style>
    @media (prefers-color-scheme: dark) {{
      .otj-paper {{ background: #262421 !important; }}
      .otj-card {{ background: #2f2d2a !important; border-color: #3d3a36 !important; }}
      .otj-ink {{ color: #e9e6e2 !important; }}
      .otj-link {{ color: #9fd4a5 !important; }}
      .otj-panel {{ background: #33402f !important; }}
    }}
  </style>
</head>
<body class="otj-paper" style="margin:0; padding:0; background:{_PAPER};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="otj-paper" style="background:{_PAPER};">
    <tr>
      <td align="center" style="padding:24px 12px 32px 12px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px;">

          <tr>
            <td style="background:{_INK}; padding:14px 18px; border-bottom:3px solid {_ACCENT};">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="22" height="22" style="width:22px; height:22px; background:{_ACCENT}; border-radius:6px; font-size:0; line-height:0;">&nbsp;</td>
                  <td style="padding-left:10px; font-family:{_FONT}; font-size:16px; font-weight:700; color:{_PAPER}; letter-spacing:0.2px;">OpenTechJobs</td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td class="otj-panel" style="background:{_PANEL}; padding:22px 20px 20px 20px;">
              <div class="otj-ink" style="font-family:{_FONT}; font-size:28px; line-height:1.15; font-weight:700; color:{_INK};">{n} new role{"s" if n != 1 else ""}</div>
              <div class="otj-ink" style="font-family:{_FONT}; font-size:16px; line-height:1.3; color:{_INK}; margin-top:2px;">matching your alert</div>
              <div class="otj-ink" style="font-family:{_FONT}; font-size:13px; line-height:1.4; color:{_INK}; margin-top:10px;">{" &middot; ".join(esc(x) for x in summary)}</div>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:14px;">
                <tr>
                  <td style="background:{_INK}; border-radius:4px;">
                    <a href="{esc(view_all)}" style="display:inline-block; padding:10px 16px; font-family:{_FONT}; font-size:14px; font-weight:700; color:{_PAPER}; text-decoration:none;">View all matches &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:16px 0 0 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}
              </table>
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:10px 0 4px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:{_INK}; border-radius:4px;">
                    <a href="{esc(view_all)}" style="display:inline-block; padding:12px 20px; font-family:{_FONT}; font-size:14px; font-weight:700; color:{_PAPER}; text-decoration:none;">View all {n} listing{"s" if n != 1 else ""} &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:24px 4px 0 4px; border-top:1px solid {_LINE}; margin-top:20px;">
              <div class="otj-ink" style="font-family:{_FONT}; font-size:12px; line-height:1.5; color:{_INK};">
                You're receiving this because you saved an alert on OpenTechJobs.
              </div>
              <div style="font-family:{_FONT}; font-size:12px; line-height:1.5; margin-top:6px;">
                <a href="{SITE_ORIGIN}/account" class="otj-link" style="color:{_LINK}; text-decoration:underline;">Manage alert</a>
                <span class="otj-ink" style="color:{_INK};">&nbsp;&middot;&nbsp;</span>
                <a href="{SITE_ORIGIN}/account" class="otj-link" style="color:{_LINK}; text-decoration:underline;">Pause alert</a>
                <span class="otj-ink" style="color:{_INK};">&nbsp;&middot;&nbsp;</span>
                <a href="{SITE_ORIGIN}/account" class="otj-link" style="color:{_LINK}; text-decoration:underline;">Unsubscribe</a>
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
