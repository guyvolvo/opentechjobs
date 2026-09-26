// Phone top bars after the fix, working copy over the live site.
import { chromium } from "playwright";
const browser = await chromium.launch();
for (const [name, path] of [["privacy", "/privacy"], ["board", "/board"], ["contact", "/contact"], ["stats", "/stats"], ["account", "/account"]]) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.goto("https://oceanofjobs.com" + path, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const row = await page.evaluate(() => {
    const vis = (sel) => { const e = document.querySelector(sel); if (!e) return "none"; const r = e.getBoundingClientRect(); return r.width > 0 && getComputedStyle(e).display !== "none" ? `${Math.round(r.left)}-${Math.round(r.right)}` : "hidden"; };
    return { toggle: vis(".hero-nav-toggle"), name: vis(".topbar-name"), nav: vis(".topbar-left .topbar-nav"), overflow: document.documentElement.scrollWidth > innerWidth };
  });
  console.log(name, JSON.stringify(row));
  await page.screenshot({ path: `qa/phonebar-${name}.png`, clip: { x: 0, y: 0, width: 390, height: 70 } });
  await page.close();
}
await browser.close();
