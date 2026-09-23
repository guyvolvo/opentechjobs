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
from datetime import datetime, timedelta, timezone

# IL_KEYWORDS moved to countries.py, where the resolver that has to
# agree with it lives. Re-exported so every caller is unchanged.
from countries import ALPHA2, IL_FALSE_FRIENDS, IL_KEYWORDS  # noqa: F401
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


# A posting older than this is treated as an archived ghost listing, not
# a real open req. ATSes don't reliably mark outdated postings closed.
# Hidden from the board and stats by default (see include_outdated/
# include_closed params). NULL posted_at is kept, not hidden: unknown
# isn't evidence the posting has aged out.
BOARD_MAX_AGE_DAYS = 365
FRESH_CLAUSE = f"(posted_at IS NULL OR julianday('now') - julianday(posted_at) <= {BOARD_MAX_AGE_DAYS})"


def fresh_clause(caps: "SnapshotCaps") -> tuple[str, list]:
    """FRESH_CLAUSE, or its indexable twin when the snapshot allows it.

    julianday() on every row is a full scan, and it was half of every
    /api/jobs call: 0.54s to count 736k open rows on the box. Once
    posted_at is stored in one canonical UTC form (posted_at_utc in
    meta, set by the loader's --box pass) a plain string comparison
    against a cutoff computed here is the same test, and it runs off the
    posted_at index. The julianday form stays for snapshots that still
    carry mixed offsets, where string order would lie.
    """
    if caps.posted_at_utc:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BOARD_MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        # The unary + keeps the IS NULL half out of index planning. Left
        # bare, SQLite answers the OR with two index probes and a
        # 723k-rowid union to deduplicate them, 2.3s measured, where
        # walking one partial index with the OR as a row filter is
        # 0.2s and the ordered page off that index is under 5ms.
        return "(+posted_at IS NULL OR posted_at >= ?)", [cutoff]
    return FRESH_CLAUSE, []


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


def has_role_class(conn) -> bool:
    """Whether this database carries the role verdict column. The
    handler drops a roles= parameter when it does not, so a snapshot
    from before the column answers with every role rather than an
    error."""
    try:
        return any(r[1] == "role_class" for r in conn.execute("PRAGMA table_info(jobs)"))
    except Exception:
        return False


class SnapshotCaps:
    """What the snapshot in hand can do, read from the file itself.

    Returned by has_fts_index() in place of the bool it used to be, and
    truthy exactly when the full-text index is usable, so every
    `if has_fts:` and every build_jobs_where(params, has_fts_index(conn))
    call site reads as before. The extra fields let the query layer use
    what the box's loader adds (a category column, canonical posted_at)
    without a second signature to keep in step across the API, the
    alert evaluator and bootstrap.py.

    fts is usable only when the loader has marked the index complete
    (meta fts_complete = 1). Existence is not enough: after the outage
    fix the table came back empty and refilled with only the rows that
    changed since, and search answered from 1.9% of the corpus while
    claiming to search all of it. Not marked means not used.
    """

    __slots__ = ("fts", "fts_full", "category_col", "posted_at_utc", "board_indexes")

    def __init__(self, fts: bool = False, fts_full: bool = False,
                 category_col: str | None = None, posted_at_utc: bool = False,
                 board_indexes: bool = False):
        self.fts = fts
        self.fts_full = fts_full
        self.category_col = category_col
        self.posted_at_utc = posted_at_utc
        self.board_indexes = board_indexes

    def __bool__(self) -> bool:
        return self.fts

    @classmethod
    def coerce(cls, value) -> "SnapshotCaps":
        return value if isinstance(value, cls) else cls(fts=bool(value))


# Capabilities by connection, kept for a minute. Three schema reads per
# call is nothing on its own, but compute_scoped_stats asks per clause
# and the alert evaluator per alert, and the snapshot behind an open
# connection changes at most once a minute anyway (db.py's own recheck
# cadence on Lambda; the applier's timer on the box).
_CAPS_TTL_S = 60.0
_caps_cache: dict[int, tuple[float, SnapshotCaps]] = {}


