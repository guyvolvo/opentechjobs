#!/usr/bin/env python3
"""
ATS probe v2: discover a company's applicant tracking system by probing the ATS
APIs directly with token candidates derived from the domain. Does not parse
careers pages, so JS-rendered SPAs are not a problem.

Usage:
    python probe2.py --domain wiz.io
    python probe2.py --batch domains.txt --json > resolved.json
    python probe2.py --fetch greenhouse:airbnb
    python probe2.py --selftest

Dependencies: requests, pyyaml (only needed to load companies.yml pins)
"""

import argparse
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

import requests

try:
    import yaml
except ImportError:
    yaml = None

UA = "ats-probe/0.2 (+https://github.com/guyvolvo/REPLACE-ME)"
TIMEOUT = 12
WORKERS = 8
PINS_FILE = Path(__file__).with_name("companies.yml")

COMMON_TLDS = {"com", "io", "ai", "net", "org", "co", "il", "tech", "dev",
               "app", "cloud", "xyz", "me", "inc", "team"}


@dataclass
class Job:
    ats: str
    token: str
    external_id: str
    title: str
    location: str
    url: str
    posted_at: str | None = None
    department: str | None = None
    description_chars: int = 0
    # Cleaned/truncated plain text (see _clean_text) for keyword search --
    # None where the ATS's list endpoint doesn't include description
    # content at all (SmartRecruiters, Comeet, Workday: confirmed live,
    # would need a per-job detail request to get it, too expensive for a
    # batch scrape of this size).
    description: str | None = None
    # 'intern'|'junior'|'mid'|'senior'|'staff'|'principal'|'lead'|
    # 'manager'|'director'|'exec'|None (no signal found). Set directly by
    # a fetcher
    # when the ATS provides a structured level field (SmartRecruiters'
    # experienceLevel, Comeet's experience_level -- both ground-truthed
    # live, see their fetchers); resolve() fills in a title-keyword guess
    # via _classify_seniority() for everything else. Most postings simply
    # don't state a level -- None is the honest, expected common case,
    # not a bug.
    seniority: str | None = None
    # 'remote'|'hybrid'|'onsite'|None (no signal). Set directly by a
    # fetcher when the ATS provides a structured field (Ashby/Lever's
    # workplaceType, SmartRecruiters' location.remote+hybrid booleans,
    # Recruitee's remote+hybrid+on_site booleans, Comeet's Remote field --
    # all ground-truthed live, see their fetchers); resolve() fills in a
    # location-text guess via _classify_workplace() for everything else
    # (Greenhouse has no structured field at all -- confirmed live,
    # metadata is null -- but commonly puts "Remote"/"Hybrid" directly in
    # the location string, e.g. "United States - Remote"). None is a
    # real, common outcome, not a bug: plenty of postings just don't say.
    workplace_type: str | None = None


@dataclass
class Resolution:
    domain: str
    ats: str | None = None
    token: str | None = None
    job_count: int = 0
    tried: int = 0
    error: str | None = None
    jobs: list[Job] = field(default_factory=list)


VERBOSE = False
SCRAPE_COMEET = True
SCRAPE_EMBED = True


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    a = requests.adapters.HTTPAdapter(pool_connections=WORKERS * 2,
                                      pool_maxsize=WORKERS * 2)
    s.mount("https://", a)
    return s


def get_json(sess: requests.Session, url: str) -> Any:
    """GET returning parsed JSON, or None if the response is not usable.

    Every None exit is logged to stderr (when VERBOSE) with the reason, so a
    batch run doesn't just report MISS with no way to tell a real 404 apart
    from a UA block, a timeout, or an ATS that quietly changed its response
    shape.

    One retry on connection-level failures (timeout, DNS, reset): observed
    live during batch testing that a single dropped request to the *correct*
    ATS endpoint let resolve() fall back and silently report a coincidental
    empty match on a different ATS instead. A 404/wrong-shape is a real
    signal and is not retried; only exceptions are, since those mean we
    don't actually know what the server would have said.
    """
    return _request_json(lambda: sess.get(url, timeout=TIMEOUT), url, "get_json")


def get_json_post(sess: requests.Session, url: str, body: dict) -> Any:
    """POST variant of get_json -- Workday's CXS API takes its search
    params (appliedFacets/limit/offset/searchText) as a JSON body, not
    query string. Same retry/logging/content-type discipline throughout.
    """
    return _request_json(lambda: sess.post(url, json=body, timeout=TIMEOUT), url, "get_json_post")


def _request_json(do_request, url: str, label: str) -> Any:
    for attempt in (1, 2):
        try:
            r = do_request()
            break
        except requests.RequestException as e:
            if attempt == 2:
                if VERBOSE:
                    print(f"    [{label}] {url} -> exception after retry: {e!r}",
                          file=sys.stderr)
                return None
            if VERBOSE:
                print(f"    [{label}] {url} -> exception, retrying once: {e!r}",
                      file=sys.stderr)
            time.sleep(0.5)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200:
        if VERBOSE:
            print(f"    [{label}] {url} -> status={r.status_code} type={ctype!r}",
                  file=sys.stderr)
        return None
    if "json" not in ctype.lower():
        if VERBOSE:
            print(f"    [{label}] {url} -> status=200 but non-json type={ctype!r}",
                  file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError as e:
        if VERBOSE:
            print(f"    [{label}] {url} -> status=200 json={ctype!r} but "
                  f"body didn't parse: {e!r}", file=sys.stderr)
        return None


def token_candidates(domain: str) -> list[str]:
    host = re.sub(r"^(https?://)?(www\.)?", "", domain.strip().lower()).strip("/")
    parts = host.split(".")
    stem, rest = parts[0], parts[1:]
    out = [stem, stem.replace("-", "")]
    if rest and rest[0] not in COMMON_TLDS:
        out += [stem + rest[0], stem + "-" + rest[0]]
    # Ground-truthed against wiz.io: its actual Greenhouse token is
    # "wizinc", not "wiz" or any dash/tld variant above. US-incorporated
    # Israeli companies commonly register the ATS token under the legal
    # entity name. Cheap to add, so add it, but keep it last (lowest prior).
    out.append(stem + "inc")
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# --------------------------------------------------------------------------
# fetchers: each returns a list[Job], or None if the token does not exist
# --------------------------------------------------------------------------

def _txt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "text", "label", "location"):
            if k in v:
                return _txt(v[k])
        return ""
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v if x)
    return str(v)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
