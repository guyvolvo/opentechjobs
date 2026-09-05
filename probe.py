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

# Shared with api/handler.py and alerts.py -- see job_filters.py's own
# docstring on why (avoiding a second, independently-drifting Israel-
# location keyword list). Flat top-level import works as-is once
# deployed (deploy-scrape-lambda.yml already copies api/job_filters.py
# alongside probe.py in the Lambda build); the except branch is only for
# running probe.py straight from the repo root, where job_filters.py
# still lives under api/.
try:
    from job_filters import IL_KEYWORDS
except ImportError:
    sys.path.insert(0, str(Path(__file__).with_name("api")))
    from job_filters import IL_KEYWORDS

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
    # Cleaned plain text via _clean_text(). None when the ATS's list
    # endpoint doesn't include a description (SmartRecruiters, Comeet,
    # Workday). For Comeet and Workday, an extra per-job detail request
    # can fill this in -- see FETCH_FULL_DESCRIPTIONS -- but only during
    # scrape-discover.yml's slower, less-frequent pass; doing this on
    # every 10-min fast-poll, for every known listing on either ATS,
    # would be both too slow and needlessly hard on their APIs for
    # content that rarely changes. SmartRecruiters has the same
    # list-endpoint gap, not yet given the same treatment.
    description: str | None = None
    # intern/junior/mid/senior/staff/principal/lead/manager/director/exec,
    # or None if the posting doesn't state a level. Some ATSes provide a
    # structured field (SmartRecruiters, Comeet); otherwise resolve() falls
    # back to a title-keyword guess via _classify_seniority().
    seniority: str | None = None
    # remote/hybrid/onsite, or None if unstated. Some ATSes provide a
    # structured field (Ashby, Lever, SmartRecruiters, Recruitee, Comeet);
    # otherwise resolve() falls back to a location-text guess via
    # _classify_workplace() (Greenhouse has no structured field at all).
    workplace_type: str | None = None
    # Up to 5 tech/skill terms matched against title+description via
    # _extract_skills() -- see _fill_classifications(). Empty when
    # neither field mentions anything in _SKILL_KEYWORDS, common for
    # non-technical roles or ATSes with no description on this pass.
    skills: list[str] = field(default_factory=list)
    # Real disclosed comp (Ashby's structured field today) or a market
    # estimate (_estimate_salary, Israeli role x seniority snapshot) --
    # never both; see salary_is_estimate. None when neither is available.
    salary_text: str | None = None
    salary_is_estimate: bool = False


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
FETCH_FULL_DESCRIPTIONS = False  # Comeet + Workday's extra per-job detail request; see the Job.description comment


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    a = requests.adapters.HTTPAdapter(pool_connections=WORKERS * 2,
                                      pool_maxsize=WORKERS * 2)
    s.mount("https://", a)
    return s


def get_json(sess: requests.Session, url: str) -> Any:
    """GET returning parsed JSON, or None if the response isn't usable.

    Logs the reason for every None (when VERBOSE) so a MISS can be told
    apart from a 404, a UA block, or a timeout. Retries once on connection
    errors only (timeout/DNS/reset). A real 404 is a signal, not a fluke,
    and isn't retried.
    """
    return _request_json(lambda: sess.get(url, timeout=TIMEOUT), url, "get_json")


