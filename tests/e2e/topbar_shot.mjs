import { chromium, devices } from "playwright";
import { readFile } from "node:fs/promises";
const origin = "https://opentechjobs.org";
const LOCAL = { "/board": "frontend/board.html", "/account": "frontend/account.html", "/style.css": "frontend/style.css", "/app.js": "frontend/app.js" };
const b = await chromium.launch();
for (const [name, opts, path] of [["board-desktop", { viewport: { width: 1400, height: 300 } }, "/board"], ["account-desktop", { viewport: { width: 1400, height: 300 } }, "/account"], ["board-phone", { ...devices["iPhone 13"] }, "/board"]]) {
  const ctx = await b.newContext({ ...opts, serviceWorkers: "block" }); const page = await ctx.newPage();
  await page.route(`${origin}/**`, async (route) => { const u = new URL(route.request().url()); const f = LOCAL[u.pathname];
    if (f) return route.fulfill({ status: 200, contentType: f.endsWith(".css") ? "text/css" : f.endsWith(".js") ? "text/javascript" : "text/html", body: await readFile(f) }); return route.continue(); });
  await page.addInitScript(() => { try { localStorage.setItem("geo-prompt-answered", "1"); } catch {} });
  await page.goto(origin + path, { waitUntil: "load" }); await page.waitForTimeout(2500);
  await page.screenshot({ path: `tests/e2e/topbar-${name}.png`, clip: { x: 0, y: 0, width: Math.min(1400, page.viewportSize().width), height: 140 } });
}
await b.close(); console.log("shots done");
