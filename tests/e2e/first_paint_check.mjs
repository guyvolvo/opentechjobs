// The first paint of a first-time visit, with this checkout's app.js and
// board.html served against the live API.
//
//   node first_paint_check.mjs
//
// What changed and what this guards: the geo prompt used to hold the
// first listing fetch until a human answered it, so the table behind the
// dialog was empty for as long as they took to read it. Now the board
// loads behind the prompt, and only accepting a country re-queries.
//
// Expected: rows are on screen while the prompt is still open; the jobs
// request does not queue behind /stats.json; accepting asks for
// country=IL and replaces the rows; skipping asks for nothing more.
import { chromium } from "playwright";

const b = await chromium.launch();
const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`);
  if (!ok) failures.push(name);
};

async function open() {
  const ctx = await b.newContext({ viewport: { width: 1400, height: 900 }, serviceWorkers: "block" });
  const p = await ctx.newPage();
  const jobCalls = [];
  p.on("pageerror", (e) => failures.push("pageerror: " + e.message.slice(0, 100)));
  p.on("request", (r) => {
    const u = r.url();
    if (/\/api\/jobs\b/.test(u) && /limit=50/.test(u)) jobCalls.push(u.split("?")[1]);
  });
  await p.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await p.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await p.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
  return { ctx, p, jobCalls };
}

// Rows behind an open prompt.
{
  const { ctx, p, jobCalls } = await open();
  await p.goto("https://opentechjobs.org/board", { waitUntil: "commit" });
  await p.locator(".geo-prompt").waitFor({ state: "visible", timeout: 30_000 });
  await p.waitForSelector("#jobs-body tr[data-id]", { timeout: 30_000 });
  const promptStillOpen = await p.locator(".geo-prompt").isVisible();
  const rows = await p.locator("#jobs-body tr[data-id]").count();
  check("rows render while the prompt is still open", promptStillOpen && rows > 0, `open=${promptStillOpen} rows=${rows}`);

  const t = await p.evaluate(() => {
    const res = performance.getEntriesByType("resource");
    const stats = res.find((e) => /stats\.json/.test(e.name));
    const api = res.filter((e) => /\/api\/jobs\b/.test(e.name) && /limit=50/.test(e.name))[0];
    return { stats_start: stats ? Math.round(stats.startTime) : null, api_start: api ? Math.round(api.startTime) : null };
  });
  // Both are started together now, so they begin within a frame or two
  // of each other. Comparing against when stats FINISHED would pass by
  // luck whenever stats happens to be fast, which it usually is.
  check("the jobs request starts alongside stats, not after it",
    t.api_start === null || t.stats_start === null || t.api_start - t.stats_start < 250,
    `api_start=${t.api_start} stats_start=${t.stats_start}`);

  // Accepting re-queries, with the country on it. The wait is generous
  // because country=IL is the one first view with no precomputed page
  // behind it, so a cold API instance can take ten seconds or more. That
  // is the gap the Israel bootstrap is meant to close.
  const wasShowing = await p.locator("#jobs-body tr[data-id]").first().getAttribute("data-id");
  // Armed before the click: the answer can come back faster than a
  // listener attached afterwards can catch it, which is how this test
  // first failed against a request it had already made.
  const answered = p.waitForResponse(
    (r) => /\/api\/jobs/.test(r.url()) && /country=IL/.test(r.url()) && /limit=50/.test(r.url()),
    { timeout: 40_000 });
  await p.locator(".geo-prompt button", { hasText: "jobs" }).first().click();
  await answered;
  await p.waitForTimeout(700);
  check("accepting asks for the country", jobCalls.some((q) => /country=IL/.test(q)), JSON.stringify(jobCalls));
  const nowShowing = await p.locator("#jobs-body tr[data-id]").first().getAttribute("data-id");
  check("and the rows are replaced, not merged",
    (await p.locator("#jobs-body tr[data-id]").count()) > 0 && nowShowing !== wasShowing,
    `was=${wasShowing} now=${nowShowing}`);
  const url = new URL(p.url());
  check("and the address bar agrees", url.searchParams.get("country") === "IL", p.url());
  await ctx.close();
}

// Skip changes nothing.
{
  const { ctx, p, jobCalls } = await open();
  await p.goto("https://opentechjobs.org/board", { waitUntil: "commit" });
  await p.locator(".geo-prompt").waitFor({ state: "visible", timeout: 30_000 });
  await p.waitForSelector("#jobs-body tr[data-id]", { timeout: 30_000 });
  await p.locator(".geo-prompt button", { hasText: "Skip" }).first().click();
  await p.waitForTimeout(3000);
  // One request for the whole visit: the confirming fetch behind the
  // bootstrap render, which fires after the rows are already up. Skip
  // must not add a second one.
  check("skipping does not re-query", jobCalls.length === 1 && !/country=/.test(jobCalls[0] || ""),
    JSON.stringify(jobCalls));
  // Not row identity: the confirming fetch behind the bootstrap render
  // can legitimately bring newer listings. What must hold is that Skip
  // left a full page of the global board up, not an empty table.
  check("and leaves a full page of listings up",
    (await p.locator("#jobs-body tr[data-id]").count()) >= 50,
    String(await p.locator("#jobs-body tr[data-id]").count()));
  check("and no country filter is set", !new URL(p.url()).searchParams.get("country"), p.url());
  await ctx.close();
}

await b.close();
console.log();
if (failures.length) {
  console.log(`${failures.length} failed:`);
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("all passed");
