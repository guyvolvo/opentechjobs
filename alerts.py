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
    return {"alerts_checked": len(alerts), "digests_sent": sent, "errors": errors,
            "watched_domains": sorted(_watched_domains(alerts))}


def _watched_domains(alerts: list[dict]) -> set[str]:
    """The company domains somebody is actually waiting on.

    A board nobody follows can sit at the four-hour ceiling without
    anyone noticing. A board with an alert on it cannot: the reader is
    waiting for exactly the posting that ceiling delays. The sweep gives
    these a much lower ceiling of their own (see loader/scrape_state.py).

    Only alerts that name a company count. An alert on "python in
    Israel" follows no particular board, and treating it as though it
    followed all ten thousand would empty the idea of meaning.
    """
    out: set[str] = set()
    for alert in alerts:
        raw = (alert.get("filter") or {}).get("company")
        if not raw:
            continue
        # Same ',' convention build_jobs_where reads it with.
        for part in str(raw).split(","):
            part = part.strip().lower()
            if part:
                out.add(part)
    return out


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
    try:
        has_logo = any(r[1] == "logo_url" for r in conn.execute("PRAGMA table_info(companies)"))
    except sqlite3.Error:
        has_logo = False
    logo_sql = ("(SELECT logo_url FROM companies WHERE domain = jobs.company_domain) AS logo_url"
                if has_logo else "NULL AS logo_url")
    rows = conn.execute(
        f"SELECT id, title, company_domain, {name_sql}, {logo_sql}, location, url, posted_at, first_seen, "
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
        meta = [_company(j), _place(j, alert)]
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


def _place(job: dict, alert: dict | None = None) -> str:
    """Where, compactly. A listing posted in ten offices at once is one
    line on the board and a wall in a mail. The place shown is the one
    the alert asked about when there is one, else the first, with the
    rest counted: "Tel Aviv, Israel + 9 locations"."""
    raw = (job.get("location") or "").strip()
    if not raw:
        return "Location unknown"
    places = [x.strip() for x in raw.split(";") if x.strip()]
    if len(places) <= 1:
        return raw
    want = ""
    f = (alert or {}).get("filter") or {}
    codes = str(f.get("country") or ("IL" if f.get("israel_only") else "")).split(",")
    labels = [label_for(c).lower() for c in codes if c]
    for pl in places:
        if any(lbl and lbl in pl.lower() for lbl in labels):
            want = pl
            break
    want = want or places[0]
    rest = len(places) - 1
    return f"{want} + {rest} location{'s' if rest != 1 else ''}"


# DESIGN.md's palette, both modes, as literal values because an email
# client has no CSS variables. Paper is the only surface: DESIGN.md
# says it is never a panel colour and the site draws its cards with
# hairlines rather than fills, so the mail does the same. Green is
# Signal Green and Green Text, and it goes on exactly what it means:
# the Apply buttons and the links back to the board, and nowhere else:
# the header rule is ink, the footer links are ink.
# Titles and metadata are ink. The logo band tokens are the site's own
# treatment for a company mark on paper.
_PAPER = "#f2f0ef"
_INK = "#40513b"
_GREEN = "#609966"
_LINK = "#3f6f45"
_LINE = "#d2cfcb"
_LOGO_BAND = "#edf0ec"
_LOGO_RULE = "#d6ded6"
_DARK_PAPER = "#17181c"
_DARK_INK = "#ededec"
_DARK_GREEN = "#2fae60"
_DARK_GREY = "#9a9a9a"
_DARK_LINE = "#2b2c31"
_DARK_BAND = "#23252b"
# The Apply button is the board's own: Green Text fill with paper text
# in light mode, and in dark mode the site's dark Signal Green with the
# dark row tint as its text, which is the button as it appears there.
_BTN_TEXT = _PAPER
_DARK_BTN_TEXT = "#23252b"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
_MARK = f"{SITE_ORIGIN}/favicon-32.png"


def _logo_cell(job: dict) -> str:
    """The company's mark in the site's logo band, the band running the
    full height of the listing so the mark sits centred beside all three
    lines. 44px inside a 52px band; a lettered square of the same size
    when there is no logo, so every row lines up. No rule around the
    band: it boxed the mark in, and the tint alone holds it."""
    esc = html.escape
    band = f"width:52px; background:{_LOGO_BAND}; vertical-align:middle;"
    if job.get("logo_url"):
        return (f'<td width="52" align="center" valign="middle" class="otj-band" style="{band}">'
                f'<img src="{esc(job["logo_url"])}" width="44" height="44" alt="" '
                f'style="display:block; width:44px; height:44px; border:0;" /></td>')
    letter = (_company(job)[:1] or "?").upper()
    return (f'<td width="52" align="center" valign="middle" class="otj-band otj-ink" style="{band} '
            f'font-family:{_FONT}; font-size:18px; font-weight:700; color:{_INK};">{esc(letter)}</td>')


def _row_html(j: dict, now: datetime, alert: dict | None = None) -> str:
    """One listing: mark, title as the link in ink, who and where on one
    line, when and what on the next, Apply in green at the end. A
    hairline under each, nothing around it."""
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
    who = " &middot; ".join(esc(x) for x in (_company(j), _place(j, alert)) if x)
    return f"""
          <tr>
            <td style="padding:12px 0 12px 0; border-bottom:1px solid {_LINE};" class="otj-line">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  {_logo_cell(j)}
                  <td style="padding-left:12px; vertical-align:top;">
                    <div{title_dir} style="text-align:{title_align};">
                      <a href="{esc(j['url'])}"{title_dir} class="otj-ink" style="font-family:{_FONT}; font-size:15px; line-height:1.3; font-weight:700; color:{_INK}; text-decoration:none;">{esc(j['title'])}</a>
                    </div>
                    <div dir="ltr" class="otj-ink" style="font-family:{_FONT}; font-size:13px; line-height:1.4; color:{_INK}; margin-top:3px;">{who}</div>
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:4px;">
                      <tr>
                        <td dir="ltr" class="otj-grey" style="font-family:{_FONT}; font-size:12px; line-height:1.4; color:{_INK};">{" &middot; ".join(bits)}</td>
                        <td dir="ltr" align="right" style="white-space:nowrap; padding-left:12px;">
                          <table role="presentation" cellpadding="0" cellspacing="0" align="right"><tr>
                            <td class="otj-btn" style="background:{_LINK}; border-radius:4px;">
                              <a href="{esc(j['url'])}" class="otj-btn-text" style="display:inline-block; padding:7px 12px; font-family:{_FONT}; font-size:13px; line-height:1; font-weight:700; color:{_BTN_TEXT}; text-decoration:none; white-space:nowrap;">Apply &#8599;</a>
                            </td>
                          </tr></table>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""


def _digest_html(n: int, matches: list[dict], alert: dict | None = None, now: datetime | None = None) -> str:
    """The digest as a quiet page in the site's own colours: a compact
    header with the mark and a green rule, one line saying how many and
    for which alert, the listings under hairlines, a link back to the
    board with the same filters, and a footer that says why this
    arrived and where to stop it.

    Tables and inline styles, system fonts, one image (the site's own
    favicon, with the band behind it if a client blocks it), fixed at
    600px. The dark-mode rule carries DESIGN.md's dark tokens and is an
    enhancement for clients that honour it; the light values are the
    contract.
    """
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    alert = alert or {}
    view_all = board_url(alert)
    summary = _filter_summary(alert) + ["since your last alert"]
    rows = "".join(_row_html(j, now, alert) for j in matches)
    roles = f"{n} new role{'s' if n != 1 else ''}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="color-scheme" content="light dark" />
  <title>{roles} on OpenTechJobs</title>
  <style>
    @media (prefers-color-scheme: dark) {{
      .otj-paper {{ background: {_DARK_PAPER} !important; }}
      .otj-ink {{ color: {_DARK_INK} !important; }}
      .otj-grey {{ color: {_DARK_GREY} !important; }}
      .otj-link {{ color: {_DARK_GREEN} !important; }}
      .otj-rule {{ border-bottom-color: {_DARK_INK} !important; }}
      .otj-btn {{ background: {_DARK_GREEN} !important; }}
      .otj-btn-text {{ color: {_DARK_BTN_TEXT} !important; }}
      .otj-line {{ border-bottom-color: {_DARK_LINE} !important; }}
      .otj-band {{ background: {_DARK_BAND} !important; }}
    }}
  </style>
</head>
<body class="otj-paper" style="margin:0; padding:0; background:{_PAPER};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="otj-paper" style="background:{_PAPER};">
    <tr>
      <td align="center" style="padding:24px 12px 32px 12px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px;">

          <tr>
            <td class="otj-rule" style="padding:0 0 10px 0; border-bottom:2px solid {_INK};">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="24" class="otj-band" style="width:24px; height:24px; background:{_LOGO_BAND};">
                    <img src="{_MARK}" width="24" height="24" alt="" style="display:block; width:24px; height:24px; border:0;" />
                  </td>
                  <td class="otj-ink" style="padding-left:8px; font-family:{_FONT}; font-size:15px; font-weight:700; color:{_INK};">OpenTechJobs</td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td class="otj-line" style="padding:18px 0 16px 0; border-bottom:1px solid {_LINE};">
              <div class="otj-ink" style="font-family:{_FONT}; font-size:18px; line-height:1.3; font-weight:700; color:{_INK};">{roles} matching your alert</div>
              <div class="otj-grey" style="font-family:{_FONT}; font-size:13px; line-height:1.5; color:{_INK}; margin-top:4px;">{" &middot; ".join(esc(x) for x in summary)}</div>
              <div style="font-family:{_FONT}; font-size:14px; line-height:1.5; margin-top:10px;">
                <a href="{esc(view_all)}" class="otj-link" style="color:{_LINK}; font-weight:700; text-decoration:none;">View all {n} match{"es" if n != 1 else ""} &rarr;</a>
              </div>
            </td>
          </tr>

          <tr>
            <td>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:16px 0 0 0; font-family:{_FONT}; font-size:14px; line-height:1.5;">
              <a href="{esc(view_all)}" class="otj-link" style="color:{_LINK}; font-weight:700; text-decoration:none;">View all matches &rarr;</a>
            </td>
          </tr>

          <tr>
            <td class="otj-line" style="padding:20px 0 0 0; border-top:1px solid {_LINE}; margin-top:20px;">
              <div class="otj-grey" style="font-family:{_FONT}; font-size:12px; line-height:1.5; color:{_INK};">
                You're receiving this because you saved an alert on OpenTechJobs.
              </div>
              <div style="font-family:{_FONT}; font-size:12px; line-height:1.5; margin-top:6px;">
                <a href="{SITE_ORIGIN}/account" class="otj-ink" style="color:{_INK}; text-decoration:underline;">Manage alert</a>
                <span class="otj-grey" style="color:{_INK};">&nbsp;&middot;&nbsp;</span>
                <a href="{SITE_ORIGIN}/account" class="otj-ink" style="color:{_INK}; text-decoration:underline;">Pause alert</a>
                <span class="otj-grey" style="color:{_INK};">&nbsp;&middot;&nbsp;</span>
                <a href="{SITE_ORIGIN}/account" class="otj-ink" style="color:{_INK}; text-decoration:underline;">Unsubscribe</a>
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
