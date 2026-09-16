// The board for /hero's devices: 16:10 for the MacBook screen, and a
// phone-sized shot for the iPhone. Light and dark each.
// Run from tests/e2e:  node board_shot.mjs
import { chromium, devices } from "@playwright/test";
const browser = await chromium.launch();
for (const [name, opts] of [
  ["desktop", { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 }],
  ["phone", { ...devices["iPhone 14"], isMobile: false, hasTouch: true }],
]) {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext(opts);
    await context.addInitScript((t) => { try { localStorage.setItem("iljobs_theme", t); } catch {} }, theme);
    const page = await context.newPage();
    await page.goto("https://opentechjobs.org/", { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("#jobs-body tr", { timeout: 60000 });
    await page.waitForTimeout(4000);
    await page.screenshot({ path: `board-${name}-${theme}.png` });
    console.log(name, theme, "shot");
    await context.close();
  }
}
await browser.close();
