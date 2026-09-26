// The row's salary chip and the active-filter chips, working copy over
// the live site, light and dark. node tests/e2e/chips_shot.mjs
import { chromium } from "playwright";
const browser = await chromium.launch();
for (const theme of ["light", "dark"]) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.addInitScript((t) => { localStorage.setItem("iljobs_geo_asked", "1"); localStorage.setItem("iljobs_theme", t); }, theme);
  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.goto("https://opentechjobs.org/board?country=IL&salary_min=32000&salary_known=1&workplace=hybrid", { waitUntil: "networkidle" });
  await page.waitForSelector("tr[data-id] .job-chip.salary");
  if (theme === "dark") await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.waitForTimeout(800);
  await page.screenshot({ path: `chips-${theme}.png`, clip: { x: 260, y: 60, width: 900, height: 330 } });
  await page.close();
}
await browser.close();
