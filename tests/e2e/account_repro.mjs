// The account page against one live user's real DynamoDB rows, in each
// of the states its boot can be in. Only /api/me/* is faked, because
// there is no way to mint a real Cognito token from here; the stats
// download, the jobs lookup behind Saved and the matches query all go
// to the live API.
//
//   node account_repro.mjs [scenario]
//
//   ok               everything answers (the default)
//   slow-profile     /me/profile never comes back
//   slow-alerts      /me/alerts never comes back
//   stalled-refresh  the token has expired and Cognito does not answer
//   dead-refresh     the token has expired and Cognito refuses it
//
// Reported live on 2026-09-24 as "alerts still not loading" and "my
// profile isn't acting right". Both were stalled-refresh: the id_token
// expires after an hour in an open tab, fetch has no timeout, and
// bootAccount awaited the profile ahead of everything else, so the two
// lists sat on "Loading..." with no watchdog and nothing in the console.
import { chromium } from "playwright";

const SCENARIO = process.argv[2] || "ok";
const LIVE = new Set(["ok", "slow-profile", "slow-alerts"]);
const SUB = "fa0302ac-20c1-70c3-23da-d6734f299216";
const EMAIL = "guyvoloshin@gmail.com";
const never = async () => { await new Promise(() => {}); };

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const now = Math.floor(Date.now() / 1000);
const ID_TOKEN = [
  b64({ alg: "RS256", typ: "JWT" }),
  b64({ sub: SUB, email: EMAIL, name: "Guy Voloshin", iat: now,
        exp: LIVE.has(SCENARIO) ? now + 3600 : now - 60 }),
  "not-a-real-signature",
].join(".");

// Copied from the table on 2026-09-24. The search terms are stand-ins;
// the shapes, the key names and the count are the stored ones.
const ALERTS = [
  { user_id: SUB, alert_id: "863f51cb-d35c-4753-bc0c-e18b9b0fa9ff", email: EMAIL, active: true,
    created_at: "2026-09-22T10:31:30.399977+00:00", last_notified_at: "2026-09-24T13:20:53.491524+00:00",
    filter: { search: "devops", country: "IL", department: "Software Engineering" } },
  { user_id: SUB, alert_id: "d3858bfa-1000-4be0-9bf7-5b7044763b16", email: EMAIL, active: true,
    created_at: "2026-09-22T10:31:47.317917+00:00", last_notified_at: "2026-09-24T13:20:53.555951+00:00",
    filter: { search: "platform", country: "IL" } },
  { user_id: SUB, alert_id: "fd29ca9e-5dc6-44f9-ae43-a27adbf8714a", email: EMAIL, active: true,
    created_at: "2026-09-22T10:31:36.448507+00:00", last_notified_at: "2026-09-24T13:20:53.587768+00:00",
    filter: { search: "sre", country: "IL" } },
];
const SAVED = [
  { job_id: "74bf9886b169b25b", saved_at: "2026-09-24T12:29:04.862372+00:00" },
  { job_id: "b9ad3b9ba94f2fae", saved_at: "2026-09-24T12:29:04.876248+00:00" },
];
const SKILLS = ["Azure", "Git", "Python", "AWS", "CI/CD", "Terraform", "Linux", "Docker",
  "Kubernetes", "Bash", "Ansible", "Jenkins", "Grafana", "Prometheus", "PostgreSQL"];
// seniority and workplace are stored unset for this user, as they are
// for every user: the account page has no control for either.
const PROFILE = { skills: SKILLS, seniority: null, workplace: [], israel_only: true };

const browser = await chromium.launch();
// The second argument is the width. The account page is a different
// layout below 800px and the fault reported on 2026-09-24 only shows
// there, so the phone is worth running as its own case.
const WIDTH = Number(process.argv[3]) || 1440;
const page = await browser.newPage({ viewport: { width: WIDTH, height: WIDTH < 800 ? 844 : 1000 } });
const errs = [];
page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
page.on("console", (m) => { if (m.type() === "error") errs.push(`console: ${m.text()}`); });

await page.addInitScript((tok) => {
  localStorage.setItem("iljobs_auth_tokens", JSON.stringify({
    id_token: tok, access_token: tok, refresh_token: "refresh-token",
  }));
  localStorage.setItem("iljobs_geo_asked", "1");
}, ID_TOKEN);

