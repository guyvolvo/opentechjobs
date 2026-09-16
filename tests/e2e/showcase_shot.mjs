// The /hero product-shot section, desktop and phone, light and dark.
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8835", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
for (const [label, opts] of [["desktop", { viewport: { width: 1440, height: 900 } }], ["phone", devices["Pixel 7"]]]) {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ ...opts });
    await context.addInitScript((t) => { try { localStorage.setItem("iljobs_theme", t); } catch {} }, theme);
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:8835/hero.html", { waitUntil: "networkidle" });
    await page.waitForTimeout(900);
    const el = page.locator(".hero-showcase");
    await el.scrollIntoViewIfNeeded();
    await page.waitForTimeout(600);
    await el.screenshot({ path: `showcase-${label}-${theme}.png` });
    console.log(label, theme, JSON.stringify(await el.boundingBox()));
    await context.close();
  }
}
await browser.close();
server.kill();
