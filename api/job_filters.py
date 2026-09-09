"""The /api/jobs filter-to-SQL translation, factored out of handler.py so
the alert evaluator (alerts.py, running in the scrape-fast Lambda) can
match a saved alert's filter against newly-seen jobs using the *exact*
same semantics a user sees on the live board -- not a second,
independently-maintained approximation that quietly drifts from it.

Copied into both Lambda deployment packages at build time (see
deploy-api.yml and deploy-scrape-lambda.yml); import it as a flat
top-level module (`from job_filters import ...`), not `api.job_filters`,
so the same import line works in both.
"""

import re

# Coarse, cross-company category -- complements the raw `department`
# column (kept as-is; still shown in the job detail view) rather than
# replacing it. Reported live: one company's "R&D" is another's
# "Engineering," Comeet/Workday/SmartRecruiters postings carry a
# structured department while JazzHR/Teamtailor carry none at all, so
# the "Category" filter (department's actual label in the UI -- see
# handler.py's top_departments comment) was really just "the top 20 raw
# strings by count," not a real taxonomy -- asked for by name: Security,
# Infrastructure, Operations, Software, none of which reliably existed
# as a single clean department string across companies.
#
# Matched against department+title together (either alone is sometimes
# missing or too specific/generic on its own), first match wins, so
# order runs specific -> general: "Security Engineer" needs to land in
# Security, not fall through to Software Engineering's generic
# "engineer" catch-all further down. Computed at query time via a
# registered SQLite function (see register_functions below), not stored
# on the row -- works instantly against every already-scraped job, no
# migration or backfill, and the taxonomy can still be tuned later
# without a re-scrape.
CATEGORIES = [
    "Security",
    "Data & AI",
    "Infrastructure",
    "QA",
    "Software Engineering",
    "Product & Design",
    "Sales & Marketing",
    "Customer Success",
    "Operations & Business",
]

_CATEGORY_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    (label, re.compile(r"\b(?:" + "|".join(re.escape(n) for n in needles) + r")\b", re.IGNORECASE))
    for label, needles in [
        ("Security", [
            "security", "cyber", "soc analyst", "threat intel", "threat research",
            "incident response", "penetration test", "pentest", "red team", "blue team",
            "\\bgrc\\b", "appsec", "application security", "infosec", "malware",
        ]),
        ("Data & AI", [
            "data scientist", "data science", "data engineer", "machine learning",
            "ml engineer", "ml researcher", "artificial intelligence", "ai researcher",
            "ai engineer", "\\bai\\b", "nlp", "computer vision", "data analyst",
            "business intelligence", "\\bbi\\b analyst",
        ]),
        ("Infrastructure", [
            "devops", "\\bsre\\b", "site reliability", "platform engineer",
            "infrastructure", "cloud engineer", "network engineer", "systems engineer",
            "system administrator", "sysadmin", "\\bnoc\\b", "help desk", "helpdesk",
            "it support", "it administrator",
        ]),
        ("QA", [
            "quality assurance", "\\bqa\\b", "test engineer", "test automation", "\\bsdet\\b",
        ]),
        ("Software Engineering", [
            "software engineer", "software development", "developer", "backend",
            "back-end", "back end", "frontend", "front-end", "front end", "full stack",
            "fullstack", "full-stack", "mobile engineer", "ios engineer",
            "android engineer", "embedded", "firmware", "web developer", "engineer",
            "engineering",
        ]),
        ("Product & Design", [
            "product manager", "product owner", "\\bux\\b", "\\bui\\b",
            "user experience", "user research", "product design", "graphic design",
        ]),
        ("Sales & Marketing", [
            "sales", "account executive", "business development", "marketing",
            "growth", "demand generation", "\\bseo\\b", "content writer", "partnerships",
        ]),
        ("Customer Success", [
            "customer success", "customer support", "technical support",
            "solutions engineer", "solution engineer", "support engineer",
            "customer experience",
        ]),
        ("Operations & Business", [
            "operations", "finance", "accounting", "human resources", "\\bhr\\b",
            "people team", "people partner", "legal", "office manager",
            "administrative", "procurement", "supply chain", "recruiter", "recruiting",
            "talent acquisition",
        ]),
    ]
]


def classify_category(department: str | None, title: str | None) -> str | None:
    """None when nothing matches -- real, not every posting fits one of
    these buckets cleanly (an executive assistant role, say), and
    forcing a guess would be worse than admitting it doesn't know.
    """
    text = f"{department or ''} {title or ''}"
    if not text.strip():
        return None
    for label, pattern in _CATEGORY_RULES:
        if pattern.search(text):
            return label
    return None


def register_functions(conn) -> None:
    """Register classify_category as SQLite's `category_of(department,
    title)`, usable directly in SQL (WHERE/GROUP BY/SELECT), so filtering
    and grouping by category still runs as one indexed-ish SQL query
    instead of a fetch-everything-then-filter-in-Python pass. Call once
    per connection -- handler.py's db.py and alerts.py's own sqlite3.connect
    each open their own connection, so each needs its own call.
    """
    conn.create_function("category_of", 2, classify_category)

