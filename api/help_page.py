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
        <p class="api-help-intro">Every route below is public and needs no key or account, same data the job board itself runs on -- except /me/alerts, which is Cognito-JWT-gated and scoped to the signed-in caller.</p>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs</span></div>
          <p>Search and filter job listings. Every listing carries <span class="param">salary_source</span>, which says what stands behind <span class="param">salary_text</span> and is null when there is no figure at all: <span class="param">disclosed</span> means the employer published that range on the listing itself, <span class="param">table</span> means our Israeli market estimate by role and seniority, and <span class="param">estimated</span> means our estimate from what comparable roles actually pay at that company and location. Only <span class="param">disclosed</span> is the employer's own number. The older <span class="param">salary_is_estimate</span> boolean is still returned and still means "not disclosed".</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">search</span></td><td>Space-separated words, ALL must appear, e.g. <span class="param">kubernetes tel aviv</span>. Matched against title, company, location, category and description. <span class="param">"Quote a phrase"</span> to keep it whole.</td></tr>
              <tr><td><span class="param">q</span></td><td>Free-text match against title, company, location, department. Superseded by <span class="param">search</span>; still answered for saved alerts.</td></tr>
              <tr><td><span class="param">keywords</span></td><td>Semicolon-separated terms, ALL must appear, e.g. <span class="param">azure;excel;iso</span>. Matched against title and description. Superseded by <span class="param">search</span>; still answered for saved alerts.</td></tr>
              <tr><td><span class="param">ats</span></td><td>Comma-separated ATS names, e.g. <span class="param">greenhouse,lever</span>.</td></tr>
              <tr><td><span class="param">company</span></td><td>Comma-separated company domains.</td></tr>
              <tr><td><span class="param">department</span></td><td>Comma-separated categories (Security, Infrastructure, Software Engineering, ...) -- a normalized grouping, not the raw ATS department string.</td></tr>
              <tr><td><span class="param">seniority</span></td><td>Comma-separated levels.</td></tr>
              <tr><td><span class="param">location</span></td><td>Comma-separated locations.</td></tr>
              <tr><td><span class="param">workplace</span></td><td>Comma-separated workplace types: <span class="param">remote</span>, <span class="param">hybrid</span>, <span class="param">onsite</span>.</td></tr>
              <tr><td><span class="param">confidence</span></td><td><span class="param">verified</span> (default), <span class="param">best_effort</span>, or <span class="param">all</span>.</td></tr>
              <tr><td><span class="param">israel_only</span></td><td><span class="param">1</span> to restrict to Israeli locations. Default is global, every location.</td></tr>
              <tr><td><span class="param">include_closed</span></td><td><span class="param">1</span> to include closed listings. Default excludes them. Reaches back 30 days: a listing closed longer ago than that has left the served snapshot, so this returns a month of history rather than all of it.</td></tr>
              <tr><td><span class="param">include_outdated</span></td><td><span class="param">1</span> to include listings past the freshness window. Default excludes them.</td></tr>
              <tr><td><span class="param">min_age_days</span> / <span class="param">max_age_days</span></td><td>Filter by days since posting.</td></tr>
              <tr><td><span class="param">sort</span></td><td><span class="param">age</span> (default), <span class="param">company</span>, <span class="param">title</span>, <span class="param">location</span>, <span class="param">ats</span>.</td></tr>
              <tr><td><span class="param">dir</span></td><td><span class="param">asc</span> (default, newest first for age) or <span class="param">desc</span>.</td></tr>
              <tr><td><span class="param">limit</span></td><td>1-500, default 100.</td></tr>
              <tr><td><span class="param">offset</span></td><td>Default 0.</td></tr>
            </tbody>
          </table>
          <code>curl /api/jobs?q=backend&amp;israel_only=1</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs/{id}</span></div>
          <p>A single listing by its stable id, including full description. Resolves even after the job closes (<span class="param">closed_at</span> gets set instead of the record disappearing).</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/companies</span></div>
          <p>The tracked company list and their scrape status.</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">resolved_only</span></td><td><span class="param">1</span> to only include companies with a known ATS.</td></tr>
              <tr><td><span class="param">ats</span></td><td>Comma-separated ATS names.</td></tr>
            </tbody>
          </table>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/stats</span></div>
          <p>Market aggregates powering the panels on the job board.</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">israel_only</span></td><td><span class="param">1</span> to scope <span class="param">top_locations</span> to Israeli locations.</td></tr>
            </tbody>
          </table>
          <code>curl /api/stats</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/health</span></div>
          <p>Liveness check for both the API and the scrape pipeline. A 200 with a real DB query behind it, not just "the Lambda started": <span class="param">db_reachable</span>, <span class="param">jobs_total</span>, <span class="param">jobs_open</span>, <span class="param">companies_resolved</span>, <span class="param">last_checked</span>, <span class="param">minutes_since_check</span>, <span class="param">timestamp_clustering_warnings</span> (non-empty only if many different companies' jobs share one suspiciously-identical posted date -- a data-quality canary, not a routine field).</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/pipeline-status</span></div>
          <p>What the scrape pipeline is doing right now, not just when it last finished: <span class="param">scrape</span> (the fast-poll/discover side, every 5-10 minutes) and <span class="param">merge</span> (the hourly partition merge) each carry their own <span class="param">phase</span>, <span class="param">detail</span>, <span class="param">at</span>.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/facets</span></div>
          <p>Per-option counts for the board's own Category/Location/Company filter dropdowns, scoped to whatever else is currently selected.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET/POST</span><span class="api-path">/api/me/alerts</span></div>
          <p>List or create saved job alerts for the signed-in caller. Requires a Cognito JWT (<span class="param">Authorization: Bearer &lt;id_token&gt;</span>). A created alert's <span class="param">filter</span> is the same query-param shape as <span class="param">/api/jobs</span> above.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">PATCH/DELETE</span><span class="api-path">/api/me/alerts/{id}</span></div>
          <p>Pause/resume (<span class="param">PATCH</span> with <span class="param">{"active": true|false}</span>) or delete an existing alert. Same auth as above, and only ever the caller's own.</p>
        </div>

        <p class="legal-back"><a class="link" href="/">&larr; Back to the job board</a></p>
      </div>
    </section>
  </div>

</body>
</html>
"""
