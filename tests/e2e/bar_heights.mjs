import { chromium } from "playwright";
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1440, height: 900 } });
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com" })).toString("base64url") + ".y";
await page.addInitScript((tok) => { try { localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: tok })); } catch {} }, t);
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(900);
console.log(JSON.stringify(await page.evaluate(() => {
  const h = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().height) : null; };
  return { stack: h(".hero-bar-stack"), accountBox: h(".hero-account"), button: h(".hero-account-btn") };
})));
await b.close();
