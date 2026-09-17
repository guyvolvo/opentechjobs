import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8837", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
for (const [label, opts] of [["phone", devices["Pixel 7"]], ["desktop", { viewport: { width: 1440, height: 900 } }]]) {
  const context = await browser.newContext({ ...opts });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:8837/index.html", { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  const m = await page.evaluate(() => {
    const block = document.querySelector(".hero-block");
    const cs = getComputedStyle(block);
    const b = block.getBoundingClientRect();
    const nums = document.querySelector(".hero-ticker.to-right").getBoundingClientRect();
    const track = document.getElementById("ticker-top").getBoundingClientRect();
    const logos = document.querySelector(".hero-logos").getBoundingClientRect();
    return {
      blockTop: Math.round(b.top), blockH: Math.round(b.height),
      padTop: cs.paddingTop, padBottom: cs.paddingBottom, gap: cs.rowGap,
      tickerSize: getComputedStyle(document.getElementById("ticker-top")).fontSize,
      numsOffsetFromTop: Math.round(nums.top - b.top), numsH: Math.round(nums.height),
      trackOffsetFromTop: Math.round(track.top - b.top), trackH: Math.round(track.height),
      logosOffsetFromTop: Math.round(logos.top - b.top), logosH: Math.round(logos.height),
      bottomGap: Math.round(b.bottom - logos.bottom),
    };
  });
  console.log(label, JSON.stringify(m));
  await context.close();
}
await browser.close();
server.kill();
