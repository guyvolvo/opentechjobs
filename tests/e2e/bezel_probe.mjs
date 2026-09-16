// The drawn frame's own numbers, against the measured machine: bezel as a
// fraction of the lid's width, and the notch as a fraction of the screen.
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8838", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
for (const [label, opts, target] of [
  ["laptop", { viewport: { width: 1440, height: 900 } }, { bezel: 0.018, notch: 0.124 }],
  ["phone", devices["Pixel 7"], { bezel: 0.037, island: 0.226 }],
]) {
  const context = await browser.newContext({ ...opts });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:8838/hero.html", { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  const m = await page.evaluate(() => {
    const dev = document.querySelector(".showcase-device");
    const scr = document.querySelector(".device-screen");
    const cs = getComputedStyle(dev);
    const before = getComputedStyle(dev, "::before");
    return {
      lid: dev.getBoundingClientRect().width,
      screen: scr.getBoundingClientRect().width,
      padTop: parseFloat(cs.paddingTop), padSide: parseFloat(cs.paddingLeft),
      notchW: parseFloat(before.width), notchH: parseFloat(before.height),
    };
  });
  const sideRatio = m.padSide / m.lid, topRatio = m.padTop / m.lid;
  console.log(label, JSON.stringify({
    lid: Math.round(m.lid), screen: Math.round(m.screen),
    side: sideRatio.toFixed(4), top: topRatio.toFixed(4), want: target.bezel,
    notch: (m.notchW / m.screen).toFixed(4), wantNotch: target.notch ?? target.island,
  }));
  await context.close();
}
await browser.close();
server.kill();
