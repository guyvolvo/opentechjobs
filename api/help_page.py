"""GET /api/help -- a plain HTML page documenting every route this API
serves. Used to be inline in the frontend's own footer (index.html);
moved here 2026-09-08 on request, since that put the full parameter
reference on every single page load when almost nobody ever scrolled
that far -- the footer now just links here instead.

Reuses the frontend's own style.css: this page is served by the API
Lambda, not the S3 bucket the rest of the frontend lives in, but both
sit behind the same CloudFront distribution/domain (see
infra/cloudfront.tf), so an absolute /style.css resolves to the
frontend's default cache behavior regardless of which origin actually
served this HTML. Kept as a hand-written string, not a template engine
-- one static page, same reasoning as handler.py's own "no framework"
docstring.

Rewritten 2026-09-16 into sections a reader can navigate: what the API
is, how to call it, what comes back, then the routes grouped by the
thing they act on. The previous version was one flat list of nine
endpoints with a twenty-row parameter table on the first one, which
answered "what can /api/jobs take" only if you already knew to look
there, and never answered "how do I authenticate" or "what does a 400
mean" at all. Four routes the Lambda actually serves were missing from
it entirely: /geo, /companies/search, /me/profile and /me/saved, along
with the match sort and the country, city, skills and ids parameters.

Every claim here is checked against the code that implements it rather
than against the last version of this page. The rate limit is the one
in infra/apigateway.tf (20 req/s, burst 40), not a rounder number that
reads better.
"""

HELP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>API Reference -- OpenTechJobs.org</title>
  <meta name="description" content="OpenTechJobs.org's public API: endpoints, parameters, and example requests." />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png" />
  <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png" />
  <link rel="icon" href="/favicon.ico" sizes="any" />
  <link rel="apple-touch-icon" sizes="180x180" href="/favicon-180.png" />
  <link rel="stylesheet" href="/style.css" />
  <script>
    if (localStorage.getItem("iljobs_theme") === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    }
  </script>
