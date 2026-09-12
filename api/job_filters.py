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

from countries import ALPHA2
from skills import SKILL_LABELS

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


def salary_source_select(conn) -> str:
    """salary_source if the snapshot has it, otherwise the same answer
    derived from the flag that has always been there.

    Same deployment-clock problem as _has_company_name: code ships in
    seconds, the snapshot gains a column only when the merge next runs.
    Unlike company_name this degrades to a real value rather than NULL,
    because the old boolean already carries the distinction the new
    column refines. Every estimate before the learned model came from
    probe.py's table, so "salary_is_estimate = 1" means "table" for every
    row written before this column existed. API consumers therefore see
    the new field working from the moment it deploys, and it simply gets
    more precise once the column lands.
    """
    has_column = False
    try:
        has_column = any(r[1] == "salary_source" for r in conn.execute("PRAGMA table_info(jobs)"))
    except Exception:
        pass
    if has_column:
        return "salary_source"
    return ("CASE WHEN salary_text IS NULL OR salary_text = '' THEN NULL "
            "WHEN salary_is_estimate = 1 THEN 'table' ELSE 'disclosed' END AS salary_source")


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


# The skills a caller asked to match on, as canonical labels.
#
# Validated against the vocabulary rather than passed through, which is
# what lets the SQL below interpolate them into a LIKE without escaping:
# nothing outside SKILL_LABELS survives this, and no label contains a
# LIKE wildcard. It also means a stale bookmark naming a skill we have
# since dropped narrows the match instead of erroring the board out.
MAX_MATCH_SKILLS = 20


def wanted_skills(params: dict) -> list[str]:
    known = {s.lower(): s for s in SKILL_LABELS}
    out: list[str] = []
    for part in (params.get("skills") or "").split(","):
        label = known.get(part.strip().lower())
        if label and label not in out:
            out.append(label)
    return out[:MAX_MATCH_SKILLS]


def skills_score_sql(wanted: list[str]) -> tuple[str, list]:
    """How many of `wanted` a row carries, as a SELECT expression.

    SQLite has no set intersection, so this is one LIKE per skill summed
    as booleans. Twenty of them is the cap and they run over a column
    that is at most five comma-joined labels, so it stays cheap.

    The count saturates: probe.py stores only the first five skills it
    finds in a job, so a row matching six of yours still scores five.
    Fine for ranking, which is all it is for.
    """
    if not wanted:
        return "0", []
    expr = " + ".join("((',' || COALESCE(skills, '') || ',') LIKE ?)" for _ in wanted)
    return f"({expr})", [f"%,{s},%" for s in wanted]


# One search box, one param.
#
# It replaces two that a reader had to choose between without being told
# the difference: q matched a substring of the title, company, location
# or department, and keywords was semicolon-separated, AND-matched, and
# the only one that read the job description. Nobody could be expected to
# know that "kubernetes" in the left box and the right box asked
# different questions.
#
# So: whitespace-separated words, all of which must appear, each matched
# against everything we hold about a job, description included. That is
# what a search box is assumed to do. Quotes keep a phrase whole.
#
# q and keywords are still honoured below, because saved alerts carry
# them and a filter someone saved in March must keep meaning what it
# meant in March.
def search_terms(raw: str) -> list[str]:
    """Whitespace-separated, with "quoted phrases" kept whole."""
    out = []
    for match in re.findall(r'"([^"]*)"|(\S+)', raw or ""):
        term = (match[0] or match[1]).strip()
        if term:
            out.append(term)
    return out[:10]


# The countries a caller asked for, as codes this vocabulary knows.
#
# Validated rather than passed through, which is what lets the SQL
# interpolate them into a LIKE without escaping, and what makes a stale
# link naming a code we have since dropped narrow the filter instead of
# erroring the board out. Same rule as wanted_skills below it.
MAX_COUNTRIES_FILTER = 20


def wanted_country_codes(params: dict) -> list[str]:
    out: list[str] = []
    for part in (params.get("country") or "").split(","):
        code = part.strip().upper()
        if code in ALPHA2 and code not in out:
            out.append(code)
    return out[:MAX_COUNTRIES_FILTER]


# The cities a caller asked for, by canonical name.
#
# Not validated the way wanted_country_codes validates codes, because
# there is no set to validate against: countries.py keeps a city it does
# not recognise under its own written name, deliberately, so that a place
# nobody has added to the gazetteer still appears on the board. The
# vocabulary is open, so a name arriving from a link can be one this
# process has never seen and still be right.
#
# Sanitising is what replaces validation, since the SQL below interpolates
# nothing and the names go in as bound parameters, but a LIKE pattern is
# still a pattern. A "%" or a "_" inside a name is a wildcard and would
# match places nobody asked for. A "," would split one name into two
# across the comma-wrapping the match depends on. No real city name holds
# any of the three, so a name that does is dropped rather than escaped:
# the filter narrows, exactly as a stale country code does, instead of
# erroring the board out.
MAX_CITIES_FILTER = 20