def has_fts_index(conn) -> SnapshotCaps:
    """The snapshot's capabilities; truthy when jobs_fts is complete.

    Checked rather than assumed because the same function serves the
    merged snapshot and the per-shard partitions, and those gain the
    index at different times. See SnapshotCaps for why a table that
    merely exists does not count.
    """
    import time

    now = time.monotonic()
    hit = _caps_cache.get(id(conn))
    if hit and hit[0] > now:
        return hit[1]
    caps = _read_caps(conn)
    if len(_caps_cache) > 64:
        _caps_cache.clear()
    _caps_cache[id(conn)] = (now + _CAPS_TTL_S, caps)
    return caps


def _read_caps(conn) -> SnapshotCaps:
    # One statement, not four: test_scoped_stats budgets the schema
    # probes per request, and this is the probe the FTS check always
    # was, just answering more questions. The index's own CREATE text
    # says which columns it carries.
    try:
        fts_sql, category, fts_complete, posted_at_utc, board_indexes = conn.execute(
            "SELECT (SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs_fts'),"
            " (SELECT COUNT(*) FROM pragma_table_info('jobs') WHERE name = 'category'),"
            " (SELECT value FROM meta WHERE key = 'fts_complete'),"
            " (SELECT value FROM meta WHERE key = 'posted_at_utc'),"
            " (SELECT value FROM meta WHERE key = 'board_indexes')"
        ).fetchone()
    except Exception:
        return SnapshotCaps()
    complete = bool(fts_sql) and fts_complete == "1"
    return SnapshotCaps(
        fts=complete,
        fts_full=complete and "title" in (fts_sql or ""),
        category_col="category" if category else None,
        posted_at_utc=posted_at_utc == "1",
        board_indexes=board_indexes == "1",
    )


# Filters that have an index of their own, and so should pick it rather
# than be handed the one below.
_SELECTIVE_PARAMS = ("roles", "department", "company", "seniority", "workplace",
                     "ats", "ids", "search", "keywords", "skills", "q", "name")


def count_index_hint(params: dict, caps: "SnapshotCaps") -> str:
    """`INDEXED BY ...` for the "Showing 1-50 of N" count, or "".

    The count is the one query on the board with no ORDER BY, and
    without one SQLite has nothing pushing it towards an index: it
    compares a full scan of the table against a full scan of a covering
    index and, with no STAT4 in Ubuntu's build, treats them as roughly
    equal and takes the table. Measured on the box, 862,354 open
    listings: 11.6s scanning a 1GB table against 0.252s scanning the
    38MB index that answers the same question, and 26.4s if the
    freshness test is left to resolve as a two-range OR instead.

    So the index is named. Only for a request that carries no filter
    with a better index of its own, because naming one forbids the
    others: department=Security answers from idx_jobs_open_category in
    7ms and must not be dragged onto this one.

    Empty string wherever the snapshot has no board indexes, which is
    every Lambda snapshot, since INDEXED BY an index that is not there
    is an error rather than a hint.

    And empty wherever the WHERE does not satisfy the index's own
    partial predicate. idx_jobs_open_posted is built WHERE closed_at IS
    NULL AND confidence = 'verified', so a request that does not promise
    both cannot use it, and INDEXED BY an index SQLite cannot use is not
    ignored: it is the error "no query solution", a 500 in 2ms.

    That was live. The board sends confidence=all on every single
    request, so the only thing standing between it and a broken count
    was roles=tech being in the selective list above and skipping the
    hint. One click on All roles and every count on the board 500'd,
    which is why "Showing 1-50 of N" lost its N and the detail pane's
    headline sat on a skeleton that never resolved. Reported live with a
    screenshot.
    """
    if not caps.board_indexes:
        return ""
    if any(params.get(p) for p in _SELECTIVE_PARAMS):
        return ""
    # The partial index's own two conditions, asked of the request.
    if (params.get("confidence") or "verified") != "verified":
        return ""
    if bool_param(params, "include_closed"):
        return ""
    return " INDEXED BY idx_jobs_open_posted"


