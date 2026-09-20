import { chromium } from "playwright";
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1440, height: 900 } });
await page.addInitScript(() => { try { localStorage.setItem("iljobs_geo_asked", "1"); } catch {} });
await page.goto("http://127.0.0.1:8000/board", { waitUntil: "load" });
await page.waitForTimeout(1500);
console.log(JSON.stringify(await page.evaluate(() => {
  const w = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().width) : null; };
  const cs = (s, p) => { const e = document.querySelector(s); return e ? getComputedStyle(e)[p] : null; };
  return { board: w("#board"), container: w("#board > .container"), split: w(".board-split"), list: w(".board-list"), body: w(".board-body"),
    railDisplay: cs(".filter-rail", "display"), bodyDisplay: cs(".board-body", "display"), goDisplay: cs("#f-go", "display"), toggleDisplay: cs("#filters-toggle", "display"),
    boardContainerType: cs("#board", "containerType") };
})));
await b.close();
