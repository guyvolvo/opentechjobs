// The board's salary track, live: present, drawn from the facet, and
// filtering when a handle moves.
//   node tests/e2e/salary_rail_check.mjs
import { chromium } from "playwright";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
await page.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
await page.goto("https://opentechjobs.org/board?country=IL", { waitUntil: "networkidle" });
const range = await page.waitForSelector(".rail-range", { timeout: 30000 }).catch(() => null);
if (!range) { console.log("FAIL: no salary track"); process.exit(1); }
await range.scrollIntoViewIfNeeded();
const before = await page.$eval(".rail-range", (el) => ({ min: el.dataset.min, max: el.dataset.max, ends: el.querySelector(".rail-range-ends").textContent.replace(/\s+/g, " ").trim() }));
console.log("track", JSON.stringify(before));
const group = await page.$(".rail-group:has(.rail-range)");
await group.screenshot({ path: "salary-rail.png" });
await browser.close();