def category_sql(conn) -> str:
    """The category expression for a SELECT or GROUP BY on this snapshot.

    The stored column when the loader has filled it (empty string means
    "no bucket fits", which reads back as NULL here), else the Python
    callback, which costs a regex per row and made department=Security
    a 33 second query on 846k rows."""
    if has_fts_index(conn).category_col:
        return "NULLIF(category, '')"
    return "category_of(department, title)"


# A term FTS5's tokenizer would mangle. unicode61 drops "+" and "#", so
# "c++" and "c#" would both become a search for "c". Those go through
# the substring path instead, where they always worked.
def fts_safe(term: str) -> bool:
    return not any(ch in term for ch in "+#")


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
MAX_MATCH_SKILLS = 40


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
    as booleans. Forty of them is the cap and they run over a column
    that is at most fifteen comma-joined labels, so it stays cheap.

    The count saturates: probe.py stores only the first fifteen skills it
    finds in a job, so a row matching more of yours than that still scores fifteen.
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
# Past this many terms a query costs more than it can be worth. The
# extras used to be dropped in silence; callers are told now (route_jobs
# returns them, and the board says so).
MAX_SEARCH_TERMS = 10


def search_terms(raw: str, limit: int | None = MAX_SEARCH_TERMS) -> list[str]:
    """Whitespace-separated, with "quoted phrases" kept whole."""
    out = []
    for match in re.findall(r'"([^"]*)"|(\S+)', raw or ""):
        term = (match[0] or match[1]).strip()
        if term:
            out.append(term)
    return out if limit is None else out[:limit]


# Whole words, not substrings. NOT IN USE -- see the note below.
#
# Reverted 2026-09-22, hours after it shipped, because it made the site
# unusable. Timed live against 805,863 jobs: search=python returned HTTP
# 500 after 29 seconds on both sorts, which is the Lambda's timeout, and
# search=engineer did the same on relevance. Rare terms were fine and
# common ones died, because the cost scales with how many rows the LIKE
# lets through to the GLOB: for a term matching a large share of the
# table that is a string concatenation and a GLOB per row per column,
# twice over, once for the listing and once for the count behind
# "Showing 1-50 of N".
#
# The correctness argument below is still right and the fix belongs in
# FTS5, which tokenises and has an index, and is why the description
# half of this search never had the Trust Officer problem in the first
# place. Extend the existing FTS table (loader/rebuild_fts.py) to carry
# title, company_domain, location and department, then match against it
# here. boundary_glob and word_match_sql are kept for that work and for
# the fallback a snapshot predating those columns will need.
#
# What went wrong is worth naming: this shipped with 25 correctness
# assertions against an eight-row fixture and no measurement of what the
# query costs on the real table.
#
# Reported live: searching "rust" returned Trust Officer, Entrust
# Identity and Begeleider - Buitenrust, because every field was matched
# with LIKE '%rust%'. SQLite's LIKE has no character classes, and
# registering a Python REGEXP callback would run per row over 600k rows.
#
# GLOB does have character classes, and is one operation per field
# rather than the sixteen nested REPLACEs that normalising the haystack
# would need. The value is padded with spaces so a term at either end
# still has a boundary character beside it, and lowercased because GLOB
# is case-sensitive where LIKE is not.
#
# The plain LIKE stays in front of it as a prefilter. Both are full
# scans, but LIKE is the cheaper of the two and SQLite evaluates the
# terms of an AND in order here, so the class match only runs on rows
# that could possibly match.
#
# "+" and "#" are deliberately not boundary characters: C++ and C# are
# searches people actually make, and treating those as separators would
# turn both into a bare "c".
GLOB_META = ("*", "?", "[", "]")


