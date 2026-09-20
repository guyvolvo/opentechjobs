// The map page from the dev server: the world, a city zoom with a
// popup open, dark, and a phone. Run: node tests/e2e/map_shot.mjs
import { chromium, devices } from "playwright";
const origin = "http://127.0.0.1:8000";
const b = await chromium.launch();
async function open(opts, storage = {}) {
  const ctx = await b.newContext({ ...opts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("pageerror:", e.message));
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto(origin + "/map", { waitUntil: "load" });
  await page.waitForFunction(() => /open roles in/.test(document.getElementById("map-count").textContent), null, { timeout: 60000 });
  await page.waitForTimeout(2500);
  return { ctx, page };
}
let { ctx, page } = await open({ viewport: { width: 1440, height: 900 } });
console.log("count:", await page.textContent("#map-count"));
await page.screenshot({ path: "tests/e2e/map-world.png" });
await page.evaluate(() => window.otjMap.jumpTo({ center: [34.782, 32.085], zoom: 8 }));
await page.waitForTimeout(2500);
console.log("hint:", await page.textContent("#map-hint"));
await page.screenshot({ path: "tests/e2e/map-israel.png" });
const box = await page.locator("#map").boundingBox();
await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
await page.waitForSelector(".map-pop-row", { timeout: 20000 }).catch(() => console.log("no popup rows"));
await page.waitForTimeout(800);
await page.screenshot({ path: "tests/e2e/map-popup.png" });
console.log("popup:", (await page.locator(".maplibregl-popup-content").last().textContent().catch(() => "")).slice(0, 160));
await ctx.close();
({ ctx, page } = await open({ viewport: { width: 1440, height: 900 } }, { iljobs_theme: "dark" }));
await page.screenshot({ path: "tests/e2e/map-dark.png" });
await ctx.close();
({ ctx, page } = await open({ ...devices["iPhone 13"] }));
await page.screenshot({ path: "tests/e2e/map-phone.png" });
await ctx.close();
await b.close();
console.log("shots done");
