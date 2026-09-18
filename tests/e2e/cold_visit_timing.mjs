// How long a first-time visitor waits before jobs are on screen.
//
//   node cold_visit_timing.mjs [visits] [concurrency] [arm] [url]
//   arm: prompt (default) | link | skip
//
// Every visit gets its own browser context, so nothing is cached: no HTTP
// cache, no localStorage, no warmed connection. That is the point. A reload
// measures the cache, not the visit.
//
// Concurrency is deliberately low. Fifty real Chromium contexts at once on
// one laptop measures the laptop. Waves of a few keep each timing about the
// site. This is a latency measurement, not a load test.
//
// A first-time visitor whose country CloudFront can name gets the geo
// prompt, and maybeAskCountry() holds the first jobs fetch until they
// answer. So there are three arms:
//   prompt  the prompt appears and a bot clicks "Show ... jobs" the
//           instant it does, which is the floor a human cannot beat
//   skip    same, but clicks Skip (global board, no country filter)
//   link    arrives on /board?country=IL, so state.country is already set
//           and the prompt never shows. This is the shared-link path.
//
// Every time is on the page's own clock, measured from navigation start.
// Timing the visit from the driver instead adds the ~300ms Playwright
// spends building a browser context, which is this laptop, not the site.
//   html_ttfb   request start to first byte of the document
//   fcp         first contentful paint
//   dom_ready   DOMContentLoaded
//   prompt_at   the geo prompt is in the DOM (prompt/skip arms)
//   api_ms      how long the board's /api/jobs call took
//   first_row   a job row is in the DOM
//   after_answer  first_row minus the moment the prompt was answered
import { chromium } from "playwright";

const VISITS = Number(process.argv[2] || 50);
const CONC = Number(process.argv[3] || 5);
const ARM = process.argv[4] || "prompt";
const BASE = process.argv[5] || "https://opentechjobs.org/board";
const URL = ARM === "link" ? `${BASE}${BASE.includes("?") ? "&" : "?"}country=IL` : BASE;

const browser = await chromium.launch();

async function visit(n) {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  const row = { visit: n, arm: ARM, errors: [] };
  page.on("pageerror", (e) => row.errors.push(e.message.slice(0, 80)));

  // The page timestamps its own milestones, so the driver's overhead
  // stays out of the numbers.
  await page.addInitScript(() => {
    window.__t = {};
    const watch = new MutationObserver(() => {
      if (window.__t.prompt_at === undefined && document.querySelector(".geo-prompt"))
        window.__t.prompt_at = Math.round(performance.now());
      if (document.querySelector("#jobs-body tr[data-id]")) {
        window.__t.first_row = Math.round(performance.now());
        watch.disconnect();
      }
    });
    document.addEventListener("DOMContentLoaded", () => watch.observe(document.body, { subtree: true, childList: true }));
  });

  try {
    await page.goto(URL, { waitUntil: "commit", timeout: 60_000 });
    if (ARM !== "link") {
      const btn = page.locator(".geo-prompt button", { hasText: ARM === "skip" ? "Skip" : "jobs" }).first();
      await btn.waitFor({ state: "visible", timeout: 60_000 });
      // Armed before the click, because on a warm instance the answer
      // beats a listener attached after it.
      const swap = ARM === "prompt"
        ? page.waitForResponse((r) => /\/api\/jobs/.test(r.url()) && /country=/.test(r.url()) && /limit=50/.test(r.url()),
            { timeout: 60_000 })
        : null;
      await btn.click();
      row.answered_at = await page.evaluate(() => Math.round(performance.now()));
      // The country view is the one first screen with nothing
      // precomputed behind it, so this is where a cold API instance
      // still pays for a whole snapshot download.
      if (swap) await swap;
      // waitForResponse returns on the headers; the timing entry is
      // only filed at responseEnd, so reading it immediately finds
      // nothing.
      if (swap) await page.waitForTimeout(400);
    }
    await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 60_000 });

    Object.assign(row, await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] || {};
      const res = performance.getEntriesByType("resource");
      const first = (re) => res.filter((e) => re.test(e.name)).sort((x, y) => x.startTime - y.startTime)[0];
      // The board's own call, not the 10-row ticker beside it.
      const api = first(/\/api\/jobs\?.*limit=50/);
      // The same call once a country has been chosen, which is the one
      // first view with nothing precomputed behind it.
      const country = first(/\/api\/jobs\?.*country=.*limit=50/);
      const stats = first(/stats\.json|\/api\/stats/);
      const fcp = performance.getEntriesByName("first-contentful-paint")[0];
      return {
        html_ttfb: Math.round(nav.responseStart || 0),
        html_done: Math.round(nav.responseEnd || 0),
        dom_ready: Math.round(nav.domContentLoadedEventEnd || 0),
        fcp: fcp ? Math.round(fcp.startTime) : null,
        stats_ms: stats ? Math.round(stats.duration) : null,
        api_start: api ? Math.round(api.startTime) : null,
        api_ms: api ? Math.round(api.duration) : null,
        country_api_ms: country ? Math.round(country.duration) : null,
        api_kb: api ? Math.round((api.transferSize || api.encodedBodySize || 0) / 1024) : null,
        requests: res.length,
        rows: document.querySelectorAll("#jobs-body tr[data-id]").length,
        ...window.__t,
      };
    }));
    row.after_answer = row.first_row - (row.answered_at || 0);
  } catch (e) {
    row.failed = String(e).split("\n")[0].slice(0, 120);
  }
  await ctx.close();
  return row;
}