def boundary_glob(term: str) -> str | None:
    """A GLOB pattern matching `term` only as a whole word, or None when
    the term carries GLOB syntax of its own and the caller should fall
    back to the plain substring match."""
    low = term.lower()
    if not low or any(ch in low for ch in GLOB_META):
        return None
    return f"*[^a-z0-9+#]{low}[^a-z0-9+#]*"


def word_match_sql(column: str) -> str:
    """`column` holds the term as a whole word. Two placeholders: the
    LIKE prefilter, then the boundary pattern."""
    return (f"(LOWER(COALESCE({column}, '')) LIKE ?"
            f" AND ' ' || LOWER(COALESCE({column}, '')) || ' ' GLOB ?)")


# Every term must appear ("all", the default) or any one of them may
# ("any"). Never inferred: a search that finds nothing is broadened by
# the reader asking for it, so the results always answer the question
# that was actually put.
def search_mode(params: dict) -> str:
    return "any" if (params.get("search_mode") or "").lower() == "any" else "all"


# What a term matching each field is worth, for sort=relevance. A term in
# the title is the strongest signal a listing can give; one buried in a
# long description is the weakest. Relevance is never the default sort
# (route_jobs keeps newest), so nothing about the board's usual order
# depends on these numbers.
RELEVANCE_WEIGHTS = {"title": 10, "company_domain": 7, "location": 6, "department": 5}
RELEVANCE_DESCRIPTION = 2
# The whole query, in that order, inside the title.
RELEVANCE_PHRASE_BONUS = 40
# Every term in the title, in any order.
RELEVANCE_ALL_IN_TITLE_BONUS = 30


def relevance_score_sql(params: dict, has_fts=False) -> tuple[str, list]:
    """How well each row answers the search, as a SQL expression.

    Field-weighted and countable by hand: no hidden model, and a reader
    asking why a row is where it is can be told. "0" when there is no
    search to score against, which route_jobs treats as no relevance sort
    to do.
    """
    terms = search_terms(params.get("search") or "")
    if not terms:
        return "0", []
    caps = SnapshotCaps.coerce(has_fts)
    parts, args = [], []
    for term in terms:
        like = f"%{term.lower()}%"
        for column, weight in RELEVANCE_WEIGHTS.items():
            parts.append(f"(CASE WHEN LOWER(COALESCE({column}, '')) LIKE ? THEN {weight} ELSE 0 END)")
            args.append(like)
        if caps.fts_full and fts_safe(term):
            # Column-scoped, so a term in the title does not also score
            # as a description hit.
            parts.append(f"(CASE WHEN jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)"
                         f" THEN {RELEVANCE_DESCRIPTION} ELSE 0 END)")
            args.append("description : " + fts_escape(term))
        elif caps.fts:
            parts.append(f"(CASE WHEN jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)"
                         f" THEN {RELEVANCE_DESCRIPTION} ELSE 0 END)")
            args.append(fts_escape(term))
        else:
            parts.append(f"(CASE WHEN LOWER(COALESCE(description, '')) LIKE ? "
                         f"THEN {RELEVANCE_DESCRIPTION} ELSE 0 END)")
            args.append(like)
    if len(terms) > 1:
        whole = " ".join(terms).lower()
        parts.append(f"(CASE WHEN LOWER(title) LIKE ? THEN {RELEVANCE_PHRASE_BONUS} ELSE 0 END)")
        args.append(f"%{whole}%")
        every = " AND ".join("LOWER(title) LIKE ?" for _ in terms)
        parts.append(f"(CASE WHEN {every} THEN {RELEVANCE_ALL_IN_TITLE_BONUS} ELSE 0 END)")
        args.extend(f"%{t.lower()}%" for t in terms)
    return "(" + " + ".join(parts) + ")", args


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


