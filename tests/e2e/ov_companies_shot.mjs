// The empty pane's company block, with the working copy of the frontend
// swapped in over the live site: light and dark, desktop width.
//
//   node tests/e2e/ov_companies_shot.mjs
import { chromium } from "playwright";

const browser = await chromium.launch();
for (const scheme of ["light", "dark"]) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
  await page.goto("https://opentechjobs.org/board?country=IL", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".ov-row-co .company-logo", { timeout: 30000 });
  // The theme is the switch's attribute, not the OS preference.
  if (scheme === "dark") await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.waitForTimeout(2500);
  const rows = await page.$$eval(".ov-row-co", (els) => els.map((e) => {
    const r = e.getBoundingClientRect();
    const img = e.querySelector("img");
    const ir = img.getBoundingClientRect();
    return `${e.querySelector(".ov-co-name").textContent} h=${Math.round(r.height)} img=${Math.round(ir.width)}x${Math.round(ir.height)} src=${img.currentSrc.slice(0, 60)} ok=${img.naturalWidth > 0}`;
  }));
  console.log(scheme, "\n  " + rows.join("\n  "));
  const pane = await page.$("#job-detail");
  await pane.screenshot({ path: `ov-companies-${scheme}.png` });
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.close();
}
await browser.close();
