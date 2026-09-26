"""GET /api/help -- the API reference, rendered by Swagger UI.

Was a hand-written HTML page listing every route in prose and tables.
Replaced 2026-09-24 on request: the same content is now an OpenAPI 3.1
document (openapi.py, served at /api/openapi.json) and this page is the
standard Swagger UI shell over it. One description instead of two, and
the reference gained a Try it out button against the live API, which a
static table could never have.

The copy inside the spec is still the site owner's and still not ours to
edit; see openapi.py's own docstring.

Why a CDN script tag rather than vendoring Swagger UI: this project has
no build step anywhere, the frontend is plain files, and swagger-ui-dist
is 1.5MB of JavaScript that would otherwise have to live in the repo and
be updated by hand. Pinned to a major version with Subresource Integrity
off deliberately -- jsdelivr's @5 tag moves within the major, and a
hash would pin it to one release and break the page the day that tag
advances. The page degrades to a link to the raw spec if the script does
not load.

Served from this Lambda rather than the S3 bucket for the same reason
the page it replaces was: /api/* is this origin's, and the reference
belongs beside the thing it describes.
"""

HELP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>API Reference — OceanOfJobs.com</title>
<meta name="description" content="Public JSON API for every job, company and market figure on Ocean of Jobs: search, filter, statistics and account routes." />
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
<style>
  /* Swagger UI's own look, kept. The three things below are the page
     around it, not the reference inside it. */
  body { margin: 0; background: #fafafa; }
  /* Swagger UI draws its own header bar for a URL input nobody here
     needs: the spec is one document at a fixed path. */
  .swagger-ui .topbar { display: none; }
  /* A way back to the board, which the hand-written page had at the
     bottom and which a generated one has nowhere. */
  .api-home {
    display: block;
    padding: 14px 20px;
    border-bottom: 1px solid #e3e3e3;
    background: #fff;
    font: 400 14px system-ui, -apple-system, "Segoe UI", sans-serif;
    color: #3b4151;
    text-decoration: none;
  }
  .api-home:hover { color: #4990e2; }
  /* Shown only if the CDN script never arrives. */
  .api-fallback { padding: 28px 20px; font: 400 15px/1.6 system-ui, sans-serif; color: #3b4151; }
  .api-fallback a { color: #4990e2; }
</style>
</head>
<body>
<a class="api-home" href="/board">← Back to the job board</a>
<div id="swagger-ui"><noscript><div class="api-fallback">
  This reference is rendered from an OpenAPI document. With JavaScript off,
  read it directly: <a href="/api/openapi.json">/api/openapi.json</a>.
</div></noscript></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js" crossorigin></script>
<script>
  window.addEventListener("load", function () {
    var host = document.getElementById("swagger-ui");
    if (!window.SwaggerUIBundle) {
      // The CDN is the one thing on this page that can fail, so it fails
      // into the document itself rather than into an empty page.
      host.innerHTML = '<div class="api-fallback">The reference viewer did not load. '
        + 'The API description is still here: <a href="/api/openapi.json">/api/openapi.json</a>.</div>';
      return;
    }
    SwaggerUIBundle({
      url: "/api/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      // Collapsed. Eighteen operations expanded is a page nobody can
      // scan; the tag headings are the table of contents.
      docExpansion: "list",
      defaultModelsExpandDepth: 0,
      tryItOutEnabled: true,
      persistAuthorization: true,
      // Alphabetical within a tag, and the tag order the spec declares.
      operationsSorter: "alpha",
      syntaxHighlight: { activated: true, theme: "agate" },
      presets: [SwaggerUIBundle.presets.apis],
      layout: "BaseLayout"
    });
  });
</script>
</body>
</html>
"""
