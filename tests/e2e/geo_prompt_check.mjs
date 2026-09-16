// The country prompt: shown once before the board's first load, never
// again, and never re-triggered by clearing filters.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
const PORT = 8847;
const OUT = process.argv[2];
const srv = spawn("python", ["-m", "http.server", String(PORT)], { cwd: "frontend", stdio: "ignore" });
await new Promise(r => setTimeout(r, 1200));
let bad = 0;
const check = (n, ok, d = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${n}${ok ? "" : "  -- " + d}`); if (!ok) bad++; };
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
// Stub /api/geo and /api/jobs so the test is about the prompt, not the network.
await ctx.route("**/api/geo", r => r.fulfill({ status: 200, contentType: "application/json",
  body: JSON.stringify({ country: "IL", source: "cf-ipcountry" }) }));
await ctx.route("**/api/jobs*", r => r.fulfill({ status: 200, contentType: "application/json",
  body: JSON.stringify({ jobs: [], total: 0, limit: 50, offset: 0, matched_skills: [] }) }));
// Stubbed because the prompt's copy depends on it. COUNTRY_LABELS_SEEN is
// filled from these rows, and an earlier version of this test let the
// request 404: every assertion passed while the prompt said "Are you in
// IL?" rather than naming the country. A test that cannot see that is
// worse than no test.
const FACET_LOCATIONS = [
  { value: "IL", label: "Israel", n: 3668, cities: [] },
  { value: "US", label: "United States", n: 104661, cities: [] },
];
await ctx.route("**/facets.json", r => r.fulfill({ status: 200, contentType: "application/json",
  body: JSON.stringify({ verified: { categories: [], locations: FACET_LOCATIONS, companies: [] },
                         all: { categories: [], locations: FACET_LOCATIONS, companies: [] } }) }));
// Complete enough that the boot path actually finishes. An earlier
// version omitted `freshness`, and renderMetrics opens by reading
// stats.freshness.last_checked, so it threw, refreshStats swallowed it in
// its own catch, and everything after that line was skipped. None of it
// surfaced as a page error, so the rest of this file's assertions were
// passing against a half-loaded board.
await ctx.route("**/stats.json", r => r.fulfill({ status: 200, contentType: "application/json",
  body: JSON.stringify({
    meta: { last_loaded: "2026-09-16T16:00:00+00:00" },
    freshness: { last_checked: "2026-09-16T16:00:00+00:00", minutes_since_update: 1 },
    totals: { open_jobs: 0, companies_hiring: 0, companies_total: 0, companies_resolved: 0 },
    location: { israel: 0, other: 0, total: 0 },
    age: { avg_open_days: 0, median_open_days: 0, oldest_open_days: 0 },
    throughput: { new_jobs_24h: 0, new_jobs_7d: 0, closed_jobs_24h: 0, closed_jobs_7d: 0 },
    ghost: { threshold_days: 60, dormant_count: 0, dormant_pct: 0, sample_size: 0 },
    skills_coverage: { with_skills: 0, open_jobs: 0 },
    daily_new_jobs: [], open_jobs_history: [], top_locations: [], top_companies: [],
    top_departments: [], top_skills: [], top_movers_7d: [], open_jobs_by_ats: [],
    seniority_breakdown: [], category_seniority: [], workplace: [],
  }) }));
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", e => errors.push(String(e)));

await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);

const shown = await page.locator(".geo-prompt").count();
check("prompt appears on a first visit", shown === 1, `count=${shown}`);
if (shown) {
  const info = await page.evaluate(() => {
    const b = document.querySelector(".geo-prompt");
    const acc = document.getElementById("geo-accept"), skip = document.getElementById("geo-skip");
    const cs = getComputedStyle(skip);
    return { title: b.querySelector("h2").innerText, evidence: b.querySelector(".geo-evidence").innerText.replace(/\s+/g, " "),
             accept: acc.innerText, skipBg: cs.backgroundColor, skipWeight: cs.fontWeight,
             modal: b.getAttribute("aria-modal"), scrim: !!document.querySelector(".geo-scrim") };
  });
  console.log("  ", JSON.stringify(info));
  // The country's name, not its code. /Israel|IL/ would pass on either,
  // which is how "Are you in IL?" shipped past an earlier version of
  // this check. The code alone must fail.
  check("names the country, not the code", /Israel/.test(info.title) && !/\bIL\b/.test(info.title), info.title);
  check("the button names it too", /Israel/.test(info.accept) && !/\bIL\b/.test(info.accept), info.accept);
  check("shows what was read", /cf-ipcountry/.test(info.evidence), info.evidence);
  check("has a scrim and is a modal", info.scrim && info.modal === "true");
  check("skip is visually quieter than accept", info.skipWeight !== "700" || info.skipBg === "rgba(0, 0, 0, 0)", `${info.skipWeight} ${info.skipBg}`);
  await page.screenshot({ path: `${OUT}/geo-prompt.png` });
  await page.click("#geo-accept");
  await page.waitForTimeout(800);
}
const after = await page.evaluate(() => ({
  gone: document.querySelectorAll(".geo-prompt").length === 0,
  flag: localStorage.getItem("iljobs_geo_asked"),
  country: JSON.parse(localStorage.getItem("iljobs_filters") || "{}").country,
  url: location.search,
}));
console.log("  after accept:", JSON.stringify(after));
check("prompt closes on accept", after.gone);
check("flag is set", after.flag === "1", String(after.flag));
check("country filter applied", Array.isArray(after.country) && after.country.includes("IL"), JSON.stringify(after.country));

// Reload: must not ask again.
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForTimeout(2200);
check("does not ask again on reload", (await page.locator(".geo-prompt").count()) === 0);

// Clear the location filter and reload: still must not ask.
await page.evaluate(() => {
  const f = JSON.parse(localStorage.getItem("iljobs_filters") || "{}");
  f.country = []; f.city = [];
  localStorage.setItem("iljobs_filters", JSON.stringify(f));
});
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForTimeout(2200);
const afterClear = await page.locator(".geo-prompt").count();
check("clearing filters does not re-trigger it", afterClear === 0, `count=${afterClear}`);

check("no page errors", errors.length === 0, errors.join(" | "));
await browser.close(); srv.kill();
process.exit(bad ? 1 : 0);
