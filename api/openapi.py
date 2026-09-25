"""The OpenAPI 3.1 description of this API, served at /api/openapi.json
and rendered by Swagger UI at /api/help.

Hand-written, like everything else here. There is no framework to
generate it from: handler.py is a dispatch table of `if path == ...`
against a SQLite connection, so a spec derived from decorators would
need decorators first. What this does instead is keep one file whose
only job is the description beside the file whose only job is the
routes, and the response shapes below were read off live responses
rather than guessed from the code.

The prose is the site owner's, carried over from the hand-written page
this replaced (2026-09-16) and not ours to edit. Only the structure is
ours. Anyone tempted to "fix" the wording, including the Title Case,
should leave it alone and raise it with the owner instead.

The examples are deliberately global and anonymous, on the owner's
instruction: country=US rather than IL, placeholder domains rather than
real customers, a placeholder job id rather than a live one.
israel_only stays documented because it is a real parameter the API
accepts; removing it would make the reference wrong rather than global.

/contact is not described here. It is the contact form's own POST
endpoint, it was not on the page this replaces, and a documented mail
sender is an invitation. It still works; it is simply not advertised.
"""

SITE = "https://opentechjobs.org"


def _q(name, desc, example=None, schema=None):
    """One query parameter.

    `example` is deliberately rare. Swagger UI pre-fills every example
    into the Try it out form, so an example on each of these meant a bare
    Execute on /jobs sent seven filters at once and answered with an
    empty list: the first thing anyone pressed returned nothing. The
    owner's descriptions already carry their examples inline, which is
    where an example belongs when the form is live. It stays only where
    the parameter is required and the call fails without it.
    """
    param = {
        "name": name,
        "in": "query",
        "description": desc,
        "schema": schema or {"type": "string"},
    }
    if example is not None:
        param["example"] = example
    return param


# Every /api/jobs filter, written once. /stats, /facets and
# /companies/search all say "accepts /api/jobs filters", and reusing the
# list is what keeps that promise true rather than three copies of it.
SEARCH_PARAMS = [
    _q("search",
       "Space-separated search terms (e.g., kubernetes new york). Matches title, company, "
       "location, category, and description. Use quotes for exact phrases "
       "(e.g., \"software engineer\").",
       ),
    _q("q", "Legacy title/company search."),
    _q("keywords",
       "Semicolon-separated terms (e.g., azure;excel;iso). Matches title and description.",
       ),
]

FILTER_PARAMS = [
    _q("country", "Two-letter country code (e.g., US or US,GB). Uses normalized location data."),
    _q("city", "City names (e.g., New York,London)."),
    _q("location", "Raw location text provided by the employer."),
    _q("company", "Company domain names (e.g., example.com,example.org)."),
    _q("ats", "Applicant tracking system names (e.g., greenhouse,lever)."),
    _q("department",
       "Normalized job categories (e.g., Security, Infrastructure). Fetch valid values via /api/facets."),
    _q("seniority",
       "intern, junior, mid, senior, staff, principal, lead, manager, director, exec."),
    _q("workplace", "remote, hybrid, onsite."),
    _q("skills",
       "Comma-separated skill names (e.g., Python,Kubernetes). Filters jobs matching at least one "
       "skill and sets sort=match."),
    _q("ids", "Fetch specific job IDs in a single request."),
    _q("israel_only", "Set to 1 to restrict to Israeli locations (Equivalent to country=IL)."),
    _q("confidence",
       "verified (default, confirmed on employer site), best_effort, or all.",
       schema={"type": "string", "enum": ["verified", "best_effort", "all"], "default": "verified"}),
    _q("include_closed", "Set to 1 to include roles closed within the last 30 days."),
    _q("include_outdated", "Set to 1 to include open postings older than 1 year (hidden by default)."),
    _q("min_age_days", "Filter listings by age in days.", schema={"type": "integer", "minimum": 0}),
    _q("max_age_days", "Filter listings by age in days.", schema={"type": "integer", "minimum": 0}),
]

