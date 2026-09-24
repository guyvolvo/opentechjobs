// Search moved out of the filter bar and into the topbar above 800px,
// and the recent-jobs marquee that used to hold that space is gone.
// Below 800px the same input goes back into the filter bar, where the
// phone layout already had it, so this checks both homes and the
// handoff between them.
//
//   node search_topbar_check.mjs
import { chromium } from "playwright";

const WIDTHS = [1800, 1440, 1100, 900, 801, 800, 760, 640, 390];
const browser = await chromium.launch();
const errs = [];
const rows = [];

const serve = async (page) => {
  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
};

const measure = () =>
  document.evaluate ? (() => {
    const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
    const hit = (a, b) => !!a && !!b && a.w > 0 && b.w > 0 && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
    const wrap = document.getElementById("topbar-search");
    const input = document.getElementById("f-search");
    const sorts = document.querySelector(".topbar-sorts");
    const nav = document.querySelector(".topbar-board .topnav");
    const row1 = document.querySelector(".board-bar-row-1");
    const go = document.getElementById("f-search-go");
    const filters = document.getElementById("rail-toggle");
    const b = { wrap: box(wrap), sorts: box(sorts), nav: box(nav), row1: box(row1), go: box(go), filters: box(filters) };
    return {
      home: wrap?.closest(".topbar") ? "topbar" : wrap?.closest(".board-bar") ? "board-bar" : "nowhere",
      search: b.wrap,
      sortsShown: !!(b.sorts && b.sorts.w),
      row1Shown: !!(b.row1 && b.row1.h),
      goShown: !!(b.go && b.go.w),
      filtersShown: !!(b.filters && b.filters.w),
      hintShown: !!document.querySelector(".topbar-search-key")?.getBoundingClientRect().width,
      // Same row as the Filters button in the bar, the thing that broke
      // last time a control moved between containers.
      sameRow: b.filters && b.wrap ? b.filters.y === b.wrap.y : null,
      overlapSorts: hit(b.wrap, b.sorts),
      overlapNav: hit(b.wrap, b.nav) || hit(b.sorts, b.nav),
      ticker: !!document.querySelector(".ticker, #ticker-track"),
      navRight: b.nav ? Math.round(innerWidth - (b.nav.x + b.nav.w)) : null,
      inputFont: getComputedStyle(input).fontSize,
    };
  })() : null;

for (const w of WIDTHS) {
  const page = await browser.newPage({ viewport: { width: w, height: 900 } });
  page.on("pageerror", (e) => errs.push(`${w}: ${e.message}`));
  await page.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
  await serve(page);
  await page.goto("https://opentechjobs.org/board", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(900);
  rows.push([w, await page.evaluate(measure)]);
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.close();
}

for (const [w, r] of rows) console.log(String(w).padStart(5), JSON.stringify(r));

// Behaviour, at the width the change is for.
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", (e) => errs.push(`behaviour: ${e.message}`));
await page.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
await serve(page);
await page.goto("https://opentechjobs.org/board", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 30000 });
const before = await page.textContent("#result-count").catch(() => "");

// / focuses the box from anywhere on the page, and does not type itself.
await page.click("#jobs-body tr[data-id]");
await page.keyboard.press("/");
const hot = await page.evaluate(() => ({
  focused: document.activeElement?.id,
  value: document.getElementById("f-search").value,
  hint: !!document.querySelector(".topbar-search-key")?.getBoundingClientRect().width,
}));

// Enter searches without waiting out the 500ms typing timer.
await page.keyboard.type("kubernetes");
const t0 = Date.now();
await page.keyboard.press("Enter");
await page.waitForFunction(() => new URL(location.href).searchParams.get("search") === "kubernetes" || true, null, { timeout: 5000 });
await page.waitForTimeout(1500);
const typed = await page.evaluate(() => ({
  count: document.getElementById("result-count")?.textContent.trim(),
  clear: !!document.getElementById("f-search-clear") && !document.getElementById("f-search-clear").hidden,
  hint: !!document.querySelector(".topbar-search-key")?.getBoundingClientRect().width,
  chip: [...document.querySelectorAll(".active-chips .chip")].map((c) => c.textContent.trim()),
}));
console.log("enter ms", Date.now() - t0);

await page.click("#f-search-clear");
await page.waitForTimeout(1500);
const cleared = await page.evaluate(() => ({
  value: document.getElementById("f-search").value,
  count: document.getElementById("result-count")?.textContent.trim(),
  clear: !document.getElementById("f-search-clear").hidden,
  focused: document.activeElement?.id,
}));

console.log("before ", before?.trim());
console.log("hotkey ", JSON.stringify(hot));
console.log("typed  ", JSON.stringify(typed));
console.log("cleared", JSON.stringify(cleared));

// The handoff: resize across 800 with the box focused.
await page.setViewportSize({ width: 700, height: 900 });
await page.waitForTimeout(400);
const down = await page.evaluate(measure);
await page.setViewportSize({ width: 1440, height: 900 });
await page.waitForTimeout(400);
const up = await page.evaluate(measure);
console.log("resized down", JSON.stringify({ home: down.home, sameRow: down.sameRow, h: down.search?.h }));
console.log("resized up  ", JSON.stringify({ home: up.home, overlapNav: up.overlapNav, w: up.search?.w }));

await page.screenshot({ path: "search-topbar-1440.png" });
await page.unrouteAll({ behavior: "ignoreErrors" });
await page.close();

await browser.close();
console.log(errs.length ? `page errors:\n  ${errs.join("\n  ")}` : "no page errors");
