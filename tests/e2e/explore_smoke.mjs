// The Explore page still loads clean with the spinner markup in the button.
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8842", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => { if (m.type() === "error" && !/favicon|stats\.json|explore\.json|404/.test(m.text())) errors.push(m.text().slice(0, 120)); });
await page.goto("http://127.0.0.1:8842/stats.html", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#explore-run", { timeout: 30000 });
await page.waitForTimeout(2500);
const m = await page.evaluate(() => {
  const btn = document.getElementById("explore-run");
  return {
    text: btn.textContent.replace(/\s+/g, " ").trim(),
    label: !!btn.querySelector(".btn-label"),
    spinner: !!btn.querySelector(".btn-spinner"),
    clickable: !btn.disabled,
    reset: !!document.getElementById("qb-reset"),
  };
});
console.log(JSON.stringify(m));
console.log(errors.length ? `errors: ${errors.join(" | ")}` : "no page errors");
await browser.close();
server.kill();
process.exit(errors.length ? 1 : 0);
