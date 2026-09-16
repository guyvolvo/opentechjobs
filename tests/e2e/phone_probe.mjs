// Why is the phone that wide at 412px? Report its computed box and every
// rule that sets a width on it, in order.
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8839", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
const context = await browser.newContext({ ...devices["Pixel 7"] });
const page = await context.newPage();
await page.goto("http://127.0.0.1:8839/hero.html", { waitUntil: "networkidle" });
await page.waitForTimeout(600);
const m = await page.evaluate(() => {
  const el = document.querySelector(".iphone");
  const stage = document.querySelector(".device-stage");
  const rules = [];
  for (const sheet of document.styleSheets) {
    let list;
    try { list = sheet.cssRules; } catch { continue; }
    const walk = (rs, media) => {
      for (const r of rs) {
        if (r.cssRules) { walk(r.cssRules, r.conditionText || media); continue; }
        if (!r.selectorText || !/\.iphone(\s|,|$|\{)/.test(r.selectorText + "{")) continue;
        const w = r.style.getPropertyValue("width");
        const mw = r.style.getPropertyValue("max-width");
        if (w || mw) rules.push({ media: media || "(none)", sel: r.selectorText, width: w, maxWidth: mw });
      }
    };
    walk(sheet.cssRules, "");
  }
  const cs = getComputedStyle(el);
  return {
    viewport: innerWidth,
    stageW: Math.round(stage.getBoundingClientRect().width),
    phoneW: Math.round(el.getBoundingClientRect().width),
    phoneH: Math.round(el.getBoundingClientRect().height),
    computedWidth: cs.width, aspect: cs.aspectRatio, position: cs.position,
    rules,
  };
});
console.log(JSON.stringify(m, null, 1));
await browser.close();
server.kill();