# The job ids a caller asked for.
#
# This is what the Saved view is built on: the board holds a reader's
# stars as ids and asks for exactly those rows, so a star stays visible
# whether or not the job happens to be in the 50 rows currently loaded.
#
# Validated rather than interpolated, same rule as the country codes
# above. An id here is loader/load_to_sqlite.py's job_id(), an md5
# hexdigest slice, so lowercase hex is the only shape a real one takes
# and anything else cannot match a row anyway. Dropped rather than
# rejected, so a stale star naming a job from before a re-keying narrows
# the request instead of erroring the whole board out.
#
# 200 is the cap because a request naming every star a person has is
# still one bound parameter each, and an unbounded IN list is a query
# SQLite can refuse to compile at all.
MAX_IDS_FILTER = 200
JOB_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def is_job_id(value) -> bool:
    """Whether this could be an id this database holds. Used by the API's
    own /me/saved routes too, so a saved row can never be written for
    something that is not a job.
    """
    return isinstance(value, str) and JOB_ID_RE.match(value) is not None


def wanted_ids(params: dict) -> list[str]:
    out: list[str] = []
    for part in (params.get("ids") or "").split(","):
        jid = part.strip()
        if is_job_id(jid) and jid not in out:
            out.append(jid)
    return out[:MAX_IDS_FILTER]


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


def israel_clause(places: bool) -> tuple[str, list]:
    """The Israel-only filter as one SQL clause plus its bound arguments.

    Two ways to ask the same question, and one of them is free. The
    country column already holds this answer, derived from these very
    keywords by countries_of (countries.py says why the two are required
    to agree), so reading it is a single LIKE against a short list of
    codes. Matching the raw location text instead is forty LIKEs over
    free text, and no index helps either shape.

    Measured live before this existed: /api/facets?israel_only=1 took
    14.2s against country=IL's 3.9s, and /api/jobs 5.8s against 1.3s.
    The two disagreed on 2 rows in 3,002, both of them rows the column
    had gone stale on rather than rows the keywords read better.

    The keyword form stays for a snapshot written before the column
    existed, on the same degrade-rather-than-error rule has_places is
    there for.
    """
    if places:
        return "(',' || COALESCE(country, '') || ',') LIKE ?", ["%,IL,%"]
    # REPLACE, not NOT LIKE. A location can name a real Israeli office
    # and a Beth Israel hospital in the same string, and NOT LIKE would
    # throw the whole row away. Blanking the false friend first leaves
    # every other keyword free to match what is left, which is exactly
    # what countries.matches_israel does in Python.
    haystack = "LOWER(COALESCE(location, ''))"
    for name in IL_FALSE_FRIENDS:
        haystack = f"REPLACE({haystack}, '{name}', ' ')"
    return ("(%s)" % " OR ".join(f"{haystack} LIKE ?" for _ in IL_KEYWORDS),
            [f"%{kw}%" for kw in IL_KEYWORDS])


