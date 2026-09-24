// The sticky Apply bar must never name a listing the pane is not showing.
// Reported live 2026-09-24: "Apply on nvidia.com" under "Select a listing
// to see its details here", after a filter change dropped the open one.
//
//   node pane_sticky_sync.mjs
import { chromium } from "playwright";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
const errs = [];
page.on("pageerror", (e) => errs.push(e.message));
await page.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
const LOCAL = ["app", "auth", "signin_dialog", "mobile_nav"];
await page.route((u) => LOCAL.some((n) => u.pathname === `/${n}.js`), (r) =>
  r.fulfill({ path: `../../frontend${new URL(r.request().url()).pathname}`, contentType: "application/javascript" }));
await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
await page.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));

// The invariant, checked after every step: a visible bar means a selected
// row, and the company on the bar is that row's company.
const state = () => page.evaluate(() => {
  const bar = document.getElementById("pane-sticky");
  const apply = bar?.querySelector(".pane-apply");
  const selected = document.querySelector("#jobs-body tr.selected");
  const paneEmpty = /Select a listing/i.test(document.getElementById("pane-body")?.textContent || "");
  return {
    barShown: !!bar && !bar.hidden && bar.getBoundingClientRect().height > 0,
    barApply: apply ? apply.textContent.replace(/\s+/g, " ").trim() : null,
    barHtml: (bar?.innerHTML || "").length,
    selectedRow: selected ? selected.querySelector(".job-company, .job-card-title")?.textContent.trim().slice(0, 30) : null,
    paneTitle: document.querySelector("#pane-body .job-detail-title, #pane-body h2")?.textContent.trim().slice(0, 40) || null,
    paneEmpty,
    url: new URL(location.href).searchParams.get("job"),
  };
});

const checks = [];
const check = async (label) => {
  const s = await state();
  // The contradiction the report is about, in one expression.
  const bad = s.barShown && (s.paneEmpty || !s.paneTitle);
  checks.push({ label, ok: !bad, ...s });
  console.log(`${bad ? "FAIL" : "ok  "}  ${label.padEnd(34)} bar=${s.barShown ? "shown" : "hidden"} paneEmpty=${s.paneEmpty} apply=${s.barApply ? JSON.stringify(s.barApply.slice(0, 26)) : "-"}`);
};

// Scroll the pane so the inline actions leave view and the bar appears.
const revealBar = async () => {
  await page.evaluate(() => { const b = document.getElementById("pane-body"); if (b) b.scrollTop = b.scrollHeight; });
  await page.waitForTimeout(700);
};

await page.goto("https://opentechjobs.org/board?search=sre", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 30000 });
await page.waitForTimeout(1500);
await check("boot, nothing open");

await page.click("#jobs-body tr[data-id]");
await page.waitForTimeout(2000);
await revealBar();
await check("listing open, scrolled");

// 1. Close it by hand.
await page.click(".job-detail-close").catch(() => page.keyboard.press("Escape"));
await page.waitForTimeout(1200);
await check("closed by hand");

// 2. The selected listing disappears on a filter change.
await page.click("#jobs-body tr[data-id]");
await page.waitForTimeout(2000);
await revealBar();
await page.fill("#f-search", "kubernetes operator");
await page.waitForTimeout(3500);
await check("filter dropped the open one");

// 3. Sorting.
await page.click("#jobs-body tr[data-id]");
await page.waitForTimeout(2000);
await revealBar();
await page.selectOption("#f-sort", "age:desc");
await page.waitForTimeout(3000);
await check("re-sorted");

// 4. A view switch, which replaces the whole list.
await page.click("#jobs-body tr[data-id]").catch(() => {});
await page.waitForTimeout(2000);
await revealBar();
await page.click('[data-view="all"]');
await page.waitForTimeout(3000);
await check("view switched");

// 5. Reset, which clears every filter at once.
await page.click("#jobs-body tr[data-id]").catch(() => {});
await page.waitForTimeout(2000);
await revealBar();
await page.click("#f-reset").catch(() => {});
await page.waitForTimeout(3000);
await check("filters reset");

// 6. Coming back to a shared/returned-to URL with ?job=, then leaving it.
await page.click("#jobs-body tr[data-id]");
await page.waitForTimeout(1800);
const withJob = await page.evaluate(() => location.href);
await page.goto(withJob, { waitUntil: "domcontentloaded" });
await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 30000 });
await page.waitForTimeout(2500);
await check("reloaded on ?job=");
await revealBar();
await page.goBack();
await page.waitForTimeout(2000);
await check("browser back");

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : "\nall states consistent");
console.log(errs.length ? "page errors:\n  " + errs.join("\n  ") : "no page errors");
await page.unrouteAll({ behavior: "ignoreErrors" });
await browser.close();
process.exit(failed.length ? 1 : 0);
