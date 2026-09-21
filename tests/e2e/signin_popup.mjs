import { chromium } from "playwright";
const b = await chromium.launch();
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com", exp: Math.floor(Date.now() / 1000) + 86400 })).toString("base64url") + ".y";
const errs = [];
// 1. landing: Sign in opens a dialog in place, no navigation
let ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
let page = await ctx.newPage();
page.on("pageerror", (e) => errs.push("landing: " + e.message));
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(1200);
await page.click("#hero-signin");
await page.waitForTimeout(500);
console.log("landing dialog:", (await page.textContent(".signin-dialog")).trim().replace(/\s+/g, " ").slice(0, 70), "| url unchanged:", page.url().endsWith("/"));
await page.screenshot({ path: "tests/e2e/signin-popup-landing.png" });
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
console.log("closes on Escape:", (await page.locator(".signin-dialog").count()) === 0);
await ctx.close();
// 2. board: auth still works after the extraction
for (const [name, storage] of [["board-signed-out", {}], ["board-signed-in", { iljobs_auth_tokens: JSON.stringify({ id_token: t }) }]]) {
  ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  page = await ctx.newPage();
  page.on("pageerror", (e) => errs.push(name + ": " + e.message));
  await page.addInitScript((kv) => { try { localStorage.setItem("iljobs_geo_asked", "1"); for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto("http://127.0.0.1:8000/board", { waitUntil: "load" });
  await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 50000 });
  await page.waitForTimeout(1500);
  console.log(name, "| nav:", (await page.textContent(".topnav")).trim().replace(/\s+/g, " ").slice(0, 60), "| rows:", await page.locator("#jobs-body tr[data-id]").count());
  await ctx.close();
}
// 3. account page still signs in
ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
page = await ctx.newPage();
page.on("pageerror", (e) => errs.push("account: " + e.message));
await page.goto("http://127.0.0.1:8000/account", { waitUntil: "load" });
await page.waitForTimeout(2000);
console.log("account dialog:", (await page.textContent(".signin-dialog")).trim().replace(/\s+/g, " ").slice(0, 60));
await ctx.close();
await b.close();
console.log(errs.length ? "PAGE ERRORS: " + errs.join(" | ") : "no page errors");