PAGE_PARAMS = [
    _q("sort", "age (default), company, title, location, ats, or match.",
       schema={"type": "string", "default": "age",
               "enum": ["age", "company", "title", "location", "ats", "match"]}),
    _q("dir", "asc (default; for age, asc means newest first) or desc.",
       schema={"type": "string", "enum": ["asc", "desc"], "default": "asc"}),
    _q("limit", "Number of results per page (1 to 500, default 100).",
       schema={"type": "integer", "minimum": 1, "maximum": 500, "default": 100}),
    _q("offset", "Pagination offset (default 0).",
       schema={"type": "integer", "minimum": 0, "default": 0}),
]

SENIORITIES = ["intern", "junior", "mid", "senior", "staff",
               "principal", "lead", "manager", "director", "exec"]

JOB = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "8-64 hex characters.", "example": "0a1b2c3d4e5f6a7b"},
        "title": {"type": "string", "example": "Backend Engineer"},
        "company_domain": {"type": "string", "example": "example.com"},
        "company_name": {"type": ["string", "null"], "description": "Null until the company has been named."},
        "logo_url": {"type": ["string", "null"]},
        "ats": {"type": ["string", "null"], "example": "greenhouse"},
        "location": {"type": ["string", "null"], "description": "The employer's own wording."},
        "department": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "description": "The normalized category, where one was inferred."},
        "seniority": {"type": ["string", "null"], "enum": [None] + SENIORITIES},
        "workplace_type": {"type": ["string", "null"], "enum": [None, "remote", "hybrid", "onsite"]},
        "url": {"type": "string", "description": "The original posting, on the employer's own ATS."},
        "posted_at": {"type": ["string", "null"], "format": "date-time"},
        "first_seen": {"type": "string", "format": "date-time",
                       "description": "When this project first saw the listing."},
        "last_seen": {"type": "string", "format": "date-time"},
        "closed_at": {"type": ["string", "null"], "format": "date-time",
                      "description": "Set once the listing left the employer's own board."},
        "confidence": {"type": "string", "enum": ["verified", "best_effort"]},
        "skills": {"type": "string", "description": "Comma-separated, tagged from the description."},
        "salary_text": {"type": ["string", "null"]},
        "salary_is_estimate": {"type": "integer", "enum": [0, 1]},
        "salary_source": {"type": ["string", "null"]},
        "match_score": {"type": "integer", "description": "Only meaningful with skills= and sort=match."},
    },
}

PROFILE = {
    "type": "object",
    "properties": {
        "skills": {"type": "array", "items": {"type": "string"}, "maxItems": 40},
        "seniority": {"type": ["string", "null"], "enum": [None] + SENIORITIES},
        "workplace": {"type": "array",
                      "items": {"type": "string", "enum": ["remote", "hybrid", "onsite"]}},
        "israel_only": {"type": "boolean"},
        "country": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]{2}$"}, "maxItems": 20,
                    "description": "ISO 3166-1 alpha-2 codes. Pre-fills new alerts and the matches view."},
        "city": {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 20,
                 "description": "City names as the locations facet spells them."},
        "cadence": {"type": "string", "enum": ["instant", "daily", "weekly"],
                    "description": "How often alert digests go out. Daily and weekly send one email at "
                                   "digest_time in digest_tz, when there is something new."},
        "digest_time": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$", "default": "09:00"},
        "digest_tz": {"type": "string", "default": "Asia/Jerusalem",
                      "description": "An IANA zone name the server knows; anything else becomes the default."},
        "digest_day": {"type": "integer", "minimum": 0, "maximum": 6, "default": 0,
                       "description": "Weekday for the weekly digest, Monday is 0."},
    },
}

ALERT = {
    "type": "object",
    "properties": {
        "alert_id": {"type": "string", "format": "uuid"},
        "filter": {"type": "object", "description": "Any subset of the /api/jobs filter keys."},
        "active": {"type": "boolean"},
        "created_at": {"type": "string", "format": "date-time"},
        "last_notified_at": {"type": ["string", "null"], "format": "date-time"},
    },
}

ERROR = {"type": "object", "properties": {"error": {"type": "string"}}, "required": ["error"]}

FACET = {"type": "array", "items": {"type": "object", "properties": {
    "value": {"type": "string"}, "n": {"type": "integer"}}}}

_STATUS_TEXT = {
    400: "Bad request. Invalid parameter or body payload.",
    401: "Unauthorized. Missing or invalid authentication token.",
    404: "Not found. Invalid endpoint path or missing resource ID.",
    405: "Method not allowed.",
    429: "Rate limit exceeded.",
    500: "Server error. Returns a short error string.",
}


