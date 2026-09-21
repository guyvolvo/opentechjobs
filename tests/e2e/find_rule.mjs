import { chromium } from "playwright";
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1440, height: 700 } });
await page.addInitScript(() => { try { localStorage.setItem("iljobs_theme", "dark"); } catch {} });
await page.goto("http://127.0.0.1:8000/account", { waitUntil: "load" });
await page.waitForTimeout(2000);
console.log(JSON.stringify(await page.evaluate(() => {
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const cs = getComputedStyle(el);
    for (const side of ["Top", "Bottom"]) {
      const w = parseFloat(cs["border" + side + "Width"]);
      if (w >= 1 && cs["border" + side + "Style"] !== "none" && el.getBoundingClientRect().width > 600) {
        out.push({ tag: el.tagName.toLowerCase(), cls: el.className.toString().slice(0, 40), side, w, color: cs["border" + side + "Color"], top: Math.round(el.getBoundingClientRect().top) });
      }
    }
  }
  return out.slice(0, 8);
}, null), null, 0));
await b.close();
