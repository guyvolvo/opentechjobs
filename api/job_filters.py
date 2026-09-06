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


def build_jobs_where(params: dict) -> tuple[str, list]:
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
    _add_in_filter(where, args, params, "department", "department")
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