def _json(schema, description="Success."):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _errors(*codes):
    return {str(c): _json(ERROR, _STATUS_TEXT[c]) for c in codes}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


DESCRIPTION = """This API provides public access to all job, company, and market data on the platform.

**Public Routes:** Open to everyone. No API keys or authentication required.

**Account Routes (/api/me):** Require a user authentication token. These endpoints only access or modify your own account data.

**Data Format:** All responses are JSON.

### Quick start

Fetch the 10 newest open job listings in the United States:

```
curl "%(site)s/api/jobs?country=US&limit=10"
```

### Rate limits and caching

**Rate Limit:** 20 requests per second across all users (bursts up to 40). Exceeding this limit returns HTTP 429.

**Data Export:** To download the full dataset, page through `/api/jobs?limit=500` sequentially instead of running parallel requests.

**Caching:** Most GET routes are cached for 60 seconds in the browser and 180 seconds at the edge. Repeated requests within this window return cached data.

**Real-time Routes:** `/api/pipeline-status` and `/api/geo` are never cached.

**CORS:** GET and OPTIONS requests are allowed from any origin.

All error responses return a JSON object containing an `error` key.
""" % {"site": SITE}


def _paths():
    jobs_list = {
        "type": "object",
        "properties": {
            "jobs": {"type": "array", "items": _ref("Job")},
            "total": {"type": ["integer", "null"], "description": "Null when the count was deferred."},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
            "matched_skills": {"type": "array", "items": {"type": "string"}},
            "search": {"type": ["string", "null"]},
        },
    }
    job_detail = {"allOf": [_ref("Job"), {"type": "object", "properties": {
        "description": {"type": ["string", "null"]}}}]}

    alert_post_example = (
        "```\n"
        "curl -X POST " + SITE + "/api/me/alerts \\\n"
        "  -H \"Authorization: Bearer <id_token>\" \\\n"
        "  -H \"content-type: application/json\" \\\n"
        "  -d '{\"filter\": {\"search\": \"rust\", \"country\": \"US\"}}'\n"
        "```"
    )

    return {
        "/jobs": {"get": {
            "tags": ["Jobs"],
            "summary": "Search, filter, and page through job listings",
            "description": (
                "### Skill Matching (sort=match)\n\n"
                "Ranks jobs by the number of matching skills, adjusted for posting age (every 14 "
                "days reduces the effective match count by 1).\n\n"
                "```\ncurl \"" + SITE + "/api/jobs?skills=Python,Kubernetes&sort=match&country=US\"\n```"
            ),
            "parameters": SEARCH_PARAMS + FILTER_PARAMS + PAGE_PARAMS,
            "responses": {"200": _json(jobs_list), **_errors(400, 429, 500)},
        }},
        "/jobs/{id}": {"get": {
            "tags": ["Jobs"],
            "summary": "Fetch a single job listing with its full description",
            "description": "These URLs remain accessible after a job closes (closed_at field populated).",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}, "example": "0a1b2c3d4e5f6a7b"}],
            "responses": {"200": _json(job_detail), **_errors(404, 500)},
        }},
        "/companies": {"get": {
            "tags": ["Companies"],
            "summary": "Lists all tracked companies and their scraping status",
            "parameters": [
                _q("resolved_only", "Only return companies with identified ATS endpoints. Set to 1."),
                _q("ats", "Filter by comma-separated ATS names."),
            ],
            "responses": {"200": _json({"type": "object", "properties": {
                "companies": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"}}}), **_errors(500)},
        }},
        "/companies/search": {"get": {
            "tags": ["Companies"],
            "summary": "Search companies by domain or name",
            "description": (
                "Minimum 2 characters required. Accepts any /api/jobs filter to count open jobs "
                "per company under specific conditions.\n\n"
                "```\ncurl \"" + SITE + "/api/companies/search?name=acme&country=US\"\n```"
            ),
            "parameters": [{"name": "name", "in": "query", "required": True,
                            "description": "Search string (required).",
                            "schema": {"type": "string", "minLength": 2}, "example": "acme"}] + FILTER_PARAMS,
            "responses": {"200": _json({"type": "object", "properties": {
                "companies": {"type": "array", "items": {"type": "object"}}}}), **_errors(400, 500)},
        }},
        "/stats": {"get": {
            "tags": ["Market data"],
            "summary": "Aggregate statistics, 14-day trends, top hiring companies, and market breakdowns",
            "description": "Accepts all /api/jobs filters.\n\n```\ncurl \"" + SITE + "/api/stats?country=US\"\n```",
            "parameters": SEARCH_PARAMS + FILTER_PARAMS,
            "responses": {"200": _json({"type": "object", "properties": {
                "meta": {"type": "object"},
                "top_companies": {"type": "array", "items": {"type": "object"}},
                "top_departments": {"type": "array", "items": {"type": "object"}},
                "top_locations": {"type": "array", "items": {"type": "object"}},
                "top_skills": {"type": "array", "items": {"type": "object"}},
                "daily_new_jobs": {"type": "array", "items": {"type": "object"}},
                "seniority_breakdown": {"type": "array", "items": {"type": "object"}},
            }}), **_errors(500)},
        }},
        "/facets": {"get": {
            "tags": ["Market data"],
            "summary": "Filter values alongside current job counts",
            "description": "Returns filter values (categories, locations, companies) alongside "
                           "current job counts. Accepts /api/jobs filters to narrow down options "
                           "dynamically.",
            "parameters": SEARCH_PARAMS + FILTER_PARAMS,
            "responses": {"200": _json({"type": "object", "properties": {
                "categories": FACET, "locations": FACET, "companies": FACET}}), **_errors(500)},
        }},
        "/me/profile": {
            "get": {
                "tags": ["Account"],
                "summary": "Retrieve your saved preferences",
                "description": "Retrieves your saved preferences (skills, seniority, workplace, "
                               "israel_only) and valid system options.",
                "security": [{"bearerAuth": []}],
                "responses": {"200": _json({"type": "object", "properties": {
                    "profile": _ref("Profile"),
                    "options": {"type": "object"},
                    "skill_spec": {"type": "object",
                                   "description": "The terms the in-browser CV reader matches on."},
                }}), **_errors(401, 500)},
            },
            "put": {
                "tags": ["Account"],
                "summary": "Overwrite profile preferences",
                "description": "Overwrites profile preferences. Accepts up to 40 skills. "
                               "Unrecognized skills are omitted.",
                "security": [{"bearerAuth": []}],
                "requestBody": {"required": True,
                                "content": {"application/json": {"schema": _ref("Profile")}}},
                "responses": {"200": _json({"type": "object", "properties": {"profile": _ref("Profile")}}),
                              **_errors(400, 401, 500)},
            },
        },
        "/me/alerts": {
            "get": {
                "tags": ["Account"],
                "summary": "Lists active email alerts",
                "security": [{"bearerAuth": []}],
                "responses": {"200": _json({"type": "object", "properties": {
                    "alerts": {"type": "array", "items": _ref("Alert")}}}), **_errors(401, 500)},
            },
            "post": {
                "tags": ["Account"],
                "summary": "Creates a new alert",
                "description": "Accepts the same filter keys as /api/jobs. Alerts only apply to "
                               "jobs posted after creation.\n\n" + alert_post_example,
                "security": [{"bearerAuth": []}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["filter"],
                    "properties": {"filter": {"type": "object",
                                              "description": "Any subset of the /api/jobs filter keys."}},
                    "example": {"filter": {"search": "rust", "country": "US"}}}}}},
                "responses": {"201": _json(_ref("Alert"), "Resource created (e.g., alert added)."),
                              **_errors(400, 401, 500)},
            },
        },
        "/me/alerts/{id}": {
            "patch": {
                "tags": ["Account"],
                "summary": "Pause, resume or re-filter an alert",
                "description": "Pause/resume an alert ({\"active\": true|false}) or update its "
                               "filter payload ({\"filter\": {...}}).",
                "security": [{"bearerAuth": []}],
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"active": {"type": "boolean"}, "filter": {"type": "object"}},
                    "example": {"active": False}}}}},
                "responses": {"200": _json(_ref("Alert")), **_errors(400, 401, 404, 500)},
            },
            "delete": {
                "tags": ["Account"],
                "summary": "Removes an alert",
                "security": [{"bearerAuth": []}],
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}],
                "responses": {"204": {"description": "Success with no content returned (e.g., deleted item)."},
                              **_errors(401, 404, 500)},
            },
        },
        "/me/saved": {"get": {
            "tags": ["Account"],
            "summary": "Every job you have starred",
            "description": "Returns all starred job IDs and timestamp saved (saved_at), sorted "
                           "newest first. Pass these IDs to GET /api/jobs?ids= to fetch full listings.",
            "security": [{"bearerAuth": []}],
            "responses": {"200": _json({"type": "object", "properties": {
                "saved": {"type": "array", "items": {"type": "object", "properties": {
                    "job_id": {"type": "string"},
                    "saved_at": {"type": "string", "format": "date-time"}}}}}}),
                **_errors(401, 500)},
        }},
        "/me/saved/{job_id}": {
            "put": {
                "tags": ["Account"],
                "summary": "Star a job listing",
                "security": [{"bearerAuth": []}],
                "parameters": [{"name": "job_id", "in": "path", "required": True,
                                "schema": {"type": "string"}, "example": "0a1b2c3d4e5f6a7b"}],
                "responses": {"204": {"description": "Success with no content returned (e.g., saved item)."},
                              **_errors(400, 401, 500)},
            },
            "delete": {
                "tags": ["Account"],
                "summary": "Unstar a job listing",
                "security": [{"bearerAuth": []}],
                "parameters": [{"name": "job_id", "in": "path", "required": True,
                                "schema": {"type": "string"}, "example": "0a1b2c3d4e5f6a7b"}],
                "responses": {"204": {"description": "Success with no content returned."},
                              **_errors(400, 401, 500)},
            },
        },
        "/health": {"get": {
            "tags": ["System"],
            "summary": "Database connectivity, job counts, scrape metrics and data quality warnings",
            "responses": {"200": _json({"type": "object", "properties": {
                "ok": {"type": "boolean"},
                "db_reachable": {"type": "boolean"},
                "jobs_total": {"type": "integer"},
                "jobs_open": {"type": "integer"},
                "companies_resolved": {"type": "integer"},
                "last_checked": {"type": "string", "format": "date-time"},
                "minutes_since_check": {"type": "number"},
                "snapshot": {"type": "object"},
                "timestamp_clustering_warning_count": {"type": "integer"},
            }}), **_errors(500)},
        }},
        "/pipeline-status": {"get": {
            "tags": ["System"],
            "summary": "Real-time scraping and merging phase activity",
            "description": "Never cached.",
            "responses": {"200": _json({"type": "object", "properties": {
                "scrape": {"type": "object"}, "merge": {"type": "object"}}}), **_errors(500)},
        }},
        "/geo": {"get": {
            "tags": ["System"],
            "summary": "The estimated caller country, from CDN headers",
            "description": "Returns null if undetectable. Never cached.",
            "responses": {"200": _json({"type": "object", "properties": {
                "country": {"type": ["string", "null"], "example": "US"},
                "source": {"type": ["string", "null"], "example": "cf-ipcountry"}}})},
        }},
    }


def spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "OpenTechJobs API",
            "version": "1.0",
            "summary": "Every job, company and market figure on the board, as JSON.",
            "description": DESCRIPTION,
            "license": {"name": "Source on GitHub",
                        "url": "https://github.com/guyvolvo/opentechjobs"},
        },
        "servers": [{"url": f"{SITE}/api", "description": "Production (HTTPS only)"}],
        "tags": [
            {"name": "Jobs", "description": "Search, filter, and page through job listings."},
            {"name": "Companies", "description": "Every company this project tracks, and its scraping status."},
            {"name": "Market data", "description": "Aggregates over the board, narrowed by the same filters."},
            {"name": "Account", "description": "Your own saved jobs, alerts and skill profile. Bearer token required."},
            {"name": "System", "description": "Whether the data behind all of the above is current."},
        ],
        "components": {
            "securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                "description": "Your Cognito ID token, as `Authorization: Bearer <id_token>`. If a "
                               "token is missing, expired, or invalid, the request returns 401 "
                               "Unauthorized.",
            }},
            "schemas": {"Job": JOB, "Profile": PROFILE, "Alert": ALERT, "Error": ERROR},
        },
        "paths": _paths(),
    }
