// Landing bar signed out and signed in, and the contact page, from the dev server.
import { chromium, devices } from "playwright";
const origin = "http://127.0.0.1:8000";
const b = await chromium.launch();
const fakeToken = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com" })).toString("base64url") + ".y";
async function shot(name, path, opts, storage, clip) {
  const ctx = await b.newContext({ ...opts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log(name, "pageerror:", e.message));
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto(origin + path, { waitUntil: "load" });
  await page.waitForTimeout(1200);
  if (name.includes("signed-in")) { await page.click("#hero-account-btn"); await page.waitForTimeout(300); }
  await page.screenshot({ path: `tests/e2e/${name}.png`, clip });
  console.log(name, "| account:", (await page.textContent("#hero-account")).trim().replace(/\s+/g, " ").slice(0, 80));
  await ctx.close();
}
const desk = { viewport: { width: 1440, height: 900 } };
await shot("landing-bar-signed-out", "/", desk, {}, { x: 0, y: 0, width: 1440, height: 120 });
await shot("landing-bar-signed-in", "/", desk, { iljobs_auth_tokens: JSON.stringify({ id_token: fakeToken }) }, { x: 900, y: 0, width: 540, height: 380 });
await shot("contact-desktop", "/contact", desk, {});
await shot("contact-phone", "/contact", { ...devices["iPhone 13"] }, {});
// the form, end to end against the dev server's handler (mail send will fail without SES rights: the page must say so plainly)
const ctx = await b.newContext({ ...desk, serviceWorkers: "block" });
const page = await ctx.newPage();
await page.goto(origin + "/contact", { waitUntil: "load" });
await page.fill("#contact-email", "dana@example.com"); await page.fill("#contact-message", "Hello from the automated check, please ignore.");
await page.click("#contact-send"); await page.waitForTimeout(6000);
console.log("form status:", await page.textContent("#contact-status"));
await ctx.close();
await b.close();
