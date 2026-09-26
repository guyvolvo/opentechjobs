"""Alert evaluation: matches each active alert's filter against jobs
first-seen since its own watermark, sends one digest email per alert
with any new matches, advances the watermark. Called once per fast-poll
cycle (scrape_handler.py, right after the loader step, while jobs.db is
already fresh on /tmp -- no separate download needed here).

The owner's profile says how often: instant is every pass, daily and
weekly hold the watermark until the digest is due, at the owner's own
time of day in their own zone, so the matches pile up behind it and go
out as one email (digest_due below).

Uses job_filters.build_jobs_where() for the actual matching, the same
function /api/jobs itself uses (api/handler.py) -- an alert matches
exactly what its owner would see applying those filters on the live
board, not a second, independently-drifting approximation of it.
"""

import html
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import boto3
from boto3.dynamodb.conditions import Attr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from countries import label_for
from job_filters import build_jobs_where, has_fts_index, has_places, register_functions, salary_source_select
from profile import DIGEST_DAY, DIGEST_TIME, DIGEST_TZ, PROFILE_ID

ALERTS_TABLE = os.environ.get("ALERTS_TABLE")
FROM_EMAIL = os.environ.get("ALERTS_FROM_EMAIL", "alerts@guyvoloshin.com")
SITE_ORIGIN = os.environ.get("SITE_ORIGIN", "https://oceanofjobs.com")

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

    profiles = _profiles(table, {a["user_id"] for a in alerts})
    now = datetime.now(timezone.utc)
    sent = 0
    held = 0
    errors = []
    for alert in alerts:
        try:
            prof = profiles.get(alert["user_id"]) or {}
            cadence = prof.get("cadence") or "instant"
            matches = _find_new_matches(conn, alert)
            # Before the first digest, the alert's own creation is the
            # last moment, so a daily alert made at ten waits for
            # tomorrow's nine rather than sending in the first pass.
            if matches and not digest_due(cadence, alert.get("last_digest_at") or alert.get("created_at"), now,
                                          at=prof.get("digest_time"), tz=prof.get("digest_tz"),
                                          day=prof.get("digest_day")):
                # The watermark stays where it is, so these are in the
                # digest when it is due, with whatever arrives meanwhile.
                held += 1
                continue
            update = "SET last_notified_at = :t"
            if matches:
                _send_digest(alert, matches)
                sent += 1
                update += ", last_digest_at = :t"
            table.update_item(
                Key={"user_id": alert["user_id"], "alert_id": alert["alert_id"]},
                UpdateExpression=update,
                ExpressionAttributeValues={":t": now.isoformat()},
            )
        except Exception as e:
            # One user's bad filter or bounced address shouldn't stop
            # every other alert from being checked.
            errors.append(f"{alert['user_id']}/{alert['alert_id']}: {e}")

    watched = _watched_domains(alerts) | _recently_matching_domains(conn, alerts)
    conn.close()
    return {"alerts_checked": len(alerts), "digests_sent": sent, "digests_held": held, "errors": errors,
            "watched_domains": sorted(watched)}