DESCRIPTION_MAX_CHARS = 8000


def _clean_text(raw: Any, max_chars: int = DESCRIPTION_MAX_CHARS) -> str | None:
    """Raw description field -> plain, searchable text, or None.

    Some ATSes hand back HTML (Greenhouse's `content`, most likely
    Workable/Recruitee too), others already give a plain-text sibling
    field (Lever/Ashby's `descriptionPlain`) -- stripping tags either way
    is harmless on already-plain text and necessary on HTML, so every
    caller runs through this rather than branching per-ATS.

    Truncated to max_chars -- measured against a real ~3000-job batch
    before picking the number: 4000 sounded reasonable but actually
    truncated 58% of descriptions (median real length is ~5700 raw HTML
    chars, well above that); 8000 cuts that to 6% truncated for 27% more
    total jobs.db size (31MB vs 24.5MB in that same batch). jobs.db gets
    downloaded on every Lambda cold start (see api/db.py), so this isn't
    free, but keyword search silently missing a term that's genuinely in
    the posting -- because it happened to fall past a too-eager cutoff --
    would defeat the feature far more than an extra ~7MB costs.
    """
    text = _txt(raw)
    if not text:
        return None
    # Greenhouse's `content` (ground-truthed live) is HTML with its own
    # tags entity-escaped -- "&lt;h2&gt;...&lt;/h2&gt;", not "<h2>...".
    # Unescaping BEFORE stripping tags is required, not cosmetic: stripping
    # first finds nothing to strip (no literal "<" yet), then unescaping
    # reveals the tags with nothing left to remove them. Unescape again
    # after stripping to catch entities that were sitting in the visible
    # text itself (e.g. "&amp;" -> "&"), not just in now-removed tags.
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars] or None


# Checked top-to-bottom, first match wins -- ordered highest seniority
# signal first so e.g. "VP of Engineering" hits exec before anything
# looser could claim it, and "Senior Staff Engineer" hits staff (the
# senior-er of the two) rather than senior. \b word-boundary matching,
# not substring -- a naive "intern" in title.lower() check would
# misclassify "International Account Executive" as an internship.
_SENIORITY_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    (level, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for level, needles in [
        ("exec", ["chief", "cto", "ceo", "cfo", "coo", "vp", "svp", "vice president"]),
        ("director", ["director", "head of"]),
        ("principal", ["principal"]),
        ("staff", ["staff"]),
        ("lead", ["lead", "tech lead", "team lead"]),
        ("manager", ["manager"]),
        ("senior", ["senior", "sr"]),
        ("mid", ["mid-level", "mid level", "midlevel"]),
        ("junior", ["junior", "jr", "entry level", "entry-level", "new grad", "graduate"]),
        ("intern", ["intern", "internship", "co-op", "coop"]),
    ]
]

# Structured level fields, ground-truthed live against real postings (see
# the conversation that added this feature for the exact requests) --
# preferred over the title-keyword guess above when the ATS provides one.
_SMARTRECRUITERS_LEVEL_MAP = {
    "internship": "intern",
    "entry_level": "junior",
    "associate": "junior",
    "mid_senior_level": "senior",
    "director": "director",
    "executive": "exec",
    # "not_applicable" and anything unrecognized -> None, on purpose --
    # don't guess past what SmartRecruiters actually told us.
}

_COMEET_LEVEL_MAP = {
    "junior": "junior",
    "intermediate": "mid",
    "senior": "senior",
    "management": "manager",
    # Comeet's experience_level is company-customizable and occasionally
    # holds an unrelated value (observed "Full-time" -- an employment
    # type, not a level, on at least one live posting) -- unrecognized
    # values fall through to None rather than emitting a wrong signal.
}

# Structured workplace-type fields, ground-truthed live the same way as
# the seniority maps above. Ashby/Lever both use a "workplaceType" field
# (values differently cased -- "Remote"/"Hybrid" vs "remote"/"hybrid" --
# lowercased before lookup so one map covers both). Comeet's "Remote"
# field additionally has an explicit "On-site" value (confirmed live,
# 3 real postings had it) and a large None share that's genuinely
# unspecified, not "therefore onsite" -- don't guess past what's there.
_ATS_WORKPLACE_MAP = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on site": "onsite",
}


def _classify_seniority(title: str | None) -> str | None:
    """Best-effort seniority from title text alone -- the fallback for
    every ATS that doesn't hand back a structured level field (see
    Job.seniority's docstring). Most titles state no level at all, so
    None is the expected common outcome, not a sign this is broken.
    """
    if not title:
        return None
    for level, pattern in _SENIORITY_RULES:
        if pattern.search(title):
            return level
    return None


# Checked in this order -- "remote" wins over "hybrid" if a location
# string somehow says both (hasn't happened in practice, but hybrid is
# the more common false-positive direction: "Hybrid Remote" reads as
# remote-first to a job seeker). word-boundary matching, same reasoning
# as _SENIORITY_RULES: a naive substring check would misfire on stray
# text this fallback has no business guessing from.
_WORKPLACE_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    (level, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for level, needles in [
        ("remote", ["remote", "work from home", "wfh"]),
        ("hybrid", ["hybrid"]),
        ("onsite", ["on-site", "onsite", "on site"]),
    ]
]


def _classify_workplace(location: str | None) -> str | None:
    """Best-effort remote/hybrid/onsite from location text -- the fallback
    for every ATS without a structured workplaceType-style field (see
    Job.workplace_type's docstring). Greenhouse is the big one this
    covers: no structured field at all, but "United States - Remote"
    style locations are common there.
    """
    if not location:
        return None
    for level, pattern in _WORKPLACE_RULES:
        if pattern.search(location):
            return level
    return None