</head>
<body>

  <div class="topbar">
    <div class="container">
      <a class="wordmark" href="/">
        <img src="/logo-horizontal.png" alt="OpenTechJobs.org" class="wordmark-logo" />
      </a>
    </div>
  </div>

  <div class="workspace">
    <section class="section">
      <div class="container api-help">
        <h2 class="section-title">API Reference</h2>
        <p class="api-help-intro">Every job on this board, readable over HTTP. The listings, company and market
        routes are public and need no key. The routes under /api/me act on one signed-in person's own data and
        need a token. Responses are JSON, except this page.</p>

        <h3>Start here</h3>
        <p>The ten newest open listings in Israel:</p>
        <code>curl "https://opentechjobs.org/api/jobs?country=IL&amp;limit=10"</code>
        <p>Every response from <span class="param">/api/jobs</span> carries the rows, the unpaged total, and the
        paging you asked for:</p>
        <code>{
  "jobs": [ { "id": "fef6f069a7697d5e", "title": "Backend Engineer", ... } ],
  "total": 3667,
  "limit": 10,
  "offset": 0,
  "matched_skills": []
}</code>

        <h3>Base URL</h3>
        <code>https://opentechjobs.org/api</code>
        <p>Every path below is relative to that. HTTPS only.</p>

        <h3>Authentication</h3>
        <p>Nothing to send for the public routes. Just call them.</p>
        <p>The <span class="param">/api/me/*</span> routes carry a Cognito ID token, the same one the site itself
        uses after you sign in:</p>
        <code>curl -H "Authorization: Bearer &lt;id_token&gt;" https://opentechjobs.org/api/me/saved</code>
        <p>An API Gateway authorizer checks the signature and expiry before the request reaches the application,
        so a missing, malformed or expired token comes back <span class="param">401</span> with no body from us.
        These routes only ever read and write the calling account's own rows, never anyone else's.</p>

        <h3>Rate limits</h3>
        <p>20 requests per second, bursting to 40. The limit counts every caller together rather than
        per IP address, so a heavy client can use up the allowance that others are sharing. Over it, requests
        get <span class="param">429</span>. There is no quota beyond that and no key to apply for.</p>
        <p>If you want the whole dataset, page through <span class="param">/api/jobs</span> with
        <span class="param">limit=500</span> rather than firing concurrent requests. It is faster in practice and
        it keeps you under the burst.</p>

        <h3>Caching and freshness</h3>
        <p>The scrapers refresh continuously and the served snapshot is rebuilt every few minutes. Most routes
        are cached for 180 seconds at the edge and 60 in your own browser, so a repeated call inside that window
        is free and may be up to three minutes behind the pipeline.
        <span class="param">/api/pipeline-status</span> and <span class="param">/api/geo</span> are never cached,
        because both exist to answer a question about right now.</p>
        <p>Cross-origin requests are allowed from anywhere, for <span class="param">GET</span> and
        <span class="param">OPTIONS</span>.</p>

        <h3>Status codes</h3>
        <table class="api-params">
          <tbody>
            <tr><td><span class="param">200</span></td><td>The request worked.</td></tr>
            <tr><td><span class="param">201</span></td><td>An alert was created.</td></tr>
            <tr><td><span class="param">204</span></td><td>The change went through and there is nothing to return. Saves, unsaves, alert deletions.</td></tr>
            <tr><td><span class="param">400</span></td><td>A parameter was wrong: an unknown sort key, a confidence value that is not one of the three, a filter key that does not exist. The body names the problem.</td></tr>
            <tr><td><span class="param">401</span></td><td>An /api/me route without a valid token.</td></tr>
            <tr><td><span class="param">404</span></td><td>No such path, or no job or alert with that id.</td></tr>
            <tr><td><span class="param">405</span></td><td>The path exists but not with that method.</td></tr>
            <tr><td><span class="param">429</span></td><td>Over the rate limit.</td></tr>
            <tr><td><span class="param">500</span></td><td>Our fault. The body carries a short detail string, never a stack trace.</td></tr>
          </tbody>
        </table>
        <p>Every error body is JSON with an <span class="param">error</span> key.</p>

        <h3>Jobs</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs</span></div>
          <p>Search and filter the board. Returns <span class="param">jobs</span>,
          <span class="param">total</span> (the count before paging), <span class="param">limit</span>,
          <span class="param">offset</span> and <span class="param">matched_skills</span>.</p>

          <p>Searching:</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">search</span></td><td>Space-separated words, all of which must appear, for example <span class="param">kubernetes tel aviv</span>. Matches title, company, location, category and description. Wrap a phrase in double quotes to keep it whole. This is the one to use.</td></tr>
              <tr><td><span class="param">q</span></td><td>Free text against title, company, location and department. Superseded by <span class="param">search</span>, still answered for alerts saved before it existed.</td></tr>
              <tr><td><span class="param">keywords</span></td><td>Semicolon-separated terms, all of which must appear, for example <span class="param">azure;excel;iso</span>. Matches title and description. Also superseded by <span class="param">search</span>.</td></tr>
            </tbody>
          </table>

          <p>Filtering. All of these take a comma-separated list and match any value in it:</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">country</span></td><td>Two-letter country codes, for example <span class="param">IL</span> or <span class="param">IL,US</span>. Checked against normalized location data rather than the raw text, so it is both faster and more accurate than matching on <span class="param">location</span>.</td></tr>
              <tr><td><span class="param">city</span></td><td>City names, for example <span class="param">Tel Aviv,Haifa</span>.</td></tr>
              <tr><td><span class="param">location</span></td><td>The raw location string as the employer wrote it. Prefer <span class="param">country</span> and <span class="param">city</span>.</td></tr>
              <tr><td><span class="param">company</span></td><td>Company domains, for example <span class="param">wix.com,monday.com</span>.</td></tr>
              <tr><td><span class="param">ats</span></td><td>Applicant tracking systems, for example <span class="param">greenhouse,comeet</span>.</td></tr>
              <tr><td><span class="param">department</span></td><td>Normalized categories such as Security, Infrastructure or Software Engineering. This is our grouping, not the employer's own department string. Call <span class="param">/api/facets</span> for the current list.</td></tr>
              <tr><td><span class="param">seniority</span></td><td>intern, junior, mid, senior, staff, principal, lead, manager, director, exec.</td></tr>
              <tr><td><span class="param">workplace</span></td><td>remote, hybrid, onsite.</td></tr>
              <tr><td><span class="param">skills</span></td><td>Skill labels. Keeps listings tagged with at least one of them, and sets up <span class="param">sort=match</span>.</td></tr>
              <tr><td><span class="param">ids</span></td><td>Specific job ids, for looking up a known set in one call.</td></tr>
              <tr><td><span class="param">israel_only</span></td><td><span class="param">1</span> restricts to Israeli locations. Equivalent to <span class="param">country=IL</span> and kept for older callers.</td></tr>
            </tbody>
          </table>

          <p>Choosing which listings count:</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">confidence</span></td><td><span class="param">verified</span> (default) for listings confirmed on the employer's own board, <span class="param">best_effort</span> for the rest, <span class="param">all</span> for both.</td></tr>
              <tr><td><span class="param">include_closed</span></td><td><span class="param">1</span> also returns roles that have closed. Reaches back 30 days; older closed listings have left the served snapshot, so this is a month of history rather than all of it.</td></tr>
              <tr><td><span class="param">include_outdated</span></td><td><span class="param">1</span> also returns open postings older than a year, which are hidden by default because most are abandoned rather than genuinely open.</td></tr>
              <tr><td><span class="param">min_age_days</span>, <span class="param">max_age_days</span></td><td>Days since the role was posted.</td></tr>
            </tbody>
          </table>

          <p>Ordering and paging:</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">sort</span></td><td><span class="param">age</span> (default), <span class="param">company</span>, <span class="param">title</span>, <span class="param">location</span>, <span class="param">ats</span>, or <span class="param">match</span>.</td></tr>
              <tr><td><span class="param">dir</span></td><td><span class="param">asc</span> (default; for age that means newest first) or <span class="param">desc</span>.</td></tr>
              <tr><td><span class="param">limit</span></td><td>1 to 500. Default 100.</td></tr>
              <tr><td><span class="param">offset</span></td><td>Default 0. Ordering is stable across pages, so a listing cannot appear on two of them.</td></tr>
            </tbody>
          </table>

          <p><span class="param">sort=match</span> ranks by how many of your <span class="param">skills</span> a
          listing has, with recency folded in: every 14 days since posting costs one matched skill, so a fresh
          four-of-seven outranks a six-week-old five-of-seven. Each row carries its own
          <span class="param">match_score</span>, the plain count before that adjustment. Asking for
          <span class="param">match</span> without any <span class="param">skills</span> sorts by age instead.</p>
          <code>curl "https://opentechjobs.org/api/jobs?skills=Python,Kubernetes&amp;sort=match&amp;country=IL"</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs/{id}</span></div>
          <p>One listing with its full description. This is the permalink: it keeps resolving after the role
          closes, answering with <span class="param">closed_at</span> set rather than disappearing, so a saved
          link never dead-ends. The employer's own URL often stops working at that point, which is why this
          exists.</p>
          <code>curl https://opentechjobs.org/api/jobs/fef6f069a7697d5e</code>
        </div>

        <h3>Companies</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/companies</span></div>
          <p>Every tracked company and the state of its last scrape: <span class="param">domain</span>,
          <span class="param">ats</span>, <span class="param">token</span>,
          <span class="param">confidence</span>, <span class="param">job_count</span>,
          <span class="param">tried</span>, <span class="param">error</span>,
          <span class="param">first_seen</span>, <span class="param">last_checked</span>.</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">resolved_only</span></td><td><span class="param">1</span> keeps only companies whose ATS we have identified.</td></tr>
              <tr><td><span class="param">ats</span></td><td>Comma-separated ATS names.</td></tr>
            </tbody>
          </table>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/companies/search</span></div>
          <p>Companies whose domain or name contains <span class="param">name</span>, each with its open-listing
          count. Searches the whole snapshot rather than a top-500 slice. Two characters minimum; shorter returns
          an empty list, since one character matches half the board.</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">name</span></td><td>What to look for. Required.</td></tr>
              <tr><td>any /api/jobs filter</td><td>Counts each company under those filters, so you can ask who is hiring in Israel specifically. The <span class="param">company</span> filter itself is ignored here.</td></tr>
            </tbody>
          </table>
          <code>curl "https://opentechjobs.org/api/companies/search?name=micr&amp;country=IL"</code>
        </div>

        <h3>Market data</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/stats</span></div>
          <p>The aggregates behind the board's own panels: totals, a 14-day history of new and open listings,
          top companies, departments, locations and skills, breakdowns by seniority, workplace and ATS, plus
          pipeline health and how stale the oldest tracked company is. Accepts the same filters as
          <span class="param">/api/jobs</span>, which scope the parts of the response that describe a result set
          rather than the market as a whole.</p>
          <code>curl "https://opentechjobs.org/api/stats?country=IL"</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/facets</span></div>
          <p>The values worth offering in a filter, each with its count: <span class="param">categories</span>,
          <span class="param">locations</span> (countries, each with its cities) and
          <span class="param">companies</span>. Pass any <span class="param">/api/jobs</span> filter and each
          facet is counted with the <em>other</em> filters applied, which is what stops a dropdown offering an
          option that would return nothing.</p>
        </div>

        <h3>Your account</h3>
        <p>All of these need a bearer token and act only on the caller's own data.</p>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET/PUT</span><span class="api-path">/api/me/profile</span></div>
          <p>The caller's saved search preferences: <span class="param">skills</span>,
          <span class="param">seniority</span>, <span class="param">workplace</span> and
          <span class="param">israel_only</span>. A <span class="param">GET</span> also ships the vocabularies
          the account page needs to render, so there is one list of skill names rather than a second copy in the
          browser that drifts from ours.</p>
          <p>Someone who has never filled one in gets an empty profile rather than a 404. A
          <span class="param">PUT</span> replaces the whole object, so clearing your skills is distinguishable
          from leaving them alone. Values we do not recognise are dropped rather than rejected: one misspelled
          skill should not lose the rest of the edit. Up to 40 skills.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET/POST</span><span class="api-path">/api/me/alerts</span></div>
          <p>List saved alerts, or create one. A <span class="param">POST</span> body is
          <span class="param">{"filter": {...}}</span>, where the filter holds the same keys
          <span class="param">/api/jobs</span> accepts. An unknown key is a
          <span class="param">400</span> rather than a filter that silently matches nothing forever.</p>
          <p>A new alert only ever notifies on postings that appear after it was created, not on everything
          already open that happens to match.</p>
          <code>curl -X POST https://opentechjobs.org/api/me/alerts \\
  -H "Authorization: Bearer &lt;id_token&gt;" \\
  -H "content-type: application/json" \\
  -d '{"filter": {"search": "rust", "country": "IL"}}'</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">PATCH/DELETE</span><span class="api-path">/api/me/alerts/{id}</span></div>
          <p>Pause or resume with <span class="param">{"active": true|false}</span>, change what it watches with
          <span class="param">{"filter": {...}}</span>, or delete it. A body with neither key is a
          <span class="param">400</span>.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/me/saved</span></div>
          <p>Every listing the caller has starred, newest first, as
          <span class="param">job_id</span> and <span class="param">saved_at</span>. Hand the ids to
          <span class="param">/api/jobs?ids=</span> to get the listings themselves in one call.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">PUT/DELETE</span><span class="api-path">/api/me/saved/{job_id}</span></div>
          <p>Star or unstar one listing. Both answer <span class="param">204</span>. A job id is hexadecimal;
          anything else is a <span class="param">400</span>, so a junk path cannot write a saved row pointing at
          nothing.</p>
        </div>

        <h3>Service status</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/health</span></div>
          <p>A real query against the database, not just confirmation that the process started:
          <span class="param">db_reachable</span>, <span class="param">jobs_total</span>,
          <span class="param">jobs_open</span>, <span class="param">companies_resolved</span>,
          <span class="param">last_checked</span>, <span class="param">minutes_since_check</span>. It also
          carries <span class="param">timestamp_clustering_warnings</span>, which is empty unless many different
          companies' listings share one suspiciously identical posted date. That is a data-quality canary rather
          than a routine field.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/pipeline-status</span></div>
          <p>What the scrapers are doing right now rather than when they last finished.
          <span class="param">scrape</span> and <span class="param">merge</span> each report a
          <span class="param">phase</span>, a <span class="param">detail</span> and an
          <span class="param">at</span>. Never cached.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/geo</span></div>
          <p>The country the request appears to come from, as
          <span class="param">{"country": "IL", "source": "cf-ipcountry"}</span>. Read from a CDN header that
          reflects the network edge handling the request, not a device location, and the board uses it only to
          offer a local default that the reader can decline.</p>
          <p>When the header is missing or says nothing useful, both fields are
          <span class="param">null</span>. A wrong guess about where someone is would be worse than no guess, so
          this never falls back to one.</p>
        </div>

        <p class="legal-back"><a class="link" href="/">&larr; Back to the job board</a></p>
      </div>
    </section>
  </div>

</body>
</html>
"""