def get_json_post(sess: requests.Session, url: str, body: dict) -> Any:
    """POST variant of get_json. Workday's CXS API takes search params
    as a JSON body, not a query string. Same retry/logging behavior.
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
    # wiz.io's real Greenhouse token is "wizinc". Israeli companies often
    # register under their US legal entity name. Cheap to try, low priority.
    out.append(stem + "inc")
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# fetchers: each returns a list[Job], or None if the token does not exist

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
# Each regex below turns one HTML structural boundary into a real line
# break (headings/bullets also get a "## "/"- " marker) before the
# catch-all tag strip runs. Order matters: heading/li openers are consumed
# first so the general closing-tag pass doesn't re-match them.
_HEADING_OPEN_RE = re.compile(r"<h[1-6]\b[^>]*>", re.IGNORECASE)
# Greenhouse marks section labels as a <p> containing only one <strong>
# (e.g. <p><strong>What You'll Be Doing</strong></p>) rather than a real
# heading tag. Only fires when the bold span is the paragraph's entire
# content, not incidental emphasis mid-sentence.
_BOLD_PARAGRAPH_HEADING_RE = re.compile(
    r"<p\b[^>]*>\s*<(strong|b)\b[^>]*>(.*?)</\1>\s*</p>", re.IGNORECASE | re.DOTALL
)
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BLOCK_BOUNDARY_RE = re.compile(r"</?(p|br|li|ul|ol|h[1-6]|div|tr|table)\b[^>]*>", re.IGNORECASE)
_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
DESCRIPTION_MAX_CHARS = 8000


def _clean_text(raw: Any, max_chars: int = DESCRIPTION_MAX_CHARS) -> str | None:
    """Raw description field -> plain text with structure preserved, or None.

    Handles both HTML (Greenhouse's `content`) and plain-text fields
    (Lever/Ashby's `descriptionPlain`) through the same path. Headings
    become "## " and list items "- ", markers the frontend renders as a
    bold label / bullet. Only horizontal whitespace collapses; real line
    breaks are preserved rather than flattened to a single space.

    Truncated to max_chars (8000): measured against a real ~3000-job
    batch, this keeps 94% of descriptions whole for ~27% more DB size,
    a better trade than a shorter cutoff silently dropping a keyword a
    search might rely on.
    """
    text = _txt(raw)
    if not text:
        return None
    # Greenhouse's `content` is HTML with its own tags entity-escaped
    # ("&lt;h2&gt;" not "<h2>"), so unescape before stripping tags, then
    # unescape again to catch entities left in the visible text.
    text = html.unescape(text)
    text = _BOLD_PARAGRAPH_HEADING_RE.sub(r"\n## \2\n", text)
    text = _HEADING_OPEN_RE.sub("\n## ", text)
    text = _LI_OPEN_RE.sub("\n- ", text)
    text = _BLOCK_BOUNDARY_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _HORIZONTAL_WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    return text[:max_chars] or None


# Checked top-to-bottom, first match wins. Highest seniority first so
# "VP of Engineering" hits exec, "Senior Staff Engineer" hits staff.
# Word-boundary (\b) matching, not substring, so "International Account
# Executive" doesn't misclassify as an internship.
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

# Structured level fields from ATSes that provide one, preferred over
# the title-keyword guess below. Unrecognized values map to None rather
# than a guessed signal.
_SMARTRECRUITERS_LEVEL_MAP = {
    "internship": "intern",
    "entry_level": "junior",
    "associate": "junior",
    "mid_senior_level": "senior",
    "director": "director",
    "executive": "exec",
}

_COMEET_LEVEL_MAP = {
    "junior": "junior",
    "intermediate": "mid",
    "senior": "senior",
    "management": "manager",
    # Comeet's experience_level is company-customizable and can hold an
    # unrelated value (e.g. "Full-time"); unrecognized values map to None.
}

# Structured workplace fields, lowercased before lookup since ATSes case
# them differently ("Remote" vs "remote"). Comeet's field also has a
# large None share that means genuinely unspecified, not onsite.
_ATS_WORKPLACE_MAP = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on site": "onsite",
}


def _classify_seniority(title: str | None) -> str | None:
    """Best-effort seniority from title text. Fallback for ATSes with no
    structured level field. Most titles state no level, so None is normal.
    """
    if not title:
        return None
    for level, pattern in _SENIORITY_RULES:
        if pattern.search(title):
            return level
    return None


# "remote" wins over "hybrid" if a location string somehow states both.
# Word-boundary matching, same reasoning as _SENIORITY_RULES.
_WORKPLACE_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    (level, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for level, needles in [
        ("remote", ["remote", "work from home", "wfh"]),
        ("hybrid", ["hybrid"]),
        ("onsite", ["on-site", "onsite", "on site"]),
    ]
]


def _classify_workplace(location: str | None) -> str | None:
    """Best-effort remote/hybrid/onsite from location text. Fallback for
    ATSes with no structured field. Mainly covers Greenhouse, where
    locations like "United States - Remote" are common.
    """
    if not location:
        return None
    for level, pattern in _WORKPLACE_RULES:
        if pattern.search(location):
            return level
    return None


# Hand-curated, same reasoning as _SENIORITY_RULES/_WORKPLACE_RULES above
# rather than sourced from an external taxonomy (there's no free "job
# skill keyword" API/dataset worth round-tripping through for this).
# Canonical display label -> alternate spellings/casings to match.
# Order matters where a shorter term is a substring of a longer one's
# *words* (word-boundary regex alone doesn't save you there, e.g. "C"
# would match inside "C++" text as its own word) -- longer/specific
# terms are listed first and _extract_skills dedupes by canonical label
# so a title matching both "Node.js" and "JavaScript" shows both, not
# a double-count of one.
_SKILL_KEYWORDS: list[tuple[str, "re.Pattern[str]"]] = [
    (label, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for label, needles in [
        # Languages
        ("Python", ["python"]),
        ("TypeScript", ["typescript", "ts"]),
        ("JavaScript", ["javascript", "js"]),
        ("Java", ["java"]),
        ("Go", ["golang"]),  # bare "go" is too common a word to match safely
        ("Rust", ["rust"]),
        ("C++", ["c++", "cpp"]),
        # ".NET" itself isn't matched: it starts with punctuation, and the
        # shared \b...\b wrapper (see the comprehension above) can't
        # anchor a boundary directly before a leading ".". "dotnet"/"c#"
        # cover it in practice.
        ("C#", ["c#", "csharp", "dotnet"]),
        ("Ruby", ["ruby"]),
        ("PHP", ["php"]),
        ("Swift", ["swift"]),
        ("Kotlin", ["kotlin"]),
        ("Scala", ["scala"]),
        ("SQL", ["sql"]),
        # Cloud / infra
        ("AWS", ["aws", "amazon web services"]),
        ("GCP", ["gcp", "google cloud"]),
        ("Azure", ["azure"]),
        ("Docker", ["docker"]),
        ("Kubernetes", ["kubernetes", "k8s"]),
        ("Terraform", ["terraform"]),
        ("Ansible", ["ansible"]),
        ("Linux", ["linux"]),
        ("CI/CD", ["ci/cd", "continuous integration", "continuous deployment"]),
        # Frameworks / frontend
        ("React", ["react", "react.js", "reactjs"]),
        ("Angular", ["angular"]),
        ("Vue", ["vue.js", "vuejs"]),  # bare "vue" is too common a fragment (e.g. "point of view")
        ("Node.js", ["node.js", "nodejs"]),  # bare "node" is ambiguous with a cluster/graph node
        ("Django", ["django"]),
        ("Flask", ["flask"]),
        ("Spring Boot", ["spring boot", "springboot"]),  # bare "spring" is an ordinary English word
        ("GraphQL", ["graphql"]),
        # Databases
        ("PostgreSQL", ["postgresql", "postgres"]),
        ("MySQL", ["mysql"]),
        ("MongoDB", ["mongodb", "mongo"]),
        ("Redis", ["redis"]),
        ("Elasticsearch", ["elasticsearch"]),
        ("Kafka", ["kafka"]),
        ("Spark", ["spark"]),
        # Data / ML
        ("TensorFlow", ["tensorflow"]),
        ("PyTorch", ["pytorch"]),
        ("LLM", ["llm", "large language model"]),
        # Bare "rag" isn't matched: too easily confused with the ordinary
        # word, or with "RAG status" (red-amber-green) in PM postings.
        ("RAG", ["retrieval-augmented generation"]),
        ("NLP", ["nlp", "natural language processing"]),
        # Security
        ("Active Directory", ["active directory"]),
        ("SIEM", ["siem"]),
        ("Penetration Testing", ["penetration testing", "pentest"]),
        # General practice
        ("Agile", ["agile"]),
        ("Scrum", ["scrum"]),
        ("Git", ["git"]),
        ("REST API", ["rest api", "restful"]),
        ("Microservices", ["microservices"]),
    ]
]

# First 5 matches, in the order they appear in title+description -- not
# a fixed priority ranking -- so the tags reflect what the posting
# itself leads with, not this list's own ordering.
_SKILL_MAX_TAGS = 5


def _extract_skills(title: str | None, description: str | None) -> list[str]:
    text = f"{title or ''}\n{description or ''}"
    if not text.strip():
        return []
    found: list[str] = []
    for label, pattern in _SKILL_KEYWORDS:
        m = pattern.search(text)
        if m:
            found.append((m.start(), label))
    found.sort(key=lambda t: t[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, label in found:
        if label not in seen:
            seen.add(label)
            out.append(label)
        if len(out) == _SKILL_MAX_TAGS:
            break
    return out


# ₪/month gross, Israeli tech market. NOT this project's own data --
# hand-transcribed from GotFriends' own published salary tables (a
# specialist Israeli tech-recruitment firm; primary source, not a
# third-party derivative), https://www.gotfriends.co.il/בלוגים/high-tech-salary-tables/
# (checked live 2026-09-04). Four experience bands per role: 0-2y, 3-5y,
# 6-10y, and a "management" track column -- not every role's row has all
# four (e.g. QA/automation rows stop at 6-10y; a handful of leadership-
# only rows -- VP R&D, CTO, Director of Development, CISO -- were left
# out entirely rather than guessed at, since it wasn't clear which of
# their 3 listed figures maps to which band).
#
# Deliberately covers technical/engineering/product roles only, because
# that's genuinely all this source (or any other free source checked --
# see _estimate_salary's docstring) has real data for. Checked GotFriends'
# own site directly: no Sales, Marketing, Finance, HR, or G&A tables
# exist there at all -- it's a tech-recruitment-only firm. A listing in
# one of those departments gets no estimate, not a guessed one borrowed
# from an unrelated category.
_IL_SALARY_BANDS = ("0-2", "3-5", "6-10", "mgmt")
_IL_SALARY_RAW: dict[str, tuple[tuple[int, int], ...]] = {
    # Software development
    "dotnet": ((22, 27), (27, 37), (35, 42), (40, 50)),
    "cpp": ((22, 27), (30, 37), (38, 45), (43, 50)),
    "java": ((22, 27), (30, 37), (38, 45), (40, 50)),
    "kotlin": ((22, 27), (30, 37), (38, 45), (40, 50)),
    "frontend": ((22, 27), (27, 37), (35, 45), (42, 55)),
    "python": ((20, 26), (28, 40), (34, 45), (42, 50)),
    "fullstack": ((20, 26), (27, 37), (34, 45), (40, 50)),
    "nodejs": ((20, 25), (27, 37), (33, 44), (40, 50)),
    "php": ((18, 20), (20, 25), (25, 28), (32, 37)),
    "mobile_generic": ((20, 25), (26, 33), (32, 40), (38, 45)),
    "bigdata_dev": ((25, 30), (32, 40), (40, 50), (42, 52)),
    "backend": ((22, 27), (30, 37), (35, 45), (42, 55)),
    "scala": ((22, 28), (30, 37), (37, 45), (42, 55)),
    "go": ((20, 26), (27, 38), (33, 45), (42, 52)),
    "embedded": ((24, 28), (28, 35), (35, 42), (40, 50)),
    "angular": ((22, 27), (26, 36), (35, 45), (42, 55)),
    "ios": ((22, 27), (26, 33), (32, 40), (38, 45)),
    "react": ((24, 29), (28, 38), (36, 47), (44, 57)),
    "android": ((20, 25), (26, 33), (32, 40), (38, 45)),
    "c_lang": ((22, 27), (27, 37), (36, 45), (43, 52)),
    "data_engineer": ((20, 25), (27, 37), (33, 43), (40, 50)),
    "software_architect": ((35, 40), (40, 43), (43, 45), (45, 52)),
    "ai_engineer": ((22, 35), (28, 45), (40, 55), (40, 55)),
    # AI
    "llm_engineer": ((22, 35), (28, 45), (40, 55), (40, 60)),
    "solution_architect": ((20, 25), (25, 35), (32, 38), (38, 48)),
    # Hardware
    "vlsi": ((23, 28), (30, 37), (40, 50), (42, 48)),
    "board_design": ((23, 27), (29, 35), (33, 45), (38, 48)),
    "verification": ((22, 28), (28, 38), (38, 48), (43, 50)),
    "rf": ((18, 20), (20, 29), (26, 36), (37, 43)),
    "electrical_power": ((22, 26), (28, 34), (35, 42), (40, 47)),
    "hardware_architect": ((30, 35), (38, 45), (45, 55), (50, 65)),
    "system_engineer": ((22, 27), (28, 36), (35, 45), (42, 52)),
    "hardware_engineer": ((22, 27), (28, 35), (35, 45), (40, 50)),
    "hardware_research": ((25, 30), (33, 40), (40, 52), (48, 60)),
    # Cyber/security
    "malware_analyst": ((20, 28), (28, 35), (30, 40), (40, 50)),
    "reverse_engineer": ((30, 35), (40, 45), (45, 55), (50, 70)),
    "security_expert": ((20, 25), (25, 30), (35, 48), (60, 100)),
    "security_analyst": ((18, 20), (20, 30), (30, 40), (35, 50)),
    "vulnerability_researcher": ((20, 35), (40, 80), (45, 120), (60, 120)),
    "incident_response": ((25, 30), (30, 35), (35, 40), (40, 60)),
    "soc_analyst": ((15, 20), (20, 25), (25, 30), (30, 40)),
    "security_researcher": ((20, 25), (25, 30), (35, 48), (60, 100)),
    # Algorithms / ML
    "algo_engineer": ((22, 35), (28, 40), (35, 50), (40, 55)),
    "data_scientist": ((22, 35), (28, 45), (40, 55), (40, 60)),
    "ml_engineer": ((22, 35), (28, 45), (40, 55), (40, 55)),
    "computer_vision": ((22, 35), (28, 45), (45, 60), (45, 65)),
    "signal_processing": ((22, 33), (28, 40), (35, 50), (45, 55)),
    "deep_learning": ((22, 35), (28, 45), (40, 55), (40, 55)),
    # QA/automation -- no management column in the source for these
    "automation_dev": ((18, 25), (25, 33), (33, 38)),
    "qa_generic": ((12, 15), (15, 25), (23, 30)),
    # Support/DevOps/System
    "devops_engineer": ((25, 30), (32, 38), (35, 45), (40, 55)),
    "network_manager": ((18, 23), (23, 27), (27, 35), (30, 35)),
    # Product management
    "product_manager": ((23, 39), (28, 35), (33, 45), (40, 60)),
    # BI/Big Data
    "bi_developer": ((20, 25), (25, 27), (28, 32), (32, 42)),
    "data_analyst": ((20, 25), (25, 30), (28, 32), (30, 40)),
    "dba_bigdata": ((25, 30), (30, 35), (35, 45), (40, 50)),
    "product_analyst": ((20, 25), (25, 30), (28, 32), (30, 40)),
}
_IL_SALARY_TABLE_KNIS: dict[tuple[str, str], tuple[int, int]] = {
    (role, band): rng
    for role, ranges in _IL_SALARY_RAW.items()
    for band, rng in zip(_IL_SALARY_BANDS, ranges)
}

# Checked top-to-bottom, most specific first -- a title mentioning a
# specific language/specialization should hit that row, not the generic
# "backend"/"engineer" catch-all at the very end.
_ROLE_CATEGORY_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    (category, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for category, needles in [
        ("computer_vision", ["computer vision"]),
        ("deep_learning", ["deep learning"]),
        ("ml_engineer", ["machine learning", "ml engineer"]),
        ("data_scientist", ["data scientist"]),
        ("signal_processing", ["signal processing"]),
        ("llm_engineer", ["llm", "large language model", "genai", "generative ai"]),
        ("algo_engineer", ["algorithm"]),
        ("vlsi", ["vlsi"]),
        ("rf", ["rf engineer", "rf design"]),
        ("board_design", ["board design"]),
        ("verification", ["verification engineer"]),
        ("electrical_power", ["electrical engineer", "power engineer"]),
        ("hardware_architect", ["hardware architect"]),
        ("hardware_research", ["hardware research"]),
        ("hardware_engineer", ["hardware engineer"]),
        ("malware_analyst", ["malware"]),
        ("reverse_engineer", ["reverse engineer"]),
        ("vulnerability_researcher", ["vulnerability research"]),
        ("incident_response", ["incident response"]),
        ("soc_analyst", ["soc analyst"]),
        ("security_researcher", ["security research"]),
        ("security_expert", ["security", "appsec", "cyber", "penetration test", "pentest"]),
        ("data_engineer", ["data engineer"]),
        ("data_analyst", ["data analyst"]),
        ("bi_developer", ["bi developer", "business intelligence"]),
        ("dba_bigdata", ["dba", "database administrator"]),
        ("product_analyst", ["product analyst"]),
        ("product_manager", ["product manager"]),
        ("solution_architect", ["solution architect"]),
        ("software_architect", ["software architect"]),
        ("system_engineer", ["system engineer"]),
        ("devops_engineer", ["devops", "sre", "site reliability", "platform engineer", "infrastructure engineer"]),
        ("network_manager", ["network manager", "it manager"]),
        ("automation_dev", ["automation", "sdet"]),
        ("qa_generic", ["qa", "quality assurance", "test engineer"]),
        ("ios", ["ios developer", "ios engineer"]),
        ("android", ["android developer", "android engineer"]),
        ("mobile_generic", ["mobile engineer", "mobile developer"]),
        ("react", ["react"]),
        ("angular", ["angular"]),
        ("nodejs", ["node.js", "nodejs", "node js"]),
        ("dotnet", [".net", "dotnet", "c#", "csharp"]),
        ("kotlin", ["kotlin"]),
        ("scala", ["scala"]),
        ("go", ["golang"]),
        ("php", ["php"]),
        ("embedded", ["embedded"]),
        ("python", ["python"]),
        ("cpp", ["c++", "cpp"]),
        ("java", ["java"]),
        ("bigdata_dev", ["big data"]),
        ("ai_engineer", ["ai engineer", "artificial intelligence"]),
        ("fullstack", ["full stack", "full-stack", "fullstack"]),
        ("frontend", ["frontend", "front-end", "front end", "ui engineer"]),
        # Backend last: the widest net (bare "backend"/"engineer"/
        # "developer"), so anything more specific above gets first pick.
        ("backend", ["backend", "back-end", "back end", "software engineer",
                     "software developer", "engineer", "developer"]),
    ]
]

# lead/manager collapse to the role's own "management" column; staff and
# principal are the highest individual-contributor tier this source
# breaks out, so they use the same "6-10y" column senior does. director
# and exec have no mapping at all -- GotFriends' leadership-only rows
# (VP R&D, CTO, Director of Development, CISO) were left out of the raw
# table above because it wasn't clear which of their 3 listed figures
# maps to which experience band; better no estimate than a guessed one.
_SENIORITY_TO_BAND = {
    "intern": "0-2", "junior": "0-2",
    "mid": "3-5",
    "senior": "6-10", "staff": "6-10", "principal": "6-10",
    "lead": "mgmt", "manager": "mgmt",
}


def _estimate_salary(title: str | None, description: str | None, seniority: str | None) -> tuple[int, int] | None:
    """(low, high) in ₪K/month from _IL_SALARY_TABLE_KNIS, or None if
    neither title nor description confidently match a role category, or
    the seniority is director/exec (no mapping -- see
    _SENIORITY_TO_BAND). Never guessed for a job whose location isn't
    Israel -- callers are expected to check that themselves, since this
    function only has the title/description/seniority to work with.

    Matches against title+description together, same as
    _extract_skills -- a generic title ("Senior Software Engineer")
    often states the actual language/specialization only in the body
    (reported live: a real "Go" role with no "Go" in its title, only in
    a "preferably in Go (Golang)" line, fell back to the generic
    backend row instead of the more specific -- and more accurate --
    Go one). Role-category rules are still checked in their own
    priority order regardless of where in the combined text a term
    appears, same as before.

    A posting that doesn't state a level, or whose stated level has no
    row for this specific role (e.g. QA/automation rows have no
    "management" column), gets a wider range spanning every band this
    role DOES have data for, rather than no estimate at all -- still a
    real figure pulled from the sourced table, just less precise.
    """
    if not title:
        return None
    text = f"{title}\n{description or ''}"
    category = None
    for cat, pattern in _ROLE_CATEGORY_RULES:
        if pattern.search(text):
            category = cat
            break
    if not category:
        return None
    if seniority in ("director", "exec"):
        return None

    band = _SENIORITY_TO_BAND.get(seniority or "")
    if band:
        direct = _IL_SALARY_TABLE_KNIS.get((category, band))
        if direct:
            return direct

    available = [rng for b in _IL_SALARY_BANDS if (rng := _IL_SALARY_TABLE_KNIS.get((category, b)))]
    if not available:
        return None
    return (min(r[0] for r in available), max(r[1] for r in available))


def _normalize_date(v: Any) -> str | None:
    """Normalize any ATS's posting date to ISO 8601. Every fetcher should
    push its date through this before putting it on a Job.

    Handles two known quirks: Lever's createdAt is epoch milliseconds, and
    Recruitee's published_at ends in a literal "UTC" word instead of an
    offset. Both would silently break SQLite's julianday() age math
    downstream if left as-is.
    """
    if v is None or v == "":
        return None

    # epoch milliseconds (Lever), comes through as int or numeric string
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None

    s = str(v).strip()

    # "YYYY-MM-DD HH:MM:SS UTC" (Recruitee): literal suffix, not an offset
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
    # includeCompensation=true: verified live (2026-09-04) this is a real,
    # documented Ashby parameter -- confirmed against Ramp's public board,
    # which returns genuine disclosed ranges ("$211.4K - $290.6K") this
    # way. Most companies (esp. Israeli ones, no pay-transparency mandate)
    # still come back with every compensation field null; that's a real
    # "not disclosed," not this call failing.
    d = get_json(sess, f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    out = []
    for j in d["jobs"]:
        comp = j.get("compensation") or {}
        salary_text = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        out.append(Job("ashby", token, _txt(j.get("id")), _txt(j.get("title")),
                        _txt(j.get("location")), _txt(j.get("jobUrl")),
                        _normalize_date(j.get("publishedAt")), _txt(j.get("department")) or None,
                        len(_txt(j.get("descriptionPlain"))), _clean_text(j.get("descriptionPlain")),
                        workplace_type=_ATS_WORKPLACE_MAP.get(_txt(j.get("workplaceType")).lower()) or None,
                        salary_text=_txt(salary_text) or None, salary_is_estimate=False))
    return out


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
    # remote/hybrid/on_site are independent, non-exclusive booleans here
    # (a posting can have both hybrid and on_site true). remote wins if
    # set, then hybrid, then on_site.
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
    # This endpoint returns HTTP 200 with an empty `content` list for ANY
    # token, even ones that don't exist, so an empty result can't be
    # trusted as a real hit. Treating it as a match would resolve every
    # unmatched domain in a batch to smartrecruiters, overwriting real
    # MISSes. Costs a rare false negative (a real customer with 0 open
    # roles) to avoid a guaranteed false positive.
    if not d["content"]:
        if VERBOSE:
            print(f"    [f_smartrecruiters] {token} -> 200 empty content, treating as no-match",
                  file=sys.stderr)
        return None
    out = []
    for j in d["content"]:
        loc = j.get("location") or {}
        level = j.get("experienceLevel") or {}
        # Two independent booleans, not one field. Both explicitly False
        # is a confident onsite signal here; only claim onsite when both
        # are truly False, not merely missing.
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
    """Personio's public feed is XML, not JSON, so get_json() (which only
    accepts a json content-type) doesn't cover it. This is the same shape
    (retry once on connection errors, log why on None) for the one
    XML-based fetcher. A nonexistent tenant subdomain 307-redirects to
    personio.com's own marketing site (text/html), so the content-type
    check alone is already a sound existence signal, confirmed live. No
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


# JazzHR's hosted career page has no JSON API at all -- plain
# server-rendered HTML, ground-truthed against firstadvantage.applytojob.com
# and talentwwinc.applytojob.com (both real, live companies). Matches this
# file's token-guessing model anyway (the subdomain is just the company's
# slugified name), so it's still a drop-in FETCHERS entry, just parsing
# markup instead of calling an API -- the one exception to this module's
# own "does not parse careers pages" framing besides the Comeet/embed-scrape
# fallback tier further below.
_JAZZHR_JOB_RE = re.compile(
    r'<a href="(https://[^"]+/apply/[^"]+)">\s*(.*?)\s*</a>.*?'
    r'<ul[^>]*class=["\']list-inline[^"\']*["\'][^>]*>(.*?)</ul>',
    re.DOTALL,
)
# The map-marker icon line is always present (location); sitemap (department)
# is optional per posting -- not every JazzHR customer sets one.
_JAZZHR_META_RE = re.compile(r"fa-(map-marker|sitemap)[\"']?\s*></i>\s*([^<]*)<")


def f_jazzhr(sess, token):
    """A nonexistent token 302s to jazzhr.com's own marketing site rather
    than 404ing on its own subdomain -- both a real and a fake token come
    back HTTP 200, so "did the response actually stay on
    {token}.applytojob.com" is the real existence check, not status code
    alone. No posted-date field on this list view, and no per-job
    description without a second request per posting -- not done here,
    same cost tradeoff as Comeet's FETCH_FULL_DESCRIPTIONS.

    A THIRD state, distinct from both of the above: reported live, a
    generic/brand-name token guess (amazon, apple, microsoft, cisco,
    dell, oracle, siemens, broadcom, plus a few Israeli-company guesses:
    d-id, gem, tytocare, compass) resolves to a real 200 that stays on
    {token}.applytojob.com, but the page itself is titled "JazzHR -
    Inactive Career Page" -- someone registered the slug and never
    configured real content. Confirmed live: no false negative risk,
    genuinely active boards (including a currently-empty one, Wrap
    Technologies) carry their own real company name in that title
    instead. Treated as a MISS, same as the nonexistent-token case.

    JazzHR is mid-migration to a newer template -- one ground-truth
    company's own page source carries the comment "temporary switch to
    support feature flag disabling customers from seeing the new
    styles." This only covers the classic list-group-item template both
    ground-truth companies still render. A company already switched to
    the new template comes back a real 200 this regex simply finds zero
    jobs in -- indistinguishable from a genuinely empty board.
    """
    url = f"https://{token}.applytojob.com/apply"
    try:
        r = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code != 200 or f"{token}.applytojob.com" not in r.url:
        return None
    if "Inactive Career Page" in r.text:
        return None
    out = []
    for m in _JAZZHR_JOB_RE.finditer(r.text):
        job_url, raw_title, meta_html = m.group(1), m.group(2), m.group(3)
        title = html.unescape(_WHITESPACE_RE.sub(" ", raw_title)).strip()
        location = department = None
        for kind, raw_text in _JAZZHR_META_RE.findall(meta_html):
            text = html.unescape(raw_text).strip() or None
            if kind == "map-marker":
                location = text
            elif kind == "sitemap":
                department = text
        job_id = job_url.split("/apply/", 1)[-1].split("/", 1)[0]
        out.append(Job("jazzhr", token, job_id, title, location or "", job_url,
                       None, department))
    return out


# Teamtailor's hosted career page (`{token}.teamtailor.com`) is also plain
# server-rendered HTML, no public JSON API -- the real API
# (api.teamtailor.com) needs a per-company key, useless for probing
# companies we haven't onboarded. Ground-truthed against
# cigames.teamtailor.com and sessions.teamtailor.com. A nonexistent token
# 404s cleanly, unlike JazzHR's redirect-to-marketing-site trick.
_TEAMTAILOR_JOB_RE = re.compile(
    r'<a[^>]+href="(https://[^"]+/jobs/\d+-[^"]*)"[^>]*>\s*'
    r'(?:<span[^>]*></span>\s*)?(.*?)\s*</a>\s*'
    r'<div class="mt-1 text-md">(.*?)</div>',
    re.DOTALL,
)


def f_teamtailor(sess, token):
    """The per-job meta line (department/office/remote-type, separated by
    a middot span) has no consistent field-by-field meaning across
    companies -- confirmed live, one board's middle segment is a real
    department ("Finance"), another's is a legal-entity/office name ("CI
    Games SE"), not a department at all, and a company can configure
    fewer segments or none. Rather than guess a mapping that would be
    right for some companies and wrong for others, every segment is
    joined into one `location` string (informative, not fabricated) and
    department is left unset. _classify_workplace() still catches
    "remote"/"hybrid"/"onsite" out of that combined text the same way it
    already does for Greenhouse's unstructured locations.
    """
    url = f"https://{token}.teamtailor.com/jobs"
    try:
        r = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code != 200 or f"{token}.teamtailor.com" not in r.url:
        return None
    out = []
    for m in _TEAMTAILOR_JOB_RE.finditer(r.text):
        job_url, raw_title, meta_html = m.group(1), m.group(2), m.group(3)
        title = html.unescape(_WHITESPACE_RE.sub(" ", raw_title)).strip()
        meta_text = html.unescape(_HTML_TAG_RE.sub(" ", meta_html))
        segments = [s.strip() for s in meta_text.split("·") if s.strip()]
        location = ", ".join(segments)
        job_id_match = re.search(r"/jobs/(\d+)-", job_url)
        job_id = job_id_match.group(1) if job_id_match else job_url
        out.append(Job("teamtailor", token, job_id, title, location, job_url, None, None))
    return out


# All endpoint shapes below are ground-truthed against real boards
# (greenhouse: jfrog, wiz.io; ashby: snyk, ramp; lever: lever's own token;
# workable: huggingface; smartrecruiters: see the empty-content guard
# above; recruitee: bogus subdomains 404 cleanly; personio: personio's own
# token, bogus tokens redirect to personio.com; jazzhr: firstadvantage,
# talentwwinc (career.io), bogus subdomains 302 to jazzhr.com; teamtailor:
# cigames, sessions, bogus subdomains 404 cleanly). No 403s seen with the
# ats-probe UA; swap for a browser UA if that changes.
#
# KNOWN_FALSE_POSITIVES below are guessed tokens that ARE a real, valid
# board on that ATS, just for the wrong company (a common word/name
# collides with an unrelated small business's slug). Nothing about the
# API response can tell "right company" from "wrong company," so these
# were caught by spot-checking suspicious results and verified by hand.
# Add to this set as more turn up.
KNOWN_FALSE_POSITIVES: set[tuple[str, str]] = {
    ("ashby", "matrix"),          # matrix.co.il: real board is a Boston VC firm, not Matrix IT
    ("smartrecruiters", "trigo"), # trigo.tech: real board is an unrelated French/Moroccan company
    ("greenhouse", "iai"),        # iai.co.il: real board is a UK company, not Israel Aerospace Industries
    ("recruitee", "max"),         # max.co.il: real board is a German agency's demo/template listing
    ("personio", "amazon"),       # amazon.com: same small tenant as personio:salesforce and personio:max below
    ("personio", "salesforce"),   # salesforce.com, see above
    ("personio", "max"),          # max.co.il, second collision on top of the recruitee one above
    ("personio", "hpe"),          # hpe.com: unrelated German tenant (job titles in German)
    ("personio", "matrix"),       # matrix.co.il, second collision on top of the ashby one above
    ("personio", "monday"),       # monday.com: real board is "Monday" coworking spaces (Spain/Portugal)
    ("jazzhr", "electra"),        # electra.co.il: real board is "Electra Aero," an unrelated US eVTOL company
    ("jazzhr", "intuit"),         # intuit.com: real title says "Intuit - Career Page" but the one posting is
                                   # literally titled "Sample Job" -- an unconfigured demo tenant, not the
                                   # real Intuit. Titled convincingly enough that the Inactive-Career-Page
                                   # check above doesn't catch it, hence the explicit entry.
}

FETCHERS: dict[str, Callable] = {
    "greenhouse": f_greenhouse,
    "personio": f_personio,
    "lever": f_lever,
    "ashby": f_ashby,
    "workable": f_workable,
    "recruitee": f_recruitee,
    "smartrecruiters": f_smartrecruiters,
    "jazzhr": f_jazzhr,
    "teamtailor": f_teamtailor,
}

# Comeet: not guessable like the ATSes above. The API needs an opaque
# per-company `token` + `uid`, not derivable from the domain. Recovered
# server-side from two embeds on the company's own careers page, no JS
# execution needed:
#   1. WordPress plugin: var comeetvar = {"comeet_token":"...","comeet_uid":"91.001",...}
#      (ground-truthed: aquasec.com, silverfort.com)
#   2. Comeet's generic embed widget: COMEET.init({"token":"...","company-uid":"B1.001",...})
#      (ground-truthed: overwolf.com; can arrive double-escaped on
#      streamed React payloads, so unescape before matching)
# Sites with neither embed (e.g. Coralogix) aren't caught here. This is
# a real subset of Comeet, not all of it.

COMEET_RE = re.compile(
    r'"(?:comeet_token|token)"\s*:\s*"([^"]+)"\s*,\s*"(?:comeet_uid|company-uid)"\s*:\s*"([^"]+)"'
)
COMEET_PATHS = ["/careers", "/careers/", "/jobs", "/about-us/careers", "/company/careers"]
# Shorter than TIMEOUT: this tries up to 2*len(COMEET_PATHS) URLs per
# MISS domain, so a slow/dead host is expensive; batch runs care more
# about not stalling than squeezing the last slow-but-alive host.
COMEET_TIMEOUT = 6


def _comeet_job(sess: requests.Session, j: dict, uid: str, token: str) -> Job:
    """Shared by f_comeet_scrape and _fetch_comeet_pin. Both hit the same
    positions endpoint via different token-discovery paths.

    time_updated is the only timestamp Comeet's API exposes; there's no
    separate created/posted field. A listing edited after it first went
    up (location tweak, typo fix) reports that edit time, not the
    original post date, so this understates age for edited listings.
    Same class of limitation as _parse_workday_posted_on's relative-string
    approximation above: a real signal, just not ground truth.
    """
    description = None
    description_chars = 0
    if FETCH_FULL_DESCRIPTIONS:
        # Confirmed live: the positions LIST endpoint (what `j` is) never
        # has a description at all, but the per-job detail endpoint
        # (position_url) has both `description` (role/company overview)
        # and `requirements` (qualifications) as separate HTML fields --
        # concatenated here so a listing reads the way the real posting
        # does, not split across two DB columns this schema doesn't have.
        detail = get_json(sess, j.get("position_url")) if j.get("position_url") else None
        if isinstance(detail, dict):
            raw = "\n\n".join(p for p in (_txt(detail.get("description")), _txt(detail.get("requirements"))) if p)
            description = _clean_text(raw) if raw else None
            description_chars = len(description) if description else 0

    return Job("comeet", f"{uid}:{token}", _txt(j.get("position_uid")),
               _txt(j.get("name")), _txt(j.get("location")),
               _txt(j.get("careers_page_active_url") or j.get("careers_page_url")),
               _normalize_date(j.get("time_updated")), _txt(j.get("department")) or None,
               description_chars, description,
               seniority=_COMEET_LEVEL_MAP.get(_txt(j.get("experience_level")).lower()) or None,
               workplace_type=_ATS_WORKPLACE_MAP.get(_txt(j.get("Remote")).lower()) or None)


def f_comeet_scrape(sess: requests.Session, domain: str) -> tuple[list[Job], str] | None:
    """Try to recover a Comeet uid+token pair from `domain`'s own careers
    page and, if found, fetch real postings. Returns (jobs, "uid:token")
    or None. Shaped differently from the other fetchers because it needs
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
            # newline byte, and \s* in the regex can't see across that.
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
            out = [_comeet_job(sess, j, uid, token) for j in jobs]
            return out, f"{uid}:{token}"
    return None


def _fetch_comeet_pin(sess: requests.Session, uid: str, token: str) -> list[Job] | None:
    """Same API call as f_comeet_scrape's tail end, split out so a pinned
    uid/token from companies.yml can skip the page-scraping step entirely.
    """
    jobs = get_json(sess, f"https://www.comeet.com/careers-api/1.0/company/{uid}/positions?token={token}")
    if not isinstance(jobs, list):
        return None
    return [_comeet_job(sess, j, uid, token) for j in jobs]


# Best-effort tier: for domains that miss every guessable/pinned ATS above.
# Scrapes the company's own careers page (never a third party), two ways:
#
#   1. Embed-link detection: find a link/script pointing at an ATS this
#      file already knows how to query (including Workday, which needs an
#      unguessable tenant+site pair like Comeet). Still verified against
#      the real API before counting as a hit, just as trustworthy as a
#      guessed token, only found a different way.
#
#   2. schema.org JobPosting JSON-LD: many custom career sites emit this
#      for Google for Jobs SEO. No live API to verify against, so these
#      are tagged confidence='best_effort', never blended into 'verified'.

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
    """Workday's postedOn is a relative string ("Posted Today", "Posted 3
    Days Ago", "Posted 30+ Days Ago"), not an absolute date. Approximated:
    exact for small N, a floor for the open-ended "30+" bucket, still
    useful ghost-job signal, better than dropping to None.
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


def _workday_job_detail(sess: requests.Session, api_base: str, external_path: str, locations_text: str) -> tuple[str, str | None]:
    """One GET to the per-job detail endpoint, shared by two unrelated
    needs that happen to both live at jobPostingInfo: locationsText on
    the list endpoint collapses to "2 Locations" / "3 Locations" once a
    job has more than one office (the real names are at .location plus
    .additionalLocations), and the list endpoint never has a description
    at all (FETCH_FULL_DESCRIPTIONS wants .jobDescription). Only fetches
    when at least one of those actually applies, and only once even when
    both do. Falls back to the summary location text if the fetch fails;
    description stays None on failure, same as never having tried.

    api_base must be the /wday/cxs/{tenant}/{site} API prefix, not the
    human-browsable URL f_workday builds job.url from. The browsable one
    returns an HTML SPA shell with no embedded data, not JSON.
    """
    needs_location = bool(_WORKDAY_MULTI_LOCATION_RE.match(locations_text))
    if (not needs_location and not FETCH_FULL_DESCRIPTIONS) or not external_path:
        return locations_text, None

    detail = get_json(sess, f"{api_base}{external_path}")
    info = (detail or {}).get("jobPostingInfo") or {}

    location = locations_text
    if needs_location:
        names = [n for n in [_txt(info.get("location")), *(info.get("additionalLocations") or [])] if n]
        location = ", ".join(names) if names else locations_text

    description = _clean_text(info.get("jobDescription")) if FETCH_FULL_DESCRIPTIONS else None
    return location, description


def f_workday(sess: requests.Session, tenant: str, wd: str, site: str) -> list[Job] | None:
    api_base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    d = get_json_post(sess, f"{api_base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    if not isinstance(d, dict) or "jobPostings" not in d:
        return None
    # externalPath ("/job/<location>/<title>_<reqid>") omits the site slug,
    # even though the browsable URL requires it. Without /{site}, the
    # link silently bounces to a generic error page instead of 404ing.
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    out = []
    for j in d["jobPostings"]:
        bullets = j.get("bulletFields") or []
        external_path = _txt(j.get("externalPath"))
        location, description = _workday_job_detail(sess, api_base, external_path, _txt(j.get("locationsText")))
        out.append(Job("workday", f"{tenant}:{wd}:{site}", _txt(bullets[0] if bullets else j.get("externalPath")),
                       _txt(j.get("title")), location,
                       base + external_path,
                       _parse_workday_posted_on(j.get("postedOn")), None,
                       description_chars=len(description) if description else 0,
                       description=description))
    return out


JSONLD_JOBPOSTING_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)


def _extract_jobposting_jsonld(html: str, page_url: str) -> list[Job]:
    """schema.org JobPosting blocks, single or as an ItemList/@graph of
    several. No live API backs this, so every job is tagged ats="jsonld"
    and the loader marks it confidence='best_effort'.
    """
    out = []
    for block in JSONLD_JOBPOSTING_RE.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        # unwrap @graph and itemListElement, both are common containers
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
    """Resolve `domain` by scraping its own careers page: first for an
    embedded ATS link (including Workday), falling back to schema.org
    JobPosting JSON-LD. Returns (ats, jobs, token) or None.
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
    """Load companies.yml: {ats: {domain: {uid, token}}}. A missing file or
    missing pyyaml just means "no pins": this is an optional accelerant,
    never a hard dependency.
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
    """Re-poll an already-known ats+token directly, no guessing or
    scraping. The fast, frequent-running path (see --known), versus
    resolve()'s expensive discovery.

    token format per-ats: comeet is "uid:token", workday is
    "tenant:wd:site", jsonld is the page URL itself (re-fetched and
    re-extracted), everything else is the raw FETCHERS[ats] token.
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
        # Surfaced as an error, not downgraded to a silent MISS. Could
        # mean the board closed, the token rotated, or a transient
        # failure; --verbose shows which.
        res.error = f"known {ats}:{token} did not return a valid board on re-poll"
        return res
    res.ats, res.token, res.jobs = ats, token, _fill_classifications(jobs)
    res.job_count = len(jobs)
    return res


def _fill_classifications(jobs: list[Job]) -> list[Job]:
    """Fills in a text-keyword guess for any job whose fetcher didn't
    already set a structured seniority/workplace_type, plus skills and a
    salary estimate (only where no fetcher already set a real disclosed
    salary_text -- see f_ashby -- and only for an Israel-located job, per
    _estimate_salary's own docstring). Mutates in place, returns the
    same list.
    """
    for j in jobs:
        if j.seniority is None:
            j.seniority = _classify_seniority(j.title)
        if j.workplace_type is None:
            j.workplace_type = _classify_workplace(j.location)
        j.skills = _extract_skills(j.title, j.description)
        if j.salary_text is None and j.location and any(kw in j.location.lower() for kw in IL_KEYWORDS):
            estimate = _estimate_salary(j.title, j.description, j.seniority)
            if estimate:
                lo, hi = estimate
                j.salary_text = f"₪{lo}K–{hi}K"
                j.salary_is_estimate = True
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
                    print(f"    skip {ats}:{token}, known false positive", file=sys.stderr)
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
    # Comeet doesn't fit the token-guessing loop above. Try it once per
    # domain, only winning if nothing else found real postings. Expensive
    # (measured ~5x slower over 269 domains with it on); --no-comeet skips
    # it, companies.yml pins still apply either way.
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

    # Last resort: scrape the company's own careers page. Same cost
    # profile as Comeet, gated the same way; --no-embed-scrape skips it.
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
            # jsonld is unverified: only fills a total void, never
            # overrides an empty-but-confirmed board from a verified ATS.
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
    Bypasses get_json's JSON filter, so it also shows HTML error pages and
    redirects. Useful for eyeballing what an endpoint actually returns.
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
    """Shared by --batch and --known: runs resolve_one(item) across the
    worker pool, printing rows as they land (unless --json), then a
    summary.
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
    # to the cp1252 codepage, which crashes on the first such character.
    # Hit live during batch testing on --json output. UTF-8 output is safe
    # everywhere stdout ends up (terminal, file redirect, pipe).
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--domain")
    ap.add_argument("--batch")
    ap.add_argument("--known", help="JSON array of {domain,ats,token} (a prior --json output works as-is), "
                                     "re-poll known boards directly, no guessing. Fast path, meant to run often.")
    ap.add_argument("--fetch", help="ats:token, skip discovery")
    ap.add_argument("--raw", help="dump status/content-type/first 400 chars for a URL, no parsing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="print N sample jobs per hit")
    ap.add_argument("--verbose", action="store_true", help="print every probe to stderr")
    ap.add_argument("--no-comeet", action="store_true",
                     help="skip the Comeet careers-page scrape (companies.yml pins still apply), much faster batch runs")
    ap.add_argument("--no-embed-scrape", action="store_true",
                     help="skip the careers-page embed/JobPosting-JSON-LD fallback, much faster batch runs")
    ap.add_argument("--fetch-descriptions", action="store_true",
                     help="fetch each Comeet/Workday job's per-job detail page for a real description (neither "
                          "ATS's list endpoint has one). One extra request per listing on either -- meant for "
                          "scrape-discover.yml's slower pass, not the 10-min fast-poll.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    global VERBOSE, SCRAPE_COMEET, SCRAPE_EMBED, FETCH_FULL_DESCRIPTIONS
    VERBOSE = args.verbose
    SCRAPE_COMEET = not args.no_comeet
    FETCH_FULL_DESCRIPTIONS = args.fetch_descriptions
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
            # Filter whole comment lines before tokenizing. Tokenizing
            # the raw file let words inside a "# comment" (e.g. a domain
            # mentioned in passing) leak into the domain list.
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