def digest_due(cadence: str, since, now: datetime, at=None, tz=None, day=None) -> bool:
    """Whether an alert with matches waiting should send now.

    Instant always. Daily and weekly: find the most recent scheduled
    moment at or before now, in the owner's zone (today at `at`, or the
    last `day` at `at`), and send if nothing has gone out since it.
    `since` is the last digest, or the alert's creation before there
    was one. The evaluator runs every half minute, so this fires on the
    first pass after the moment and then not again until the next one.
    A zone or a time the profile could not have stored still falls back
    to the defaults rather than to never.
    """
    if cadence not in ("daily", "weekly"):
        return True
    try:
        zone = ZoneInfo(tz or DIGEST_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = ZoneInfo(DIGEST_TZ)
    try:
        hh, mm = (int(x) for x in str(at or DIGEST_TIME).split(":", 1))
    except ValueError:
        hh, mm = (int(x) for x in DIGEST_TIME.split(":"))
    try:
        weekday = int(day if day is not None else DIGEST_DAY) % 7
    except (TypeError, ValueError):
        weekday = DIGEST_DAY
    local = now.astimezone(zone)
    scheduled = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cadence == "weekly":
        scheduled -= timedelta(days=(local.weekday() - weekday) % 7)
    if scheduled > local:
        scheduled -= timedelta(days=7 if cadence == "weekly" else 1)
    last = _parse(since)
    return last is None or last < scheduled


def _profiles(table, user_ids) -> dict[str, dict]:
    """The profile row of each owner, for its cadence. One get per
    owner rather than a scan: the active-alert scan cannot see profile
    rows (they carry no `active`), and owners are few."""
    out = {}
    for uid in user_ids:
        try:
            item = table.get_item(Key={"user_id": uid, "alert_id": PROFILE_ID}).get("Item")
        except Exception:  # noqa: BLE001 -- a missing profile is instant, the default
            item = None
        if item:
            out[uid] = item
    return out


# How far back a match counts as evidence that a board is worth watching.
#
# A board that answered somebody's alert in the last two weeks is a board
# that plausibly answers it again. Shorter and a company that posts
# monthly falls out of the fast lane between postings, which is the exact
# case this exists for.
WATCH_LOOKBACK_DAYS = 14

# Per alert, not in total. A country-wide alert matches tens of thousands
# of rows and there is no point reading them all: the fast lane is meant
# to cover the boards a reader actually hears from, and past a couple of
# hundred companies it stops being a lane and becomes the whole road.
WATCH_DOMAINS_PER_ALERT = 200


def _recently_matching_domains(conn: sqlite3.Connection, alerts: list[dict]) -> set[str]:
    """Boards that have answered somebody's alert lately.

    Reported live on 2026-09-21: a ScaleOps posting reached its reader 72
    minutes late, because ScaleOps posts rarely and had backed off to the
    four-hour ceiling. Naming the company in the alert would have fixed
    it, except that nobody does. Every active alert on the board that day
    filtered by keyword or by country, so an alert-follows-a-company rule
    covered none of them.

    This is the version that covers them. An alert for "DevOps in Israel"
    does not name a board, but the boards that answered it last fortnight
    are the boards it will most likely be answered by next, and those are
    worth polling often. Costs one bounded query per alert against a
    snapshot that is already open.
    """
    out: set[str] = set()
    since = (datetime.now(timezone.utc) - timedelta(days=WATCH_LOOKBACK_DAYS)).isoformat()
    for alert in alerts:
        try:
            where_sql, args = build_jobs_where(dict(alert.get("filter") or {}),
                                               has_fts_index(conn), has_places(conn))
            rows = conn.execute(
                f"SELECT DISTINCT company_domain FROM jobs WHERE {where_sql} AND first_seen > ? "
                f"LIMIT {WATCH_DOMAINS_PER_ALERT}", [*args, since]).fetchall()
            out.update(str(r[0]).lower() for r in rows if r[0])
        except Exception as e:
            # One unreadable filter must not cost every other alert its
            # fast lane. The worst case here is the old schedule.
            print(f"watch scan failed for {alert.get('alert_id')}: {e!r}")
    return out


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
    subject = digest_subject(alert, matches)

    _ses.send_email(
        # A named sender. The inbox list shows the name where LinkedIn's
        # shows "LinkedIn Job Alerts", and the subject no longer has to
        # say where the mail came from, which leaves it free to say what
        # is in it.
        FromEmailAddress=FROM_EMAIL if "<" in FROM_EMAIL else f"Ocean of Jobs <{FROM_EMAIL}>",
        Destination={"ToAddresses": [to_email]},
        Content={
            "Simple": {
                "Subject": {"Data": subject},
                # Gmail and Apple Mail put an Unsubscribe control beside
                # the sender when this is present, which is the control
                # people actually reach for. It points at the account
                # page rather than a one-click endpoint: List-Unsubscribe
                # -Post needs a route that unsubscribes without a session
                # and that route does not exist yet.
                "Headers": [{"Name": "List-Unsubscribe", "Value": f"<{SITE_ORIGIN}/account>"}],
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
    shown = matches[:ROWS_SHOWN]
    lines = [f'{n} new job{"s" if n != 1 else ""} for "{alert_name(alert)}"', "Since your last alert", ""]
    for j in shown:
        lines.append(j["title"])
        lines.append("  " + " · ".join(x for x in (_company(j), _place(j, alert)) if x))
        sal = _salary(j)
        bits = ([("Est. " if sal[1] else "") + sal[0]] if sal else []) + [_age(j, now).replace("Posted ", "")]
        lines.append("  " + " · ".join(bits))
        lines.append("  " + j["url"])
        lines.append("")
    lines.append(f'{"See all " + str(n) + " jobs" if n > len(shown) else "See all jobs"}: {board_url(alert)}')
    lines.append("")
    lines.append(f"Edit alert or unsubscribe: {SITE_ORIGIN}/account")
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


# DESIGN.md's palette as literal values, because an email client has no
# CSS variables. Paper is the only surface. The mail carries no mark and
# no wordmark: the sender line already says who it is from, and a logo
# plus a rule was 80px of the first screen saying nothing.
_PAPER = "#f2f0ef"
_CARD = "#ffffff"
_INK = "#40513b"
_LINK = "#3f6f45"
_LINE = "#d2cfcb"
_BTN_TEXT = _PAPER
# Arial, not a system stack: Outlook on Windows resolves an unknown first
# family to Times, and the stack that starts with -apple-system did
# exactly that. No web fonts.
_FONT = "Arial,Helvetica,sans-serif"

# Five, then the button carries the rest. A digest is a nudge to open the
# board, not the board.
ROWS_SHOWN = 5


def digest_subject(alert: dict | None, matches: list[dict]) -> str:
    """The newest listing, named, the way LinkedIn's alerts do it.

    "DevOps Engineer at Silverfort" for one. Two are both named, since
    "and 1 more" hides half the mail behind a number. Three or more name
    the first and count the rest. The count and the alert's own name,
    which the subject used to carry, moved to the preheader: the inbox
    list shows that right after the subject, so nothing is lost and the
    subject gets to lead with a job rather than with arithmetic.

    Matches arrive newest first (ORDER BY first_seen DESC), so the first
    one is the one that just appeared, which is the one worth the line.
    """
    def named(j):
        company = _company(j)
        return f"{j['title']} at {company}" if company else j["title"]

    n = len(matches)
    if n == 0:
        return f'No new jobs for "{alert_name(alert)}"'
    if n == 1:
        return named(matches[0])
    if n == 2:
        return f"{named(matches[0])} and {named(matches[1])}"
    return f"{named(matches[0])} and {n - 1} more new jobs"


def alert_name(alert: dict | None) -> str:
    """What this alert is called, in the reader's terms. Alerts have no
    name field, so it is the first thing the filter summary says, which
    is the country or city they picked. "your alert" when they picked
    nothing, which reads correctly in the subject line too."""
    parts = _filter_summary(alert or {})
    return parts[0] if parts else "your alert"


def _logo_cell(job: dict) -> str:
    """A 48px tile on white with a hairline, the same treatment the board
    gives a company mark. A lettered tile at the same size when there is
    no logo, so every row lines up whether the image loads or not."""
    esc = html.escape
    tile = (f"width:48px; height:48px; background:#ffffff; border:1px solid {_LINE}; "
            f"border-radius:4px;")
    if job.get("logo_url"):
        return (f'<td width="48" align="center" valign="middle" style="{tile}">'
                f'<img src="{esc(job["logo_url"])}" width="40" height="40" alt="" '
                f'style="display:block; width:40px; height:40px; border:0;" /></td>')
    letter = (_company(job)[:1] or "?").upper()
    return (f'<td width="48" align="center" valign="middle" style="{tile} '
            f'font-family:{_FONT}; font-size:18px; font-weight:700; color:{_INK};">{esc(letter)}</td>')


def _no_autolink(text: str) -> str:
    """A bare domain with the dots broken by a zero-width joiner.

    Gmail autolinks anything that looks like a host and paints it its own
    blue, which put a second, wrong-destination link inside a row whose
    whole job is to be one link. The joiner is invisible and copies out
    harmlessly. Only for a domain: a real company name has no dots to
    break, and this would be vandalism on ordinary text."""
    return html.escape(text).replace(".", "⁠.⁠")


def _row_meta(j: dict, now: datetime) -> str:
    """The third line: the estimate when there is one, then the age.
    Never "Undisclosed" -- a digest row has no room to say that a number
    is missing, and the absence says it."""
    sal = _salary(j)
    bits = []
    if sal:
        bits.append(("Est. " if sal[1] else "") + sal[0])
    bits.append(_age(j, now).replace("Posted ", "").replace("just now", "Just now"))
    return " &middot; ".join(html.escape(b) for b in bits)


def _row_html(j: dict, now: datetime, alert: dict | None = None) -> str:
    """One listing. The whole row is the link: title, then who and where,
    then the numbers. dir="auto" on the two text lines, so a Hebrew title
    lays itself out to the right and an English one stays left; the meta
    line is forced ltr, because a salary range and an age are ltr things
    whatever the title above them is."""
    esc = html.escape
    name = _company(j)
    # A domain is the fallback when we have no company name, and it is
    # the only thing here Gmail would try to linkify.
    company = _no_autolink(name) if ("." in name and not j.get("company_name")) else esc(name)
    where = esc(_place(j, alert))
    who = " &middot; ".join(x for x in (company, where) if x)
    link = (f'font-family:{_FONT}; color:{_INK}; text-decoration:none;')
    return f"""
          <tr>
            <td style="padding:16px 0; border-top:1px solid {_LINE};">
              <a href="{esc(j['url'])}" style="{link} display:block;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    {_logo_cell(j)}
                    <td style="padding-left:14px; vertical-align:top;">
                      <div dir="auto" style="font-family:{_FONT}; font-size:16px; line-height:1.3; font-weight:700; color:{_LINK};">{esc(j['title'])}</div>
                      <div dir="auto" style="font-family:{_FONT}; font-size:14px; line-height:1.4; color:{_INK}; padding-top:3px;">{who}</div>
                      <div dir="ltr" style="font-family:{_FONT}; font-size:13px; line-height:1.4; color:{_INK}; opacity:0.75; padding-top:3px;">{_row_meta(j, now)}</div>
                    </td>
                  </tr>
                </table>
              </a>
            </td>
          </tr>"""


def _digest_html(n: int, matches: list[dict], alert: dict | None = None, now: datetime | None = None) -> str:
    """The digest: a headline, the rows, one button, a two-link footer.

    Tables and inline styles throughout, one font stack, no web fonts and
    no CSS beyond a media query that narrows the padding on a phone,
    because that is the subset Gmail, Outlook and Apple Mail all render
    the same way. 600px, 32px of padding, 20px under 480.
    """
    esc = html.escape
    now = now or datetime.now(timezone.utc)
    alert = alert or {}
    shown = matches[:ROWS_SHOWN]
    rows = "".join(_row_html(j, now, alert) for j in shown)
    name = alert_name(alert)
    headline = f'{n} new job{"s" if n != 1 else ""} for &ldquo;{esc(name)}&rdquo;'
    button = f"See all {n} jobs" if n > len(shown) else "See all jobs"
    # Shown by the inbox list as the line after the subject, and by
    # nothing else: hidden, zero-height, and followed by enough blank
    # space that the headline does not get dragged in after it.
    preheader = f'{n} new role{"s" if n != 1 else ""} in {esc(name)}'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{headline}</title>
  <style>
    @media only screen and (max-width: 480px) {{
      .otj-pad {{ padding: 16px !important; }}
      .otj-card-pad {{ padding: 20px !important; }}
      .otj-head {{ font-size: 20px !important; }}
    }}
  </style>
</head>
<body style="margin:0; padding:0; background:{_PAPER};">
  <div style="display:none; font-size:1px; color:{_PAPER}; line-height:1px; max-height:0; max-width:0; opacity:0; overflow:hidden;">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAPER};">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px;">
          <tr>
            <td class="otj-pad" style="padding:32px;">

              <!-- The digest is a card on the paper rather than a column
                   the width of the mail, which read as the message
                   having no edges. The same 10px the board's own boxes
                   carry. Outlook's Word engine squares the corners and
                   keeps the border, which is the right way to degrade. -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CARD}; border:1px solid {_LINE}; border-radius:10px;">
                <tr>
                  <td class="otj-card-pad" style="padding:24px 28px 28px 28px;">

              <div class="otj-head" dir="auto" style="font-family:{_FONT}; font-size:22px; line-height:1.25; font-weight:700; color:{_INK};">{headline}</div>
              <div style="font-family:{_FONT}; font-size:14px; line-height:1.4; color:{_INK}; padding:6px 0 14px 0;">Since your last alert</div>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}
              </table>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td style="border-top:1px solid {_LINE}; font-size:0; line-height:0; height:1px;">&nbsp;</td></tr>
                <tr>
                  <td style="padding-top:20px;">
                    <table role="presentation" cellpadding="0" cellspacing="0">
                      <tr>
                        <td style="background:{_INK}; border-radius:4px;">
                          <a href="{esc(board_url(alert))}" style="display:inline-block; padding:11px 20px; font-family:{_FONT}; font-size:14px; line-height:1; font-weight:700; color:{_BTN_TEXT}; text-decoration:none;">{esc(button)}</a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>

                  </td>
                </tr>
              </table>

              <div style="font-family:{_FONT}; font-size:12px; line-height:1.5; color:{_INK}; opacity:0.8; padding-top:20px;">
                <a href="{SITE_ORIGIN}/account" style="color:{_INK}; text-decoration:underline;">Edit alert</a>
                &nbsp;&middot;&nbsp;
                <a href="{SITE_ORIGIN}/account" style="color:{_INK}; text-decoration:underline;">Unsubscribe</a>
              </div>

            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
