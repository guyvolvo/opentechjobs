import { chromium } from "playwright";
const b = await chromium.launch();
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com", exp: Math.floor(Date.now() / 1000) + 86400 })).toString("base64url") + ".y";
const errs = [];
for (const [name, path, opener, out] of [
  ["board", "/board", "#topbar-account-btn", "#auth-signout"],
  ["landing", "/", "#hero-account-btn", "#hero-logout"],
]) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errs.push(name + ": " + e.message));
  await page.addInitScript((tok) => { try { localStorage.setItem("iljobs_geo_asked", "1"); localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: tok })); } catch {} }, t);
  await page.goto("http://127.0.0.1:8000" + path, { waitUntil: "load" });
  await page.waitForTimeout(2500);
  await page.click(opener);
  await page.waitForTimeout(300);
  await page.click(out);
  await page.waitForTimeout(800);
  const nav = await page.textContent(name === "board" ? ".topnav" : "#hero-account");
  console.log(name, "after sign out:", nav.trim().replace(/\s+/g, " ").slice(0, 40), "| token cleared:", await page.evaluate(() => !localStorage.getItem("iljobs_auth_tokens")));
  await ctx.close();
}
await b.close();
console.log(errs.length ? "PAGE ERRORS: " + errs.join(" | ") : "no page errors");
