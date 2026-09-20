// The board as the local dev server serves it: search row, filter rail,
// listings, statistics. Run: node tests/e2e/rail_shot.mjs
import { chromium, devices } from "playwright";
const origin = "http://127.0.0.1:8000";
const b = await chromium.launch();
const shots = [
  ["rail-desktop", { viewport: { width: 1440, height: 900 } }, {}],
  ["rail-desktop-dark", { viewport: { width: 1440, height: 900 } }, { iljobs_theme: "dark" }],
  ["rail-mid", { viewport: { width: 1100, height: 900 } }, {}],
  ["rail-phone", { ...devices["iPhone 13"] }, {}],
];
for (const [name, opts, storage] of shots) {
  const ctx = await b.newContext({ ...opts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.addInitScript((kv) => { try { localStorage.setItem("iljobs_geo_asked", "1"); for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto(origin + "/board", { waitUntil: "load" });
  await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `tests/e2e/${name}.png` });
  if (name === "rail-desktop") {
    await page.click("#ms-department .ms-toggle");
    await page.waitForTimeout(400);
    await page.screenshot({ path: `tests/e2e/${name}-open.png` });
  }
  if (name === "rail-phone") {
    await page.click("#filters-toggle");
    await page.waitForTimeout(400);
    await page.screenshot({ path: `tests/e2e/${name}-open.png` });
  }
  const m = await page.evaluate(() => { const r = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().width) : null; }; return { rail: r(".filter-rail"), main: r(".board-main"), row: r(".filters"), count: document.getElementById("result-count").textContent.trim() }; });
  console.log(name, JSON.stringify(m));
  await ctx.close();
}
await b.close();