IL_KEYWORDS = [
    "israel", "tel aviv", "tel-aviv", "telaviv", "herzliya", "raanana", "ra'anana",
    "rehovot", "netanya", "haifa", "jerusalem", "beer sheva", "beersheva",
    "petah tikva", "petah-tikva", "yokneam", "kfar saba", "kfar-saba",
    "ramat gan", "ramat-gan", "modiin", "modi'in",
    "caesarea", "yavne", "hod hasharon", "hod-hasharon", "bnei brak", "bnei-brak",
    "rosh haayin", "rosh-haayin", "tlv",
    # Added after finding these unmatched in real location strings.
    # "kiryat" ("town of") deliberately catches every Kiryat-prefixed city
    # in one entry. "Azur" was deliberately left out: too easily a false
    # match against "Azure" the technology.
    #
    # Every multi-word city above now has a hyphenated form too, not just
    # Tel Aviv -- reported live: "Ramat-Gan" (Sisense's own ATS location
    # string, hyphenated) didn't match the space-only "ramat gan" entry,
    # so a real Israeli listing was silently excluded from israel_only.
    "givatayim", "karmiel", "kiryat", "rishon", "yehud",
]

# A posting older than this is treated as an archived ghost listing, not
# a real open req. ATSes don't reliably mark outdated postings closed.
# Hidden from the board and stats by default (see include_outdated/
# include_closed params). NULL posted_at is kept, not hidden: unknown
# isn't evidence the posting has aged out.
BOARD_MAX_AGE_DAYS = 365
FRESH_CLAUSE = f"(posted_at IS NULL OR julianday('now') - julianday(posted_at) <= {BOARD_MAX_AGE_DAYS})"


def bool_param(params: dict, name: str) -> bool:
    return params.get(name, "").lower() in ("1", "true", "yes")


def _add_in_filter(where: list, args: list, params: dict, param_name: str, column: str) -> None:
    """?param=a,b,c -> `column IN (?,?,?)`. Shared by every multi-select
    filter (ats, company, department, seniority, etc.).
    """
    raw = params.get(param_name)
    if not raw:
        return
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        return
    where.append(f"{column} IN (%s)" % ",".join("?" * len(values)))
    args.extend(values)


def has_fts_index(conn) -> bool:
    """Whether this database carries the jobs_fts index.

    Callers pass the result into build_jobs_where. Checked rather than
    assumed because the same function serves the merged snapshot and the
    per-shard partitions, and those gain the index at different times.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs_fts'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def fts_escape(term: str) -> str:
    """Wrap a user term as an FTS5 string literal.

    FTS5's query language treats ", *, ^, -, NEAR, OR and friends as
    syntax, so a raw term like C++ or "senior-engineer" is either a
    syntax error or silently a different query. Quoting makes it a
    literal phrase, and doubling embedded quotes escapes them.
    """
    return '"' + term.replace('"', '""') + '"'


def build_jobs_where(params: dict, has_fts: bool = False) -> tuple[str, list]:
    """Same WHERE-clause construction route_jobs() uses for /api/jobs,
    minus sort/limit/offset (callers that need a full listing add those
    themselves; the alert evaluator only ever needs WHERE + first_seen).
    """
    where = ["1=1"]
    args: list = []

    if not bool_param(params, "include_closed"):
        where.append("closed_at IS NULL")

    confidence = params.get("confidence", "verified")
    if confidence == "verified":
        where.append("confidence = 'verified'")
    elif confidence == "best_effort":
        where.append("confidence = 'best_effort'")
    elif confidence != "all":
        raise ValueError("confidence must be one of: verified, best_effort, all")

    _add_in_filter(where, args, params, "ats", "ats")
    _add_in_filter(where, args, params, "company", "company_domain")
    # "department" is the param/UI name (see CATEGORIES above for why),
    # but it now filters on the normalized category_of(department,
    # title), not the raw column -- _add_in_filter just interpolates
    # whatever column expression it's given.
    _add_in_filter(where, args, params, "department", "category_of(department, title)")
    _add_in_filter(where, args, params, "seniority", "seniority")
    _add_in_filter(where, args, params, "location", "location")
    _add_in_filter(where, args, params, "workplace", "workplace_type")

    if not bool_param(params, "include_outdated"):
        where.append(FRESH_CLAUSE)

    if params.get("q"):
        q = f"%{params['q'].lower()}%"
        where.append(
            "(LOWER(title) LIKE ? OR LOWER(company_domain) LIKE ? OR LOWER(location) LIKE ? OR LOWER(department) LIKE ?)"
        )
        args.extend([q, q, q, q])

    if params.get("keywords"):
        # ';'-separated, ALL must appear (AND, not OR): "azure;excel;iso"
        # means the job mentions all three. Matched against title OR
        # description so it still works for ATSes with no description.
        for term in (t.strip() for t in params["keywords"].split(";")):
            if not term:
                continue
            like = f"%{term.lower()}%"
            if has_fts:
                # Descriptions no longer live in this table (see
                # db/schema.sql's jobs_fts and loader/descriptions.py), so
                # the text half of this match comes from the full-text
                # index instead of a LIKE scan. Faster too: a real index
                # rather than a substring scan over every open row.
                # Title still matches by substring, so a partial word in
                # a title behaves exactly as it always has.
                where.append(
                    "(LOWER(title) LIKE ? OR jobs.rowid IN "
                    "(SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?))"
                )
                args.extend([like, fts_escape(term)])
            else:
                # No FTS table in this database: a partition written
                # before the index existed, or any caller still holding a
                # snapshot with the description column populated. Falls
                # back to the original behaviour rather than silently
                # matching nothing, which is what a keyword filter
                # failing open would do to saved alerts.
                where.append("(LOWER(title) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ?)")
                args.extend([like, like])

    if bool_param(params, "israel_only"):
        clauses = " OR ".join("LOWER(location) LIKE ?" for _ in IL_KEYWORDS)
        where.append(f"({clauses})")
        args.extend(f"%{kw}%" for kw in IL_KEYWORDS)

    min_age = params.get("min_age_days")
    if min_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) >= ?")
        args.append(int(min_age))

    max_age = params.get("max_age_days")
    if max_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) <= ?")
        args.append(int(max_age))

    return " AND ".join(where), args
