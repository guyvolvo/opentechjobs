// Routes the API to the box, as the fast path.
//
// Two routes, one per zone:
//
//     oceanofjobs.com/api/*
//     opentechjobs.org/api/*
//
// The second outlives the move to oceanofjobs.com on purpose. The rest
// of opentechjobs.org answers with a 301, and API clients that do not
// follow redirects (or would turn a POST into a GET doing so) keep
// working on the address they were written against.
//
// Nothing else. /job/* and /company/* used to be Worker routes too, and
// on the night of 2026-09-24 one crawler walking the job sitemap spent
// the free plan's 100,000 daily Worker requests by 05:00. Those pages
// are what crawlers want and they go through CloudFront now, whose
// free tier is ten million a month (see infra/cloudfront.tf).
//
// CloudFront could carry /api/* as well, and does whenever this Worker
// is over budget or has no route: Cloudflare fails the route open and
// the request lands on CloudFront's own /api/* behavior, which reaches
// the same box through the same tunnel. The Worker exists for the
// difference in the waiting: measured 2026-09-25 from Israel, a
// listing's description arrived in a steady 204 to 236 ms this way and
// in a median 331 ms with spikes to 840 ms the CloudFront way, because
// that way crosses two edges and a fresh connection between them.
// Roughly 30,000 API requests a day pass through here, a third of the
// budget; the rate limiting rule in the dashboard keeps any one
// unverified client at 60 per ten seconds.
//
// Route patterns cannot express "except", so /api/auth/* is excluded
// in code below. Those two paths (the GitHub callback and the email
// OTP start) belong to a separate Lambda behind API Gateway and must
// keep going to CloudFront, or sign-in breaks.
//
// Rollback is deleting the route: CloudFront takes over on the spot.

const BOX = "https://box.oceanofjobs.com";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Sign-in stays where it is.
    if (url.pathname.startsWith("/api/auth/")) {
      return fetch(request);
    }

    // Same path, same query, different host. Copying the original
    // request keeps the method, the body and the headers, which matters
    // for the Authorization bearer token on /api/me/* and for the
    // preflight the browser sends before it.
    const target = new URL(url.pathname + url.search, BOX);
    const headers = new Headers(request.headers);
    // The viewer's country, under a name Cloudflare will not overwrite
    // on the hop to the box. CF-IPCountry gets re-stamped there with
    // this Worker's own location; the box's /api/geo reads this header
    // first. Set unconditionally so a viewer cannot supply their own.
    headers.set("x-viewer-country", (request.cf && request.cf.country) || "");
    const response = await fetch(new Request(target, { method: request.method, headers, body: request.body, redirect: "manual" }));

    // So a human (or a curl) can tell which origin answered without
    // guessing from the response body.
    const out = new Response(response.body, response);
    out.headers.set("x-otj-origin", "box");
    return out;
  },
};