def _normalize_date(v: Any) -> str | None:
    """Every fetcher below should push its posting date through this
    before putting it on a Job, so posted_at is uniformly ISO 8601 no
    matter which ATS it came from.

    Caught two real format quirks by testing actual API responses against
    SQLite's julianday(), which the downstream API uses for age math:
    Lever's createdAt is epoch milliseconds, not a string at all, and
    Recruitee's published_at is "YYYY-MM-DD HH:MM:SS UTC" -- a literal
    trailing "UTC" word, not an offset -- which julianday() silently
    returns NULL for instead of erroring. Both would have quietly broken
    sorting/filtering for exactly those two ATSes with no visible symptom
    short of noticing the output looked wrong. Normalizing once here means
    the next ATS's date-format surprise gets caught in one place instead
    of rediscovered downstream.
    """
    if v is None or v == "":
        return None

    # epoch milliseconds (Lever) -- comes through as int or numeric string
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None

    s = str(v).strip()

    # "YYYY-MM-DD HH:MM:SS UTC" (Recruitee) -- literal suffix, not an offset
    if s.endswith(" UTC"):
        s = s[: -len(" UTC")]
        try:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None

    # standard ISO 8601, including a trailing "Z" (Python's fromisoformat
    # doesn't accept bare "Z" as an offset marker until it's swapped for "+00:00")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        if VERBOSE:
            print(f"    [_normalize_date] unrecognized date format: {v!r}", file=sys.stderr)
        return None


