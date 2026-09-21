import { chromium } from "playwright";
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
const page = await ctx.newPage();
await page.addInitScript(() => { try { localStorage.setItem("iljobs_theme", "dark"); } catch {} });
await page.goto("http://127.0.0.1:8000/", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(3500);
console.log("theme:", await page.evaluate(() => document.documentElement.getAttribute("data-theme")),
  "| toggle:", await page.textContent("#hero-theme"),
  "| band:", await page.evaluate(() => getComputedStyle(document.querySelector(".hero-logos")).backgroundColor));
await page.screenshot({ path: "tests/e2e/landing-dark.png", clip: { x: 0, y: 0, width: 1440, height: 520 } });
await ctx.close();
await b.close();
