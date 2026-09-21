import { chromium } from "playwright";
const b = await chromium.launch();
const errs = [];
// toggling on the landing page, and the choice following to the board
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
const page = await ctx.newPage();
page.on("pageerror", (e) => errs.push(e.message));
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(1000);
console.log("starts:", await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "| label:", await page.textContent("#hero-theme"));
await page.click("#hero-theme");
await page.waitForTimeout(400);
console.log("after click:", await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "| label:", await page.textContent("#hero-theme"),
  "| stored:", await page.evaluate(() => localStorage.getItem("iljobs_theme")));
await page.screenshot({ path: "tests/e2e/landing-dark.png", clip: { x: 0, y: 0, width: 1440, height: 560 } });
await page.goto("http://127.0.0.1:8000/contact", { waitUntil: "load" });
await page.waitForTimeout(800);
console.log("contact inherits:", await page.evaluate(() => document.documentElement.getAttribute("data-theme")));
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(800);
await page.click("#hero-theme");
await page.waitForTimeout(300);
console.log("back to light:", await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "| stored:", await page.evaluate(() => localStorage.getItem("iljobs_theme")));
console.log("sign in border:", await page.evaluate(() => getComputedStyle(document.querySelector("#hero-account a, #hero-account button")).borderTopWidth));
await ctx.close();
await b.close();
console.log(errs.length ? "PAGE ERRORS: " + errs.join(" | ") : "no page errors");