def wanted_cities(params: dict) -> list[str]:
    out: list[str] = []
    for part in (params.get("city") or "").split(","):
        name = part.strip()
        # The comma test cannot fire while the input is split on commas.
        # It is written anyway so this stays correct if a caller ever
        # hands the names over already split.
        if not name or "," in name or "%" in name or "_" in name:
            continue
        if name not in out:
            out.append(name)
    return out[:MAX_CITIES_FILTER]


def has_places(conn) -> bool:
    """Whether this database carries the country and city columns.

    Same reason has_fts_index exists, and the same trap load_to_sqlite.py
    warns about at _NEW_COLUMNS: this Lambda's code and the database it
    reads deploy on completely separate clocks. Code ships in seconds,
    jobs-read.db only gains a column when the merge next rebuilds it, up
    to an hour later. Shipping the country filter before that window
    closed turned every ?country= request into "no such column: country",
    exactly as that comment predicted for company_name. Confirmed live,
    on the very deploy the comment was warning about.

    Degrading means the filter is ignored for one merge cycle, so a
    reader sees more listings than they asked for rather than an error.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        return "country" in cols and "city" in cols
    except Exception:
        return False


def build_jobs_where(params: dict, has_fts: bool = False,
                     places: bool = True) -> tuple[str, list]:
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

    wanted = wanted_skills(params)
    if wanted:
        # OR, not AND. This is the CV match, and a person who knows a
        # dozen things is not looking for the job that demands all
        # twelve. It ranks by how many overlap instead (see
        # skills_score_sql); the filter only decides who is on the list.
        #
        # Matched against the tagged `skills` column rather than the
        # description, because both sides already speak one vocabulary:
        # probe.py tags every job from skills.py and the CV analyser
        # reads a CV with the same terms. A LIKE over descriptions would
        # find "no Python experience required" and call it a match.
        clauses = " OR ".join("(',' || COALESCE(skills, '') || ',') LIKE ?" for _ in wanted)
        where.append(f"({clauses})")
        args.extend(f"%,{s},%" for s in wanted)

    wanted_countries = wanted_country_codes(params) if places else []
    if wanted_countries:
        # OR across codes, and a LIKE against the comma-joined column for
        # the same reason skills uses one: a job can name more than one
        # country, and "Remote, Canada; Remote, Israel" has to be findable
        # under either. Comma-wrapped so IL never matches the IL inside
        # some future three-letter code.
        clauses = " OR ".join("(',' || COALESCE(country, '') || ',') LIKE ?" for _ in wanted_countries)
        where.append(f"({clauses})")
        args.extend(f"%,{c},%" for c in wanted_countries)

    wanted_city_names = wanted_cities(params) if places else []
    if wanted_city_names:
        # Same shape as country above, against a column stored the same
        # comma-joined way, and OR across the names for the same reason: a
        # posting naming two offices has to be findable under either.
        # Comma-wrapped so "Haifa" never matches inside some longer name
        # that happens to contain it.
        #
        # Combined with country by AND, like every other filter here, so
        # country=IL&city=Haifa asks for both and a Haifa in some other
        # country would not answer it.
        clauses = " OR ".join("(',' || COALESCE(city, '') || ',') LIKE ?" for _ in wanted_city_names)
        where.append(f"({clauses})")
        args.extend(f"%,{c},%" for c in wanted_city_names)

    if params.get("search"):
        for term in search_terms(params["search"]):
            like = f"%{term.lower()}%"
            # Every field a job has an answer for. company_domain rather
            # than the company's real name because that name lives in the
            # companies table, and this same function runs in the alert
            # evaluator, which queries jobs on its own with no join.
            parts = ["LOWER(title) LIKE ?", "LOWER(company_domain) LIKE ?",
                     "LOWER(location) LIKE ?", "LOWER(COALESCE(department, '')) LIKE ?"]
            term_args = [like, like, like, like]
            if has_fts:
                parts.append("jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
                term_args.append(fts_escape(term))
            else:
                # Same fallback as keywords below: a partition written
                # before the index existed still carries the column.
                parts.append("LOWER(COALESCE(description, '')) LIKE ?")
                term_args.append(like)
            where.append("(" + " OR ".join(parts) + ")")
            args.extend(term_args)

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
