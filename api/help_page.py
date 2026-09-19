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

The copy here is the site owner's (2026-09-16) and is not ours to edit.
Only the markup is ours: headings, tables, code blocks and the existing
.api-* classes. Anyone tempted to "fix" the wording, including the Title
Case headings that this project's own writing rules would otherwise
change to sentence case, should leave it alone and raise it with the
owner instead.

The examples are deliberately global and anonymous, on the owner's
instruction: country=US rather than IL, placeholder domains rather than
real customers, a placeholder job id rather than a live one. This is a
public page for a worldwide audience and the Israeli market is not the
example anyone reaching it needs. israel_only stays documented because
it is a real parameter the API accepts; removing it would make the
reference wrong rather than global.

Three structural readings were needed, because the source arrived as
flattened markdown. The bare "Bash" and "JSON" tokens ahead of each
command are code-fence language labels rather than body text, so they
became code blocks instead of literal words. "CodeMeaning200Success."
is a two-column table. The bold-label bullets became paragraphs with
bold labels, which matches the original markdown and keeps a long label
out of .api-params' nowrap first column, where it would reintroduce the
horizontal overflow that .workspace > .section's min-width:0 just fixed.
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
    </div>
  </div>

  <div class="workspace">
    <section class="section">
      <div class="container api-help">
        <h2 class="section-title">API Reference</h2>
        <p class="api-help-intro">This API provides public access to all job, company, and market data on the platform.</p>

        <p><b>Public Routes:</b> Open to everyone. No API keys or authentication required.</p>
        <p><b>Account Routes (/api/me):</b> Require a user authentication token. These endpoints only access or modify your own account data.</p>
        <p><b>Data Format:</b> All responses are JSON (except this documentation page).</p>
        <p><b>Base URL:</b> <code>https://opentechjobs.org/api</code> (HTTPS only).</p>

        <h3>Quick Start</h3>
        <p>Fetch the 10 newest open job listings in the United States:</p>
        <code>curl "https://opentechjobs.org/api/jobs?country=US&amp;limit=10"</code>

        <h3>Standard Response Format</h3>
        <p>Every response from /api/jobs returns the job records, total matching count, pagination limits, and matched skills:</p>
        <code>{
  "jobs": [ { "id": "0a1b2c3d4e5f6a7b", "title": "Backend Engineer" } ],
  "total": 12480,
  "limit": 10,
  "offset": 0,
  "matched_skills": []
}</code>

        <h3>Authentication</h3>
        <p>Public endpoints do not require headers.</p>
        <p>For personal account endpoints (/api/me/*), pass your Cognito ID token in the authorization header:</p>
        <code>curl -H "Authorization: Bearer &lt;id_token&gt;" https://opentechjobs.org/api/me/saved</code>
        <p>If a token is missing, expired, or invalid, the API Gateway returns a 401 Unauthorized response with no body.</p>

        <h3>Rate Limits &amp; Caching</h3>
        <p><b>Rate Limit:</b> 20 requests per second across all users (bursts up to 40). Exceeding this limit returns HTTP 429.</p>
        <p><b>Data Export:</b> To download the full dataset, page through /api/jobs?limit=500 sequentially instead of running parallel requests.</p>
        <p><b>Caching:</b> Most GET routes are cached for 60 seconds in the browser and 180 seconds at the edge. Repeated requests within this window return cached data.</p>
        <p><b>Real-time Routes:</b> /api/pipeline-status and /api/geo are never cached.</p>
        <p><b>CORS:</b> GET and OPTIONS requests are allowed from any origin.</p>

        <h3>Status Codes</h3>
        <table class="api-params">
          <tbody>
            <tr><td><b>Code</b></td><td><b>Meaning</b></td></tr>
            <tr><td><span class="param">200</span></td><td>Success.</td></tr>
            <tr><td><span class="param">201</span></td><td>Resource created (e.g., alert added).</td></tr>
            <tr><td><span class="param">204</span></td><td>Success with no content returned (e.g., deleted item, saved item).</td></tr>
            <tr><td><span class="param">400</span></td><td>Bad request. Invalid parameter or body payload.</td></tr>
            <tr><td><span class="param">401</span></td><td>Unauthorized. Missing or invalid authentication token.</td></tr>
            <tr><td><span class="param">404</span></td><td>Not found. Invalid endpoint path or missing resource ID.</td></tr>
            <tr><td><span class="param">405</span></td><td>Method not allowed.</td></tr>
            <tr><td><span class="param">429</span></td><td>Rate limit exceeded.</td></tr>
            <tr><td><span class="param">500</span></td><td>Server error. Returns a short error string.</td></tr>
          </tbody>
        </table>
        <p>All error responses return a JSON object containing an error key.</p>

        <h3>Endpoints</h3>

        <h3>1. Jobs</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs</span></div>
          <p>Search, filter, and page through job listings.</p>

          <p><b>Search Parameters:</b></p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">search</span></td><td>Space-separated search terms (e.g., kubernetes new york). Matches title, company, location, category, and description. Use quotes for exact phrases (e.g., "software engineer").</td></tr>
              <tr><td><span class="param">q</span></td><td>Legacy title/company search.</td></tr>
              <tr><td><span class="param">keywords</span></td><td>Semicolon-separated terms (e.g., azure;excel;iso). Matches title and description.</td></tr>
            </tbody>
          </table>

          <p><b>Filter Parameters (Accepts comma-separated values):</b></p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">country</span></td><td>Two-letter country code (e.g., US or US,GB). Uses normalized location data.</td></tr>
              <tr><td><span class="param">city</span></td><td>City names (e.g., New York,London).</td></tr>
              <tr><td><span class="param">location</span></td><td>Raw location text provided by the employer.</td></tr>
              <tr><td><span class="param">company</span></td><td>Company domain names (e.g., example.com,example.org).</td></tr>
              <tr><td><span class="param">ats</span></td><td>Applicant tracking system names (e.g., greenhouse,lever).</td></tr>
              <tr><td><span class="param">department</span></td><td>Normalized job categories (e.g., Security, Infrastructure). Fetch valid values via /api/facets.</td></tr>
              <tr><td><span class="param">seniority</span></td><td>intern, junior, mid, senior, staff, principal, lead, manager, director, exec.</td></tr>
              <tr><td><span class="param">workplace</span></td><td>remote, hybrid, onsite.</td></tr>
              <tr><td><span class="param">skills</span></td><td>Comma-separated skill names. Filters jobs matching at least one skill and sets sort=match.</td></tr>
              <tr><td><span class="param">ids</span></td><td>Fetch specific job IDs in a single request.</td></tr>
              <tr><td><span class="param">israel_only</span></td><td>Set to 1 to restrict to Israeli locations (Equivalent to country=IL).</td></tr>
            </tbody>
          </table>

          <p><b>Visibility &amp; Inclusion Controls:</b></p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">confidence</span></td><td>verified (default, confirmed on employer site), best_effort, or all.</td></tr>
              <tr><td><span class="param">include_closed</span></td><td>Set to 1 to include roles closed within the last 30 days.</td></tr>
              <tr><td><span class="param">include_outdated</span></td><td>Set to 1 to include open postings older than 1 year (hidden by default).</td></tr>
              <tr><td><span class="param">min_age_days</span> / <span class="param">max_age_days</span></td><td>Filter listings by age in days.</td></tr>
            </tbody>
          </table>

          <p><b>Sorting &amp; Pagination:</b></p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">sort</span></td><td>age (default), company, title, location, ats, or match.</td></tr>
              <tr><td><span class="param">dir</span></td><td>asc (default; for age, asc means newest first) or desc.</td></tr>
              <tr><td><span class="param">limit</span></td><td>Number of results per page (1 to 500, default 100).</td></tr>
              <tr><td><span class="param">offset</span></td><td>Pagination offset (default 0).</td></tr>
            </tbody>
          </table>

          <p><b>Skill Matching (sort=match):</b></p>
          <p>Ranks jobs by the number of matching skills, adjusted for posting age (every 14 days reduces the effective match count by 1).</p>
          <code>curl "https://opentechjobs.org/api/jobs?skills=Python,Kubernetes&amp;sort=match&amp;country=US"</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/jobs/{id}</span></div>
          <p>Fetch a single job listing with its full description. These URLs remain accessible after a job closes (closed_at field populated).</p>
          <code>curl https://opentechjobs.org/api/jobs/0a1b2c3d4e5f6a7b</code>
        </div>

        <h3>2. Companies</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/companies</span></div>
          <p>Lists all tracked companies and their scraping status.</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">resolved_only=1</span></td><td>Only return companies with identified ATS endpoints.</td></tr>
              <tr><td><span class="param">ats</span></td><td>Filter by comma-separated ATS names.</td></tr>
            </tbody>
          </table>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/companies/search</span></div>
          <p>Search companies by domain or name (minimum 2 characters required).</p>
          <table class="api-params">
            <tbody>
              <tr><td><span class="param">name</span></td><td>Search string (required).</td></tr>
            </tbody>
          </table>
          <p>Accepts any /api/jobs filter to count open jobs per company under specific conditions.</p>
          <code>curl "https://opentechjobs.org/api/companies/search?name=acme&amp;country=US"</code>
        </div>

        <h3>3. Market Data</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/stats</span></div>
          <p>Returns aggregate statistics, 14-day trends, top hiring companies, and market breakdowns. Accepts all /api/jobs filters.</p>
          <code>curl "https://opentechjobs.org/api/stats?country=US"</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/facets</span></div>
          <p>Returns filter values (categories, locations, companies) alongside current job counts. Accepts /api/jobs filters to narrow down options dynamically.</p>
        </div>

        <h3>4. User Account (/api/me)</h3>
        <p>Requires a valid Authorization Bearer token.</p>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET / PUT</span><span class="api-path">/api/me/profile</span></div>
          <p><b>GET:</b> Retrieves your saved preferences (skills, seniority, workplace, israel_only) and valid system options.</p>
          <p><b>PUT:</b> Overwrites profile preferences. Accepts up to 40 skills. Unrecognized skills are omitted.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET / POST</span><span class="api-path">/api/me/alerts</span></div>
          <p><b>GET:</b> Lists active email alerts.</p>
          <p><b>POST:</b> Creates a new alert. Accepts the same filter keys as /api/jobs. Alerts only apply to jobs posted after creation.</p>
          <code>curl -X POST https://opentechjobs.org/api/me/alerts \\
  -H "Authorization: Bearer &lt;id_token&gt;" \\
  -H "content-type: application/json" \\
  -d '{"filter": {"search": "rust", "country": "US"}}'</code>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">PATCH / DELETE</span><span class="api-path">/api/me/alerts/{id}</span></div>
          <p><b>PATCH:</b> Pause/resume an alert ({"active": true|false}) or update its filter payload ({"filter": {...}}).</p>
          <p><b>DELETE:</b> Removes an alert.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/me/saved</span></div>
          <p>Returns all starred job IDs and timestamp saved (saved_at), sorted newest first. Pass these IDs to GET /api/jobs?ids= to fetch full listings.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">PUT / DELETE</span><span class="api-path">/api/me/saved/{job_id}</span></div>
          <p>Star (PUT) or unstar (DELETE) a job listing. Returns 204 No Content on success.</p>
        </div>

        <h3>5. System Status</h3>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/health</span></div>
          <p>Checks database connectivity, open job counts, scrape metrics, and data quality warnings.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/pipeline-status</span></div>
          <p>Returns real-time scraping and merging phase activity. Never cached.</p>
        </div>

        <div class="api-endpoint">
          <div class="api-endpoint-head"><span class="api-method">GET</span><span class="api-path">/api/geo</span></div>
          <p>Returns the estimated user country based on CDN headers ({"country": "US", "source": "cf-ipcountry"}). Returns null if undetectable.</p>
        </div>

        <p class="legal-back"><a class="link" href="/board">&larr; Back to the job board</a></p>
      </div>
    </section>
  </div>

</body>
</html>
"""