def build_jobs_where(params: dict, has_fts=False,
                     places: bool = True) -> tuple[str, list]:
    """Same WHERE-clause construction route_jobs() uses for /api/jobs,
    minus sort/limit/offset (callers that need a full listing add those
    themselves; the alert evaluator only ever needs WHERE + first_seen).

    has_fts is a SnapshotCaps from has_fts_index(), or the bare bool it
    used to be; both are accepted so no caller had to change.
    """
    caps = SnapshotCaps.coerce(has_fts)
    has_fts = caps.fts
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
    _add_in_filter(where, args, params, "department", caps.category_col or "category_of(department, title)")
    _add_in_filter(where, args, params, "seniority", "seniority")
    _add_in_filter(where, args, params, "location", "location")
    _add_in_filter(where, args, params, "workplace", "workplace_type")
    # roles=tech: only listings the classifier (api/role_class.py) judged
    # technical work. Anything else, or nothing, is every role. The
    # handler drops the parameter when the snapshot predates the column.
    if (params.get("roles") or "").lower() == "tech":
        where.append("role_class = 'tech'")

    wanted_id_list = wanted_ids(params)
    if wanted_id_list:
        where.append("id IN (%s)" % ",".join("?" * len(wanted_id_list)))
        args.extend(wanted_id_list)
    # No else branch on purpose. An ids param that survives validation
    # empty (every id malformed, or a bare "ids=") adds no clause at all,
    # so the board comes back unfiltered rather than empty, which is how
    # every other filter in this file treats a value it cannot use.

    if not bool_param(params, "include_outdated"):
        fresh_sql, fresh_args = fresh_clause(caps)
        where.append(fresh_sql)
        args.extend(fresh_args)

    if params.get("q"):
        q = f"%{params['q'].lower()}%"
        where.append(
            "(LOWER(title) LIKE ? OR LOWER(company_domain) LIKE ? OR LOWER(location) LIKE ? OR LOWER(department) LIKE ?)"
        )
        args.extend([q, q, q, q])

    wanted = wanted_skills(params)
    # Best matches on the board and an alert built from a CV both mean
    # listings that share at least one skill. There used to be a rank-only
    # mode for the board that kept every listing and just ordered it, and
    # it made the count and the filters look broken: Israel in Best
    # matches said 2,612 listings, the same as All listings. Reported live.
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
        # "any" collects each term's clause and ORs them at the end;
        # "all" appends them to where, which ANDs them.
        any_mode = search_mode(params) == "any"
        any_parts: list[str] = []
        any_args: list = []
        for term in search_terms(params["search"]):
            like = f"%{term.lower()}%"
            # Every field a job has an answer for. company_domain rather
            # than the company's real name because that name lives in the
            # companies table, and this same function runs in the alert
            # evaluator, which queries jobs on its own with no join.
            columns = ("title", "company_domain", "location", "department")
            if caps.fts_full and fts_safe(term):
                # One index lookup across all five columns. Whole words,
                # which is what the reverted GLOB attempt above was for:
                # "rust" no longer answers with Trustly, and it costs a
                # b-tree probe rather than four LIKE scans and a GLOB.
                parts = ["jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)"]
                term_args = [fts_escape(term)]
            elif has_fts:
                parts = [f"LOWER(COALESCE({c}, '')) LIKE ?" for c in columns]
                term_args = [like] * len(columns)
                parts.append("jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
                term_args.append(fts_escape(term))
            else:
                parts = [f"LOWER(COALESCE({c}, '')) LIKE ?" for c in columns]
                term_args = [like] * len(columns)
                # Same fallback as keywords below: a partition written
                # before the index existed still carries the column.
                parts.append("LOWER(COALESCE(description, '')) LIKE ?")
                term_args.append(like)
            if any_mode:
                any_parts.append("(" + " OR ".join(parts) + ")")
                any_args.extend(term_args)
            else:
                where.append("(" + " OR ".join(parts) + ")")
                args.extend(term_args)
        if any_mode and any_parts:
            where.append("(" + " OR ".join(any_parts) + ")")
            args.extend(any_args)

    if params.get("keywords"):
        # ';'-separated, ALL must appear (AND, not OR): "azure;excel;iso"
        # means the job mentions all three. Matched against title OR
        # description so it still works for ATSes with no description.
        for term in (t.strip() for t in params["keywords"].split(";")):
            if not term:
                continue
            like = f"%{term.lower()}%"
            if caps.fts_full and fts_safe(term):
                where.append("jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
                args.append(fts_escape(term))
            elif has_fts:
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
        clause, il_args = israel_clause(places)
        where.append(clause)
        args.extend(il_args)

    min_age = params.get("min_age_days")
    if min_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) >= ?")
        args.append(int(min_age))

    max_age = params.get("max_age_days")
    if max_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) <= ?")
        args.append(int(max_age))

    return " AND ".join(where), args
