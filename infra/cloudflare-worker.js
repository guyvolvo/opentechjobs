// Routes the API and the server-rendered pages to the box.
//
// This exists because Origin Rules cannot do it on this plan: the Host
// Header, SNI and DNS Record overrides are all Enterprise-only, and
// Destination Port alone is useless here. A Worker is the remaining way
// to send some paths to a different origin while the rest of the zone
// keeps going to CloudFront.
//
// Bind it to three routes on the opentechjobs.org zone:
//
//     opentechjobs.org/api/*
//     opentechjobs.org/job/*
//     opentechjobs.org/company/*
//
// Route patterns cannot express "except", so /api/auth/* is excluded in
// code below. Those two paths (the GitHub callback and the email OTP
// start) belong to a separate Lambda behind API Gateway and must keep
// going to CloudFront, or sign-in breaks.
//
// Rollback is deleting the routes. The Lambda stack is still deployed
// and deploy-api.yml is still shipping to it, so what it falls back to
// is current code rather than whatever was there on cutover day.

const BOX = "https://box.opentechjobs.org";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Sign-in stays where it is.
    if (url.pathname.startsWith("/api/auth/")) {
      return fetch(request);
    }

    // Same path, same query, different host. Passing the original
    // request as the second argument keeps the method, the body and
    // the headers, which matters for the Authorization bearer token on
    // /api/me/* and for the preflight the browser sends before it.
    const target = new URL(url.pathname + url.search, BOX);
    const response = await fetch(new Request(target, request));

    // So a human (or a curl) can tell which origin answered without
    // guessing from the response body.
    const out = new Response(response.body, response);
    out.headers.set("x-otj-origin", "box");
    return out;
  },
};
