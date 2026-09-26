// Renders scripts/brand/og.html to frontend/og-hills.jpg, the share card.
// The ring at the left of the blank card is the O. node tests/e2e/render_og.mjs
import { chromium } from "playwright";
import { fileURLToPath } from "url";

const src = new URL("../../scripts/brand/og.html", import.meta.url);
const out = fileURLToPath(new URL("../../frontend/og-hills.jpg", import.meta.url));
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
await page.goto(src.href);
await page.evaluate(() => document.fonts.ready);
const box = await page.$eval(".word", (e) => {
  const r = e.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
});
console.log("word box", box.map(Math.round));
await page.screenshot({ path: out, type: "jpeg", quality: 88 });
await browser.close();
