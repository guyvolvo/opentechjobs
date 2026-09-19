// A locally rendered company page with the working copy's stylesheet.
// Run: node tests/e2e/company_shot.mjs <rendered.html>
import { chromium, devices } from "playwright";
import { readFile } from "node:fs/promises";

const origin = "https://opentechjobs.org";
const file = process.argv[2];
const b = await chromium.launch();
for (const [name, opts] of [["desktop", { viewport: { width: 1200, height: 1000 } }], ["phone", { ...devices["iPhone 13"] }]]) {
  const ctx = await b.newContext({ ...opts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.route(`${origin}/**`, async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname === "/company/monday.com") return route.fulfill({ status: 200, contentType: "text/html", body: await readFile(file) });
    if (u.pathname === "/style.css") return route.fulfill({ status: 200, contentType: "text/css", body: await readFile("frontend/style.css") });
    return route.continue();
  });
  await page.goto(origin + "/company/monday.com", { waitUntil: "load" });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `tests/e2e/company-${name}.png`, fullPage: false });
  await ctx.close();
}
await b.close();
console.log("shots done");
