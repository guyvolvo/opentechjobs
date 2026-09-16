// The button mid-query at 4x, and the ring at three weights side by side,
// so the choice is made on something legible rather than a 68px thumbnail.
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8843", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 4 });
const page = await context.newPage();
await page.goto("http://127.0.0.1:8843/stats.html", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#explore-run", { timeout: 30000 });

// As it stands, magnified.
await page.evaluate(() => {
  document.body.classList.add("explore-busy");
  // Freeze the ring so the capture is not a random frame.
  document.querySelector(".btn-spinner").style.animationPlayState = "paused";
});
await page.waitForTimeout(200);
await page.locator("#explore-run").screenshot({ path: "spinner-current-4x.png" });

// Three candidates in a row, on the real button colour.
await page.evaluate(() => {
  const btn = document.getElementById("explore-run");
  const row = document.createElement("div");
  row.id = "weights";
  row.style.cssText = "display:flex;gap:18px;padding:18px;background:#f2f0ef";
  for (const [w, dash] of [[3.2, "20 37"], [4, "22 35"], [5, "26 31"]]) {
    const b = btn.cloneNode(true);
    b.removeAttribute("id");
    b.style.cssText = "position:relative";
    const sp = b.querySelector(".btn-spinner");
    sp.style.cssText = "position:absolute;top:50%;left:50%;width:19px;height:19px;margin:-9.5px 0 0 -9.5px;opacity:1";
    const c = sp.querySelector("circle");
    c.setAttribute("stroke-width", String(w));
    c.setAttribute("stroke-dasharray", dash);
    b.querySelector(".btn-label").style.opacity = "0";
    row.appendChild(b);
  }
  document.body.prepend(row);
});
await page.waitForTimeout(200);
await page.locator("#weights").screenshot({ path: "spinner-weights-4x.png" });
console.log("wrote spinner-current-4x.png and spinner-weights-4x.png (3.2, 4, 5)");
await browser.close();
server.kill();