const json = (r, body) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

// A URL predicate rather than a regex: every script the page loads comes
// from the checkout, auth.js included, which app.js now calls into.
const LOCAL_JS = ["app", "account", "auth", "signin_dialog", "cv_skills", "mobile_nav"];
await page.route((u) => LOCAL_JS.some((n) => u.pathname === `/${n}.js`), (r) =>
  r.fulfill({ path: `../../frontend${new URL(r.request().url()).pathname}`, contentType: "application/javascript" }));
await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
await page.route(/\/account(\?|$)/, (r) => r.fulfill({ path: "../../frontend/account.html", contentType: "text/html" }));

await page.route((u) => u.pathname === "/api/me/profile", SCENARIO === "slow-profile" ? never : (r) => json(r, {
  profile: PROFILE,
  options: { skills: SKILLS, seniority: ["intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director", "exec"], workplace: ["remote", "hybrid", "onsite"] },
  skill_spec: { labels: SKILLS, terms: SKILLS.map((s) => [s, [s.toLowerCase()]]) },
}));
await page.route((u) => u.pathname === "/api/me/alerts", SCENARIO === "slow-alerts" ? never : (r) => json(r, { alerts: ALERTS }));
await page.route((u) => u.pathname === "/api/me/saved", (r) => json(r, { saved: SAVED }));

if (SCENARIO === "stalled-refresh") await page.route(/cognito-idp/, never);
if (SCENARIO === "dead-refresh") {
  await page.route(/cognito-idp/, (r) => r.fulfill({
    status: 400, contentType: "application/x-amz-json-1.1",
    body: JSON.stringify({ __type: "NotAuthorizedException", message: "Refresh Token has expired" }),
  }));
}

// The fourth argument is the hash. /account#alerts is a real entry
// point: it is where every "Manage alerts" link in a digest mail lands.
const HASH = process.argv[4] || "";
await page.goto(`https://opentechjobs.org/account${HASH}`, { waitUntil: "domcontentloaded" });

const read = async (label) => {
  const out = await page.evaluate(() => {
    const t = (id) => { const e = document.getElementById(id); return e ? e.textContent.replace(/\s+/g, " ").trim().slice(0, 90) : "(no element)"; };
    return {
      alerts: t("alerts-list"),
      saved: t("saved-list"),
      tags: t("acct-tags"),
      // A hidden element still reports its text, so the signed-out state
      // has to be read from the dialog and the body class.
      signedOut: !document.getElementById("account-signedout").hidden
        && document.body.classList.contains("signin-open"),
      // Structural, not timing-dependent: the header used to inject a
      // second element for each of these, earlier in the document, and
      // whichever of the two renderers got there first decided which
      // copy the page painted.
      dupes: ["alerts-list", "alert-create", "alert-f-search", "create-alert-btn",
              "alert-form-title", "create-alert-feedback", "alert-ms-department",
              "auth-signout"]
        .map((id) => [id, document.querySelectorAll(`[id="${id}"]`).length])
        .filter(([, n]) => n > 1),
      pickers: document.querySelectorAll("#alert-create .ms .ms-toggle, #alert-create .ms select, #alert-create .ms button").length,
      done: [...document.querySelectorAll(".acct-check-row")].filter((r) => r.classList.contains("done")).length,
      tiles: [...document.querySelectorAll(".acct-tile")].map((x) => x.textContent.replace(/\s+/g, " ").trim()),
    };
  });
  console.log(label, JSON.stringify(out));
};

// 4s covers the ordinary load, 16s is past the stuck watchdog, 24s is
// past authedFetch's own timeout.
await page.waitForTimeout(4000);
await read(`${SCENARIO} t+4s `);
await page.waitForTimeout(12000);
await read(`${SCENARIO} t+16s`);
await page.waitForTimeout(8000);
await read(`${SCENARIO} t+24s`);

await page.screenshot({ path: `account-${SCENARIO}-${WIDTH}.png`, fullPage: true });
console.log(errs.length ? "console errors:\n  " + errs.join("\n  ") : "no console errors");
await page.unrouteAll({ behavior: "ignoreErrors" });
await browser.close();