const rows = [];
let next = 0;
async function worker() {
  while (next < VISITS) {
    const n = next++;
    const r = await visit(n);
    rows.push(r);
    process.stderr.write(
      r.failed
        ? `  visit ${String(n).padStart(2)} FAILED ${r.failed}\n`
        : `  visit ${String(n).padStart(2)}  html ${String(r.html_ttfb).padStart(4)}  ` +
          `fcp ${String(r.fcp).padStart(4)}  prompt ${String(r.prompt_at ?? "-").padStart(5)}  ` +
          `api ${String(r.api_ms).padStart(4)}  rows ${String(r.rows).padStart(3)}  ` +
          `first row ${String(r.first_row).padStart(5)}ms\n`
    );
  }
}

console.error(`${VISITS} cold visits to ${URL} (arm ${ARM}), ${CONC} at a time`);
const started = Date.now();
await Promise.all(Array.from({ length: CONC }, worker));
await browser.close();

const ok = rows.filter((r) => !r.failed);
const pct = (vals, p) => {
  const s = [...vals].sort((a, b) => a - b);
  return s.length ? s[Math.min(s.length - 1, Math.floor((s.length * p) / 100))] : null;
};
const stat = (key) => {
  const v = ok.map((r) => r[key]).filter((x) => typeof x === "number");
  return { n: v.length, min: pct(v, 0), p50: pct(v, 50), p90: pct(v, 90), p99: pct(v, 99), max: pct(v, 100) };
};

const summary = {
  url: URL,
  arm: ARM,
  visits: VISITS,
  concurrency: CONC,
  succeeded: ok.length,
  failed: rows.length - ok.length,
  wall_seconds: Math.round((Date.now() - started) / 1000),
  rows_median: pct(ok.map((r) => r.rows), 50),
  api_kb_median: pct(ok.map((r) => r.api_kb).filter((x) => typeof x === "number"), 50),
  ms: Object.fromEntries(
    ["html_ttfb", "fcp", "html_done", "dom_ready", "prompt_at", "stats_ms", "api_start", "api_ms", "country_api_ms", "first_row", "after_answer"]
      .map((k) => [k, stat(k)]).filter(([, v]) => v.n)
  ),
};
console.log(JSON.stringify({ summary, visits: rows }, null, 1));
console.error(JSON.stringify(summary, null, 1));