def f_greenhouse(sess, token):
    d = get_json(sess, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    return [Job("greenhouse", token, str(j.get("id")), _txt(j.get("title")),
                _txt(j.get("location")), _txt(j.get("absolute_url")),
                _normalize_date(j.get("updated_at")),
                _txt((j.get("departments") or [{}])[0].get("name")) or None,
                len(_txt(j.get("content"))), _clean_text(j.get("content"))) for j in d["jobs"]]


def f_lever(sess, token):
    d = get_json(sess, f"https://api.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        c = j.get("categories") or {}
        out.append(Job("lever", token, _txt(j.get("id")), _txt(j.get("text")),
                       _txt(c.get("location")), _txt(j.get("hostedUrl")),
                       _normalize_date(j.get("createdAt")), _txt(c.get("team")) or None,
                       len(_txt(j.get("descriptionPlain"))), _clean_text(j.get("descriptionPlain")),
                       workplace_type=_ATS_WORKPLACE_MAP.get(_txt(j.get("workplaceType")).lower()) or None))
    return out


def f_ashby(sess, token):
    d = get_json(sess, f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    return [Job("ashby", token, _txt(j.get("id")), _txt(j.get("title")),
                _txt(j.get("location")), _txt(j.get("jobUrl")),
                _normalize_date(j.get("publishedAt")), _txt(j.get("department")) or None,
                len(_txt(j.get("descriptionPlain"))), _clean_text(j.get("descriptionPlain")),
                workplace_type=_ATS_WORKPLACE_MAP.get(_txt(j.get("workplaceType")).lower()) or None)
            for j in d["jobs"]]


def f_workable(sess, token):
    d = get_json(sess, f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    return [Job("workable", token, _txt(j.get("shortcode")), _txt(j.get("title")),
                ", ".join(x for x in [_txt(j.get("city")), _txt(j.get("country"))] if x),
                _txt(j.get("url")), _normalize_date(j.get("published_on")),
                _txt(j.get("department")) or None,
                len(_txt(j.get("description"))), _clean_text(j.get("description"))) for j in d["jobs"]]


def _recruitee_workplace(j: dict) -> str | None:
    # remote/hybrid/on_site are independent booleans here, not mutually
    # exclusive -- ground-truthed live, a real posting had both hybrid and
    # on_site set True at once (hybrid IS partially onsite, so that's a
    # sensible combination, not a data bug). remote wins if set, then
    # hybrid (the more specific/informative of the two remaining), then
    # on_site.
    if j.get("remote"):
        return "remote"
    if j.get("hybrid"):
        return "hybrid"
    if j.get("on_site"):
        return "onsite"
    return None


def f_recruitee(sess, token):
    d = get_json(sess, f"https://{token}.recruitee.com/api/offers/")
    if not isinstance(d, dict) or "offers" not in d:
        return None
    return [Job("recruitee", token, str(j.get("id")), _txt(j.get("title")),
                _txt(j.get("location")), _txt(j.get("careers_url")),
                _normalize_date(j.get("published_at")), _txt(j.get("department")) or None,
                len(_txt(j.get("description"))), _clean_text(j.get("description")),
                workplace_type=_recruitee_workplace(j)) for j in d["offers"]]


def f_smartrecruiters(sess, token):
    d = get_json(sess, f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100")
    if not isinstance(d, dict) or "content" not in d:
        return None
    # Ground-truthed: this endpoint returns HTTP 200 with an empty
    # `content` list for ANY token, including ones that don't exist
    # (verified against a deliberately bogus slug). There is no
    # existence signal here, only a postings-count signal. Treating an
    # empty result as a "hit with 0 jobs" would make every domain in a
    # batch resolve to smartrecruiters:<guess> once no other ATS
    # matches, silently overwriting real MISSes. So: no postings, no
    # verdict. This costs a rare false negative (a real SmartRecruiters
    # customer with zero open roles right now) to kill a guaranteed
    # false positive on every miss.
    if not d["content"]:
        if VERBOSE:
            print(f"    [f_smartrecruiters] {token} -> 200 empty content, "
                  f"treating as no-match (see comment above)", file=sys.stderr)
        return None
    out = []
    for j in d["content"]:
        loc = j.get("location") or {}
        level = j.get("experienceLevel") or {}
        # SmartRecruiters models this as two independent booleans, not a
        # single field -- ground-truthed live with both explicitly false
        # ("remote": false, "hybrid": false) for a real onsite posting, so
        # that combination IS a confident onsite signal here, unlike an
        # ATS where the field is just absent for onsite roles. Only claim
        # onsite when both are explicitly False, not merely falsy/missing.
        loc_remote, loc_hybrid = loc.get("remote"), loc.get("hybrid")
        if loc_remote:
            workplace = "remote"
        elif loc_hybrid:
            workplace = "hybrid"
        elif loc_remote is False and loc_hybrid is False:
            workplace = "onsite"
        else:
            workplace = None
        out.append(Job("smartrecruiters", token, _txt(j.get("id")), _txt(j.get("name")),
                       ", ".join(x for x in [_txt(loc.get("city")), _txt(loc.get("country"))] if x),
                       _txt(j.get("applyUrl") or j.get("ref")),
                       _normalize_date(j.get("releasedDate")),
                       _txt(j.get("department")) or None,
                       seniority=_SMARTRECRUITERS_LEVEL_MAP.get(_txt(level.get("id")).lower()) or None,
                       workplace_type=workplace))
    return out


def get_xml(sess: requests.Session, url: str) -> "ET.Element | None":
    """Personio's public feed is XML, not JSON -- get_json() only accepts
    a json content-type, so this is the same shape (retry once on
    connection errors, log why on None) for the one XML-based fetcher.
    A nonexistent tenant subdomain 307-redirects to personio.com's own
    marketing site (text/html), so the content-type check alone is
    already a sound existence signal -- confirmed live, no separate
    "empty content" false-positive risk like f_smartrecruiters had.
    """
    for attempt in (1, 2):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            break
        except requests.RequestException as e:
            if attempt == 2:
                if VERBOSE:
                    print(f"    [get_xml] {url} -> exception after retry: {e!r}", file=sys.stderr)
                return None
            time.sleep(0.5)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "xml" not in ctype.lower():
        if VERBOSE:
            print(f"    [get_xml] {url} -> status={r.status_code} type={ctype!r}", file=sys.stderr)
        return None
    try:
        return ET.fromstring(r.content)
    except ET.ParseError as e:
        if VERBOSE:
            print(f"    [get_xml] {url} -> status=200 xml but didn't parse: {e!r}", file=sys.stderr)
        return None


def f_personio(sess, token):
    root = get_xml(sess, f"https://{token}.jobs.personio.de/xml?language=en")
    if root is None:
        return None
    out = []
    for pos in root.findall("position"):
        pid = (pos.findtext("id") or "").strip()
        offices = [pos.findtext("office") or ""]
        offices += [o.text or "" for o in pos.findall("additionalOffices/office")]
        out.append(Job("personio", token, pid, (pos.findtext("name") or "").strip(),
                       ", ".join(o.strip() for o in offices if o.strip()),
                       f"https://{token}.jobs.personio.de/job/{pid}" if pid else "",
                       _normalize_date(pos.findtext("createdAt")),
                       (pos.findtext("department") or None)))
    return out


# Endpoint shapes below are now ground-truthed, not written from memory:
#   greenhouse  -- boards-api.greenhouse.io/v1/boards/{token}/jobs confirmed
#                  live against jfrog (token "jfrog") and wiz.io (token
#                  "wizinc", NOT "wiz" -- see token_candidates). The newer
#                  job-boards.greenhouse.io host is UI-only; the boards-api
#                  host still serves the same tokens, no migration needed.
#   ashby       -- confirmed live against snyk (valid board, 0 open jobs)
#                  and ramp (valid board, jobs present).
#   lever       -- confirmed live against lever's own token ("lever").
#   workable    -- confirmed live against huggingface; bogus tokens 404
#                  cleanly (existence check is sound).
#   smartrecruiters -- confirmed the /postings endpoint returns HTTP 200
#                  with empty content for ANY token, real or bogus --
#                  see the guard in f_smartrecruiters above.
#   recruitee   -- confirmed bogus subdomains 404 cleanly (existence check
#                  is sound).
#   personio    -- confirmed live against personio's own token ("personio");
#                  bogus tokens 307-redirect to personio.com itself
#                  (text/html), a clean existence signal.
# No 403s were observed from any of the above using the ats-probe/0.2 UA
# in this pass; if that changes, swap UA below for a browser string.
# Confirmed-by-hand false positives: a guessed token that IS a real,
# valid board on that ATS, just for the wrong company. Not a bug in any
# fetcher's existence check (those are all doing their job correctly) --
# short/common-word tokens (a big company's name, or an Israeli domain
# whose stem is an ordinary Hebrew/English word) can collide with an
# unrelated small business that happened to register the same slug.
# Nothing about probing can distinguish "real board, wrong company" from
# "real board, right company" -- the API response looks identical either
# way. Caught these by spot-checking results that looked suspicious
# (job locations/content that didn't fit the expected company) and
# verifying by hand; add to this set as more turn up, don't try to build
# a generic detector for it.
KNOWN_FALSE_POSITIVES: set[tuple[str, str]] = {
    ("ashby", "matrix"),          # matrix.co.il -- real board is a Boston VC firm, not Matrix IT
    ("smartrecruiters", "trigo"), # trigo.tech -- real board is an unrelated French/Moroccan company
    ("greenhouse", "iai"),        # iai.co.il -- real board is a UK company, not Israel Aerospace Industries
    ("recruitee", "max"),         # max.co.il -- real board is a German agency's demo/template listing
    ("personio", "amazon"),       # amazon.com -- same small tenant as personio:salesforce and personio:max below
    ("personio", "salesforce"),   # salesforce.com -- see above
    ("personio", "max"),          # max.co.il, second collision on top of the recruitee one above
    ("personio", "hpe"),          # hpe.com -- unrelated German tenant (job titles in German)
    ("personio", "matrix"),       # matrix.co.il, second collision on top of the ashby one above
    ("personio", "monday"),       # monday.com -- real board is "Monday" coworking spaces (Spain/Portugal)
}

FETCHERS: dict[str, Callable] = {
    "greenhouse": f_greenhouse,
    "personio": f_personio,
    "lever": f_lever,
    "ashby": f_ashby,
    "workable": f_workable,
    "recruitee": f_recruitee,
    "smartrecruiters": f_smartrecruiters,
}

# --------------------------------------------------------------------------
# Comeet: not guessable like the ATSes above -- the public API needs an
# opaque per-company `token` plus a `uid`, neither derivable from the
# domain. Two different embeds render that pair server-side into the
# company's OWN careers page, no browser/JS execution needed to read it:
#   1. WordPress plugin ("comeet-wp-plugin"):
#        var comeetvar = {"comeet_token":"...","comeet_uid":"91.001",...};
#      Ground-truthed against aquasec.com and silverfort.com.
#   2. Comeet's official generic embed widget (React/Next.js sites use
#      this -- it's what the "add this to your site" snippet in Comeet's
#      own docs looks like):
#        COMEET.init({"token": "...", "company-uid": "B1.001", ...})
#      Ground-truthed against overwolf.com. On streamed React payloads
#      this can arrive double-escaped (\" instead of ") because it's a
#      JSON string embedded inside another JSON string -- unescape before
#      matching, not by widening the regex.
# Coralogix (custom-built site, neither embed) exposes neither server-side
# -- that needs an actual Network-tab capture, same as the original brief
# anticipated. So this catches a real subset, not all of Comeet; it costs
# one GET per candidate careers path, same as any guess.
# --------------------------------------------------------------------------

COMEET_RE = re.compile(
    r'"(?:comeet_token|token)"\s*:\s*"([^"]+)"\s*,\s*"(?:comeet_uid|company-uid)"\s*:\s*"([^"]+)"'
)
COMEET_PATHS = ["/careers", "/careers/", "/jobs", "/about-us/careers", "/company/careers"]
# Shorter than the main TIMEOUT: this runs once per MISS domain trying up
# to 2*len(COMEET_PATHS) URLs, so a slow/dead host here is much more
# expensive per-domain than a single ATS API probe. Batch runs over
# hundreds of domains care more about not stalling on one bad host than
# about squeezing the last slow-but-alive one.
COMEET_TIMEOUT = 6


def _comeet_job(j: dict, uid: str, token: str) -> Job:
    """Shared between f_comeet_scrape and _fetch_comeet_pin -- both hit the
    same positions endpoint, just via different token-discovery paths.
    """
    return Job("comeet", f"{uid}:{token}", _txt(j.get("position_uid")),
               _txt(j.get("name")), _txt(j.get("location")),
               _txt(j.get("careers_page_active_url") or j.get("careers_page_url")),
               None, _txt(j.get("department")) or None,
               seniority=_COMEET_LEVEL_MAP.get(_txt(j.get("experience_level")).lower()) or None,
               workplace_type=_ATS_WORKPLACE_MAP.get(_txt(j.get("Remote")).lower()) or None)


def f_comeet_scrape(sess: requests.Session, domain: str) -> tuple[list[Job], str] | None:
    """Try to recover a Comeet uid+token pair from `domain`'s own careers
    page and, if found, fetch real postings. Returns (jobs, "uid:token")
    or None -- shaped differently from the other fetchers because it needs
    the raw domain, not a guessed token, so it isn't a drop-in FETCHERS
    entry; resolve() calls it directly.
    """
    for path in COMEET_PATHS:
        for host in (f"https://www.{domain}{path}", f"https://{domain}{path}"):
            try:
                r = sess.get(host, timeout=COMEET_TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            # Live-debugged against overwolf.com: the widget snippet often
            # arrives as a JSON string embedded inside another JSON string
            # (React streamed payload), so what's on the wire is literally
            # the two characters \ and n between fields, not a real
            # newline byte -- \s* in the regex can't see across that.
            # Unescape both before matching rather than widen the regex.
            cleaned = r.text.replace('\\"', '"').replace('\\n', '\n')
            m = COMEET_RE.search(cleaned)
            if not m:
                continue
            token, uid = m.group(1), m.group(2)
            jobs = get_json(sess, "https://www.comeet.com/careers-api/1.0/company/"
                                   f"{uid}/positions?token={token}")
            if not isinstance(jobs, list):
                continue
            out = [_comeet_job(j, uid, token) for j in jobs]
            return out, f"{uid}:{token}"
    return None


def _fetch_comeet_pin(sess: requests.Session, uid: str, token: str) -> list[Job] | None:
    """Same API call as f_comeet_scrape's tail end, split out so a pinned
    uid/token from companies.yml can skip the page-scraping step entirely.
    """
    jobs = get_json(sess, f"https://www.comeet.com/careers-api/1.0/company/{uid}/positions?token={token}")
    if not isinstance(jobs, list):
        return None
    return [_comeet_job(j, uid, token) for j in jobs]


# --------------------------------------------------------------------------
# Best-effort tier: for domains that miss every guessable/pinned ATS above.
# Two mechanisms, both scraping the company's OWN careers page (never a
# third-party aggregator):
#
#   1. Embed-link detection: look for a link/script src pointing at one of
#      the ATSes this file already knows how to query (including Workday,
#      which -- like Comeet -- needs a tenant+site pair that can't be
#      guessed from the domain, ground-truthed against workday.wd5's own
#      public board). A match here still gets verified against the real
#      API before counting as a hit, so it's just as trustworthy as a
#      guessed token -- it's finding the token a different way, not
#      lowering the bar. This is also how a wrong guess elsewhere in this
#      file (a token that isn't derivable from the domain at all) can
#      still resolve, without needing a hand entry in companies.yml.
#
#   2. schema.org JobPosting JSON-LD: many custom-built career sites emit
#      this for Google for Jobs SEO even without using any ATS this file
#      recognizes. Unlike (1), there's no live API to re-verify against --
#      it's trusting whatever the page declared -- so these land in the
#      DB as confidence='best_effort', never blended into 'verified'
#      counts (see db/schema.sql).
# --------------------------------------------------------------------------

EMBED_ATS_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/([a-zA-Z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([a-zA-Z0-9_-]+)")),
    ("recruitee", re.compile(r"([a-zA-Z0-9_-]+)\.recruitee\.com")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)")),
    ("personio", re.compile(r"([a-zA-Z0-9_-]+)\.jobs\.personio\.de")),
]

WORKDAY_RE = re.compile(
    r"([a-zA-Z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([a-zA-Z0-9_/-]+?)(?:[\"'?#]|$)"
)

CAREER_SCRAPE_PATHS = ["/careers", "/careers/", "/jobs", "/about-us/careers", "/company/careers",
                        "/about/careers", "/join-us", "/work-with-us", "/en/careers"]
CAREER_SCRAPE_TIMEOUT = 6  # same reasoning as COMEET_TIMEOUT: one bad host shouldn't stall a whole batch


def _parse_workday_posted_on(s: str | None) -> str | None:
    """Workday's postedOn is a relative human string ("Posted Today",
    "Posted 3 Days Ago", "Posted 30+ Days Ago"), not a date -- there's no
    absolute timestamp in the response at all. Approximate it: exact for
    small N, a floor for the open-ended "30+" bucket (imprecise, but
    "definitely not fresh" is still useful signal for ghost-job detection,
    better than dropping the value to None and losing that signal entirely).
    """
    if not s:
        return None
    s = s.strip().lower()
    if s == "posted today":
        days = 0
    elif s == "posted yesterday":
        days = 1
    else:
        m = re.match(r"posted (\d+)\+?\s*days? ago", s)
        if not m:
            return None
        days = int(m.group(1))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


_WORKDAY_MULTI_LOCATION_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


def _workday_location(sess: requests.Session, api_base: str, external_path: str, locations_text: str) -> str:
    """locationsText on the list endpoint collapses to a lazy "2 Locations"
    / "3 Locations" summary once a job has more than one office -- ground-
    truthed live (a real Bitsight posting), the individual locations
    aren't lost, just not on this endpoint: the per-job detail endpoint
    (one extra GET, called ONLY for postings actually hit by this, not
    every Workday job) has them as jobPostingInfo.location (the primary
    one) plus .additionalLocations (a list of the rest). Falls back to
    the original summary text if the detail fetch fails for any reason --
    "2 Locations" is a worse answer than the real names, not a wrong one.

    api_base must be the /wday/cxs/{tenant}/{site} API prefix, NOT the
    human-browsable tenant.wd.myworkdayjobs.com/{site} URL f_workday
    builds job.url from -- confirmed live those two return different
    things for the identical /job/{path} suffix: the API prefix returns
    this JSON, the browsable one 200s with an HTML SPA shell that has to
    run JS client-side to render, no embedded data to scrape out of it.
    """
    if not _WORKDAY_MULTI_LOCATION_RE.match(locations_text) or not external_path:
        return locations_text
    detail = get_json(sess, f"{api_base}{external_path}")
    info = (detail or {}).get("jobPostingInfo") or {}
    names = [n for n in [_txt(info.get("location")), *(info.get("additionalLocations") or [])] if n]
    return ", ".join(names) if names else locations_text


def f_workday(sess: requests.Session, tenant: str, wd: str, site: str) -> list[Job] | None:
    api_base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    d = get_json_post(sess, f"{api_base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    if not isinstance(d, dict) or "jobPostings" not in d:
        return None
    # externalPath comes back as "/job/<location>/<title>_<reqid>" -- it does
    # NOT include the site slug, even though the site is required in the
    # browsable URL. Building the link as tenant.wd.myworkdayjobs.com +
    # externalPath (no /{site}) produces a URL Workday's own app can't
    # resolve -- it silently bounces to a generic "invalid-url" error page
    # on community.workday.com instead of 404ing where you'd notice.
    # Ground-truthed against motorolasolutions:wd5:Careers -- see the
    # conversation that caught this for the before/after request.
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    out = []
    for j in d["jobPostings"]:
        bullets = j.get("bulletFields") or []
        external_path = _txt(j.get("externalPath"))
        location = _workday_location(sess, api_base, external_path, _txt(j.get("locationsText")))
        out.append(Job("workday", f"{tenant}:{wd}:{site}", _txt(bullets[0] if bullets else j.get("externalPath")),
                       _txt(j.get("title")), location,
                       base + external_path,
                       _parse_workday_posted_on(j.get("postedOn")), None))
    return out


JSONLD_JOBPOSTING_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)


def _extract_jobposting_jsonld(html: str, page_url: str) -> list[Job]:
    """schema.org JobPosting blocks, single or as an ItemList/@graph of
    several. No live API backs this -- see the tier comment above -- so
    every job from here is tagged ats="jsonld" and the loader marks it
    confidence='best_effort'.
    """
    out = []
    for block in JSONLD_JOBPOSTING_RE.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        # unwrap @graph and itemListElement -- both are common containers
        # for a list of postings on one page
        flat = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            if "@graph" in c and isinstance(c["@graph"], list):
                flat.extend(x for x in c["@graph"] if isinstance(x, dict))
            elif c.get("@type") == "ItemList" and isinstance(c.get("itemListElement"), list):
                flat.extend(x.get("item", x) for x in c["itemListElement"] if isinstance(x, dict))
            else:
                flat.append(c)
        for item in flat:
            if item.get("@type") != "JobPosting":
                continue
            loc = item.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
            location = ", ".join(x for x in [
                _txt(addr.get("addressLocality")) if isinstance(addr, dict) else "",
                _txt(addr.get("addressCountry")) if isinstance(addr, dict) else "",
            ] if x) or _txt(item.get("jobLocationType"))
            ident = item.get("identifier")
            if isinstance(ident, dict):
                ident = ident.get("value") or ident.get("name")  # identifier is often a PropertyValue object
            out.append(Job("jsonld", page_url, _txt(ident) or _txt(item.get("url")),
                           _txt(item.get("title")), location, _txt(item.get("url")) or page_url,
                           _normalize_date(item.get("datePosted")), None,
                           len(_txt(item.get("description"))), _clean_text(item.get("description"))))
    return out


def f_embed_scrape(sess: requests.Session, domain: str) -> tuple[str, list[Job], str] | None:
    """Try to resolve `domain` by scraping its own careers page: first for
    an embedded link to an ATS this file already knows how to verify
    (including Workday), falling back to schema.org JobPosting JSON-LD if
    nothing verifiable turns up on any path that responded. Returns
    (ats, jobs, token) or None. See the tier comment above for the
    verified-vs-best_effort distinction.
    """
    jsonld_fallback: list[Job] = []
    jsonld_source = ""

    for path in CAREER_SCRAPE_PATHS:
        for host in (f"https://www.{domain}{path}", f"https://{domain}{path}"):
            try:
                r = sess.get(host, timeout=CAREER_SCRAPE_TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            text = r.text

            for ats, pattern in EMBED_ATS_PATTERNS:
                m = pattern.search(text)
                if not m:
                    continue
                token = m.group(1)
                if (ats, token) in KNOWN_FALSE_POSITIVES:
                    continue
                try:
                    jobs = FETCHERS[ats](sess, token)
                except Exception:
                    jobs = None
                if jobs is not None:
                    return ats, jobs, token

            wm = WORKDAY_RE.search(text)
            if wm:
                tenant, wd, site = wm.group(1), wm.group(2), wm.group(3).split("/")[0]
                try:
                    jobs = f_workday(sess, tenant, wd, site)
                except Exception:
                    jobs = None
                if jobs is not None:
                    return "workday", jobs, f"{tenant}:{wd}:{site}"

            if not jsonld_fallback:
                found = _extract_jobposting_jsonld(text, host)
                if found:
                    jsonld_fallback, jsonld_source = found, host

    if jsonld_fallback:
        return "jsonld", jsonld_fallback, jsonld_source
    return None


def load_pins(path: Path = PINS_FILE) -> dict[str, dict[str, dict[str, str]]]:
    """Load companies.yml: {ats: {domain: {uid, token}}}. Missing file or
    missing pyyaml both just mean "no pins" -- companies.yml is an
    optional accelerant (skip the scrape/guess for domains someone already
    hand-verified), never a hard dependency of probe.py.
    """
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: dict[str, dict[str, dict[str, str]]] = {}
    for ats, entries in data.items():
        out[ats] = {e["domain"]: e for e in entries or [] if e.get("domain")}
    return out


PINS = load_pins()


def refetch_known(sess: requests.Session, domain: str, ats: str, token: str) -> Resolution:
    """Re-poll a company whose ats+token is already known -- no guessing,
    no careers-page scraping, one direct API call. This is the fast path:
    resolve() above answers "what ATS does this domain use" (expensive,
    runs rarely); this answers "what does that already-known board look
    like right now" (cheap, meant to run often -- see --known).

    token encodes differently per-ats, matching how resolve()/FETCHERS
    already produce/consume it:
      - comeet:  "uid:token"        (see _fetch_comeet_pin)
      - workday: "tenant:wd:site"   (see f_workday)
      - jsonld:  the page URL itself -- re-fetch and re-extract; there's
                 no API to call, same best_effort caveat as always
      - everything else: the raw FETCHERS[ats] token, used as-is
    """
    res = Resolution(domain=domain)
    try:
        if ats == "comeet":
            uid, ctoken = token.split(":", 1)
            jobs = _fetch_comeet_pin(sess, uid, ctoken)
        elif ats == "workday":
            tenant, wd, site = token.split(":", 2)
            jobs = f_workday(sess, tenant, wd, site)
        elif ats == "jsonld":
            r = sess.get(token, timeout=CAREER_SCRAPE_TIMEOUT)
            jobs = _extract_jobposting_jsonld(r.text, token) if r.status_code == 200 else None
        elif ats in FETCHERS:
            jobs = FETCHERS[ats](sess, token)
        else:
            jobs = None
    except Exception:
        jobs = None

    res.tried = 1
    if jobs is None:
        # A previously-verified board that fails on a routine re-poll is
        # surfaced as an error, not silently downgraded to a guess-again
        # MISS -- token_candidates() never runs in this path, so there's
        # no guessing fallback to fall into anyway. Could mean the board
        # closed, the token rotated, or a transient failure; --verbose
        # shows which via get_json's usual logging.
        res.error = f"known {ats}:{token} did not return a valid board on re-poll"
        return res
    res.ats, res.token, res.jobs = ats, token, _fill_classifications(jobs)
    res.job_count = len(jobs)
    return res


def _fill_classifications(jobs: list[Job]) -> list[Job]:
    """Applied once, right before a Resolution's jobs are finalized
    (every return path in resolve()/refetch_known()) -- fills in a
    text-keyword guess for any job whose fetcher didn't already set a
    structured seniority or workplace_type (see those fields' docstrings
    on Job). Mutates in place and returns the same list, so it drops into
    a `res.jobs = _fill_classifications(jobs)` one-liner at each exit
    point.
    """
    for j in jobs:
        if j.seniority is None:
            j.seniority = _classify_seniority(j.title)
        if j.workplace_type is None:
            j.workplace_type = _classify_workplace(j.location)
    return jobs


def resolve(domain: str, sess: requests.Session) -> Resolution:
    res = Resolution(domain=domain)
    tried = 0
    best: tuple[str, str, list[Job]] | None = None

    pin = PINS.get("comeet", {}).get(domain)
    if pin:
        tried += 1
        if VERBOSE:
            print(f"    probe comeet-pin:{domain}", file=sys.stderr)
        try:
            jobs = _fetch_comeet_pin(sess, pin["uid"], pin["token"])
        except Exception:
            jobs = None
        if jobs is not None:
            res.ats, res.token, res.jobs = "comeet", f"{pin['uid']}:{pin['token']}", _fill_classifications(jobs)
            res.job_count = len(jobs)
            res.tried = tried
            return res

    for token in token_candidates(domain):
        for ats, fn in FETCHERS.items():
            if (ats, token) in KNOWN_FALSE_POSITIVES:
                if VERBOSE:
                    print(f"    skip {ats}:{token} -- known false positive", file=sys.stderr)
                continue
            tried += 1
            if VERBOSE:
                print(f"    probe {ats}:{token}", file=sys.stderr)
            try:
                jobs = fn(sess, token)
            except Exception:
                jobs = None
            if jobs is None:
                continue
            # A valid board with zero open roles is still a valid board, but
            # prefer a hit that actually has postings.
            if best is None or len(jobs) > len(best[2]):
                best = (ats, token, jobs)
            if jobs:
                res.ats, res.token, res.jobs = best[0], best[1], _fill_classifications(best[2])
                res.job_count = len(res.jobs)
                res.tried = tried
                return res
    # Comeet doesn't fit the token-guessing loop above (see f_comeet_scrape
    # docstring) -- try it once per domain, and only let it win if nothing
    # else already found actual postings. This is the expensive path: up
    # to 2*len(COMEET_PATHS) extra GETs against a domain that has already
    # missed on every guessable ATS, so it dominates batch wall-clock time
    # (measured: ~5x slower over 269 domains with it on vs off). --no-comeet
    # skips it for a fast pass; companies.yml pins still apply either way.
    if SCRAPE_COMEET and (best is None or not best[2]):
        tried += 1
        if VERBOSE:
            print(f"    probe comeet-scrape:{domain}", file=sys.stderr)
        try:
            comeet_result = f_comeet_scrape(sess, domain)
        except Exception:
            comeet_result = None
        if comeet_result is not None:
            jobs, token = comeet_result
            if best is None or len(jobs) > len(best[2]):
                best = ("comeet", token, jobs)

    # Last resort: scrape the company's own careers page (see the tier
    # comment above f_embed_scrape). Same cost profile as the Comeet
    # scrape, so gated the same way -- only when nothing real has been
    # found yet -- plus --no-embed-scrape for fast passes.
    if SCRAPE_EMBED and (best is None or not best[2]):
        tried += 1
        if VERBOSE:
            print(f"    probe embed-scrape:{domain}", file=sys.stderr)
        try:
            embed_result = f_embed_scrape(sess, domain)
        except Exception:
            embed_result = None
        if embed_result is not None:
            ats, jobs, token = embed_result
            # jsonld is best_effort/unverified -- only let it fill a total
            # void, never override even an empty-but-confirmed-real board
            # from a verified ATS (comeet included).
            if ats == "jsonld":
                if best is None:
                    best = (ats, token, jobs)
            elif best is None or len(jobs) > len(best[2]):
                best = (ats, token, jobs)

    res.tried = tried
    if best:
        res.ats, res.token, res.jobs = best[0], best[1], _fill_classifications(best[2])
        res.job_count = len(res.jobs)
    else:
        res.error = "no ATS matched any token candidate"
    return res


def selftest() -> int:
    cases = [
        ("monday.com", ["monday", "mondayinc"]),
        ("next-insurance.com",
         ["next-insurance", "nextinsurance", "next-insuranceinc"]),
        ("orca.security",
         ["orca", "orcasecurity", "orca-security", "orcainc"]),
        ("https://www.at-bay.com/", ["at-bay", "atbay", "at-bayinc"]),
        ("cato.networks",
         ["cato", "catonetworks", "cato-networks", "catoinc"]),
    ]
    bad = 0
    for domain, want in cases:
        got = token_candidates(domain)
        ok = got == want
        bad += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {domain:<26} {got}")
    print(f"\n{len(cases) - bad}/{len(cases)} passed")
    return 1 if bad else 0


def raw_dump(sess: requests.Session, url: str) -> int:
    """Print status + content-type + first 400 chars for a single URL.

    Bypasses get_json's JSON-or-nothing filter entirely, so it also shows
    HTML error pages, redirects, and non-JSON responses -- useful for eyeballing
    what an ATS endpoint actually returns before deciding how a fetcher should
    parse it.
    """
    try:
        r = sess.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"exception: {e!r}", file=sys.stderr)
        return 1
    print(f"status: {r.status_code}")
    print(f"content-type: {r.headers.get('Content-Type', '')!r}")
    print(f"body[:400]: {r.text[:400]!r}")
    return 0


def print_row(r: Resolution) -> None:
    if not r.ats:
        print(f"{r.domain:<26} MISS  ({r.tried} probes)")
    else:
        print(f"{r.domain:<26} {r.ats + ':' + r.token:<38} {r.job_count:>4} jobs")


def run_and_report(items: list, resolve_one, json_mode: bool) -> list[Resolution]:
    """Shared by --batch and --known: submit one resolve_one(item) call per
    item across the worker pool, print rows as they land (unless --json),
    then a summary. Same shape either way -- --batch's items are domains
    for resolve(), --known's are (domain, ats, token) tuples for
    refetch_known() -- resolve_one closes over whichever.
    """
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(resolve_one, item): item for item in items}
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            if not json_mode:
                print_row(r)
    results.sort(key=lambda r: r.domain)
    if not json_mode:
        hits = [r for r in results if r.ats]
        by_ats: dict[str, int] = {}
        for r in hits:
            by_ats[r.ats] = by_ats.get(r.ats, 0) + 1
        print(f"\n{len(hits)}/{len(results)} resolved in {time.time() - t0:.1f}s")
        for a, n in sorted(by_ats.items(), key=lambda kv: -kv[1]):
            print(f"  {a:<18} {n}")
        print(f"  total jobs      {sum(r.job_count for r in hits)}")
    return results


def main() -> int:
    # Job titles/locations/descriptions routinely contain non-ASCII text
    # (accents, Hebrew company names, etc.). Windows consoles default stdout
    # to the cp1252 codepage, which crashes on the first such character --
    # hit live during batch testing on --json output. UTF-8 output is safe
    # everywhere stdout ends up (terminal, file redirect, pipe).
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--domain")
    ap.add_argument("--batch")
    ap.add_argument("--known", help="JSON array of {domain,ats,token} (a prior --json output works as-is, "
                                     "unresolved entries are skipped) -- re-poll already-known boards directly, "
                                     "no guessing/scraping. The fast path: seconds, not minutes, meant to run often.")
    ap.add_argument("--fetch", help="ats:token, skip discovery")
    ap.add_argument("--raw", help="dump status/content-type/first 400 chars for a URL, no parsing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="print N sample jobs per hit")
    ap.add_argument("--verbose", action="store_true", help="print every probe to stderr")
    ap.add_argument("--no-comeet", action="store_true",
                     help="skip the Comeet careers-page scrape (companies.yml pins still apply) -- much faster batch runs")
    ap.add_argument("--no-embed-scrape", action="store_true",
                     help="skip the careers-page embed/JobPosting-JSON-LD fallback -- much faster batch runs")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    global VERBOSE, SCRAPE_COMEET, SCRAPE_EMBED
    VERBOSE = args.verbose
    SCRAPE_COMEET = not args.no_comeet
    SCRAPE_EMBED = not args.no_embed_scrape

    sess = session()

    if args.raw:
        return raw_dump(sess, args.raw)

    if args.fetch:
        ats, _, token = args.fetch.partition(":")
        fn = FETCHERS.get(ats)
        if not fn:
            print(f"unknown ats '{ats}'; have: {', '.join(FETCHERS)}", file=sys.stderr)
            return 2
        jobs = fn(sess, token)
        if jobs is None:
            print(f"{ats}:{token} did not return a valid board", file=sys.stderr)
            return 1
        r = Resolution(f"{ats}:{token}", ats, token, len(jobs), 1, None, jobs)
        results = [r]
    elif args.known:
        with open(args.known, encoding="utf-8") as fh:
            entries = json.load(fh)
        known = [e for e in entries if e.get("ats") and e.get("token")]
        skipped = len(entries) - len(known)
        if VERBOSE and skipped:
            print(f"    [--known] skipping {skipped} unresolved entries from {args.known}", file=sys.stderr)
        if not known:
            print(f"no resolved (ats+token) entries in {args.known}", file=sys.stderr)
            return 2
        results = run_and_report(
            known, lambda e: refetch_known(sess, e["domain"], e["ats"], e["token"]), args.json
        )
    else:
        domains = []
        if args.domain:
            domains.append(args.domain)
        if args.batch:
            with open(args.batch, encoding="utf-8-sig") as fh:
                lines = fh.readlines()
            # Bug found live: the old parser stripped '#' only from the
            # *token* that immediately followed it, then tokenized the
            # *whole file* as one blob. A comment like "pinned in
            # companies.yml -- see comeet.com/jobs URLs" leaked
            # "companies.yml" and "comeet.com/jobs" into the domain list
            # as if they were real entries, because only the word "--"
            # itself started with '#', not the rest of the sentence.
            # Filtering whole lines before tokenizing is the actual fix.
            lines = [l for l in lines if not l.lstrip().startswith("#")]
            raw = " ".join(lines).replace('"', " ").replace("'", " ")
            domains += [t for t in re.split(r"[\s,]+", raw) if t and "." in t]
        if not domains:
            ap.print_help()
            return 2

        results = run_and_report(domains, lambda d: resolve(d, sess), args.json)

    if args.show:
        for r in results:
            for j in r.jobs[:args.show]:
                print(f"    {j.title[:58]:<58} {j.location[:30]}")

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())