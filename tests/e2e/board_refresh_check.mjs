// The board's refresh schedule, run against the live site with this
// checkout's app.js, style.css and board.html swapped in. A fake clock
// drives the timers and document.hidden is controlled by hand.
//
//   node board_refresh_check.mjs [phone]
//
// Expected: a brief hide refreshes nothing; a long one sends nothing while
// hidden and refreshes listings and ticker together on return; the next
// refresh comes two minutes after that; a new listing arriving while the
// reader is scrolled down waits behind the button and moves nothing; at
// the top it renders in place. "N new listings" counts real arrivals on
// the live board too, so N can be more than the one injected.
import { chromium, devices } from "playwright";
const b = await chromium.launch();
const phone = process.argv[2] === "phone";
const p = await b.newPage(phone ? devices["iPhone 13"] : { viewport: { width: 1400, height: 900 } });
await p.clock.install();
const errs = []; p.on("pageerror", (e) => errs.push(e.message));
await p.addInitScript(() => {
  localStorage.setItem("iljobs_geo_asked", "1");
  // Controllable visibility.
  window.__hidden = false;
  Object.defineProperty(document, "hidden", { get: () => window.__hidden });
  Object.defineProperty(document, "visibilityState", { get: () => (window.__hidden ? "hidden" : "visible") });
});
await p.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
await p.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
await p.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
let inject = false, calls = [];
await p.route(/\/api\/(jobs|stats|facets|health|pipeline)/, async (r) => {
  const u = r.request().url();
  const kind = u.match(/\/api\/(\w+)/)[1] + (u.includes("limit=10&") ? "-ticker" : "");
  calls.push(kind);
  const res = await r.fetch();
  if (inject && kind === "jobs" && u.includes("limit=50")) {
    const body = await res.json();
    const fake = { ...body.jobs[0], id: "fake-new-1", title: "INJECTED NEW ROLE" };
    body.jobs = [fake, ...body.jobs.slice(0, -1)];
    return r.fulfill({ response: res, body: JSON.stringify(body) });
  }
  return r.fulfill({ response: res });
});
await p.goto("https://opentechjobs.org/board");
await p.waitForFunction(() => document.querySelector("#result-count")?.textContent.trim(), null, { timeout: 60000 });
await p.clock.runFor(2000);
const setHidden = (h) => p.evaluate((h) => { window.__hidden = h; document.dispatchEvent(new Event("visibilitychange")); }, h);
const count = (k) => calls.filter((c) => c === k).length;

// 1. brief hide: nothing refreshes
calls = [];
await setHidden(true); await p.clock.runFor(10_000); await setHidden(false); await p.clock.runFor(3000);
console.log("brief hide -> jobs:", count("jobs"), "stats:", count("stats"), "ticker:", count("jobs-ticker"));

// 2. hidden 5 min: no polling while hidden, one full refresh on return
calls = [];
await setHidden(true); await p.clock.runFor(300_000);
console.log("while hidden 5 min -> requests:", calls.length);
await setHidden(false); await p.waitForTimeout(3000); await p.clock.runFor(1000);
console.log("return -> jobs:", count("jobs"), "ticker:", count("jobs-ticker"), "stats:", count("stats"), "health:", count("health"));

// 3. schedule restarts: nothing again until 2 min after the return
calls = [];
await p.clock.runFor(100_000); await p.waitForTimeout(500);
console.log("100s after return -> jobs:", count("jobs"));
await p.clock.runFor(40_000); await p.waitForTimeout(3000);
console.log("140s after return -> jobs:", count("jobs"), "ticker:", count("jobs-ticker"));

// 4. reader scrolled down: new rows held behind the button
inject = true;
await p.evaluate(() => window.scrollTo(0, 1500)); await p.waitForTimeout(300);
const before = await p.evaluate(() => [window.scrollY, document.querySelector("#jobs-body tr")?.dataset.id]);
await setHidden(true); await p.clock.runFor(60_000); await setHidden(false); await p.waitForTimeout(4000);
const held = await p.evaluate(() => [window.scrollY, document.querySelector("#jobs-body tr")?.dataset.id,
  !document.getElementById("new-listings").hidden, document.getElementById("new-listings").textContent]);
await p.screenshot({ path: "C:/Users/admin/AppData/Local/Temp/claude/C--Users-admin-Desktop-Test-scripts/27e70a8e-ff66-4fcc-b7c3-a924176cd57a/scratchpad/held-" + (phone ? "phone" : "desk") + ".png", clip: { x: 0, y: 0, width: phone ? 390 : 1400, height: 300 } });
console.log("scrolled, new row arrives -> scroll/firstRow before", before, "after", held.slice(0, 2), "button", held.slice(2));
await p.click("#new-listings"); await p.clock.runFor(2000); await p.waitForTimeout(800);
console.log("after click ->", await p.evaluate(() => [Math.round(window.scrollY), document.querySelector("#jobs-body tr")?.dataset.id, document.getElementById("new-listings").hidden]));

// 5. at the top: renders in place, no button
inject = false;
await p.evaluate(() => window.scrollTo(0, 0));
await setHidden(true); await p.clock.runFor(60_000); await setHidden(false); await p.waitForTimeout(4000);
console.log("at top, row goes away -> first row", await p.evaluate(() => document.querySelector("#jobs-body tr")?.dataset.id), "button hidden", await p.evaluate(() => document.getElementById("new-listings").hidden));

console.log("errors:", errs);
await b.close();
