import { chromium } from "playwright";
const b = await chromium.launch();
const errs = [];
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
const page = await ctx.newPage();
page.on("pageerror", (e) => errs.push(e.message));
page.on("response", (r) => { if (r.status() === 404) errs.push("404 " + r.url().replace("http://127.0.0.1:8000", "")); });
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(2500);
console.log("trend gone:", (await page.locator(".hero-trend").count()) === 0, "| sections:",
  (await page.evaluate(() => [...document.querySelectorAll("main > section")].map((s) => s.className).join(" | "))));
console.log("sign in present:", (await page.locator("#hero-signin").count()) === 1);
await ctx.close();
await b.close();
console.log(errs.length ? "ISSUES: " + errs.join(" | ") : "clean");
