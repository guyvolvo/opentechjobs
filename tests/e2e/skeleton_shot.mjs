// What the board looks like before app.js runs: the bones the page
// stamps in by itself. Blocks app.js so the frame holds still.
// Run: node tests/e2e/skeleton_shot.mjs
import { chromium, devices } from "playwright";
import { readFile } from "node:fs/promises";

const origin = "https://opentechjobs.org";
const LOCAL = { "/board": ["frontend/board.html", "text/html"], "/style.css": ["frontend/style.css", "text/css"] };
const b = await chromium.launch();
for (const [name, opts] of [["desktop", { viewport: { width: 1400, height: 900 } }], ["phone", { ...devices["iPhone 13"] }]]) {
  for (const scheme of ["light", "dark"]) {
    const ctx = await b.newContext({ ...opts, colorScheme: scheme, serviceWorkers: "block" });
    const page = await ctx.newPage();
    await page.route(`${origin}/**`, async (route) => {
      const u = new URL(route.request().url());
      if (u.pathname === "/app.js") return route.fulfill({ status: 200, contentType: "text/javascript", body: "" });
      const hit = LOCAL[u.pathname];
      if (hit) return route.fulfill({ status: 200, contentType: hit[1], body: await readFile(hit[0]) });
      return route.continue();
    });
    await page.goto(origin + "/board", { waitUntil: "load" });
    await page.waitForTimeout(500);
    const n = await page.evaluate(() => document.querySelectorAll("#jobs-body .skeleton-row").length);
    await page.screenshot({ path: `tests/e2e/skeleton-${name}-${scheme}.png`, fullPage: false });
    console.log(name, scheme, "skeleton rows:", n);
    await ctx.close();
  }
}
await b.close();
