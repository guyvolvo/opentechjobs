// The Run query button while a query runs: the ring turns, the label is
// hidden, and both revert when the answer lands.
// Run from tests/e2e:  node explore_spinner.mjs
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8841", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const failures = [];
const check = (name, ok, detail = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`); if (!ok) failures.push(name); };

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
await page.goto("http://127.0.0.1:8841/stats.html", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#explore-run", { timeout: 30000 });

const state = () => page.evaluate(() => {
  const btn = document.getElementById("explore-run");
  const sp = btn.querySelector(".btn-spinner");
  const label = btn.querySelector(".btn-label");
  const cs = getComputedStyle(sp);
  return {
    busy: document.body.classList.contains("explore-busy"),
    spinnerOpacity: parseFloat(cs.opacity),
    animation: cs.animationName,
    labelOpacity: parseFloat(getComputedStyle(label).opacity),
    btnWidth: Math.round(btn.getBoundingClientRect().width),
  };
});

const idle = await state();
check("at rest: ring hidden, label shown", idle.spinnerOpacity === 0 && idle.labelOpacity === 1, JSON.stringify(idle));

// Force the busy state rather than waiting on a real query: the CSS is
// what is under test, and the class is what run() toggles.
await page.evaluate(() => document.body.classList.add("explore-busy"));
await page.waitForTimeout(150);
const busy = await state();
check("running: ring shown and turning", busy.spinnerOpacity === 1 && busy.animation === "status-spin", JSON.stringify(busy));
check("running: label hidden", busy.labelOpacity === 0, JSON.stringify(busy));
check("running: the button does not change width", busy.btnWidth === idle.btnWidth, `${idle.btnWidth} -> ${busy.btnWidth}`);
await page.locator("#explore-run").screenshot({ path: "explore-run-busy.png" });

await page.evaluate(() => document.body.classList.remove("explore-busy"));
await page.waitForTimeout(150);
const done = await state();
check("done: back to the label", done.spinnerOpacity === 0 && done.labelOpacity === 1, JSON.stringify(done));

await browser.close();
server.kill();
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
