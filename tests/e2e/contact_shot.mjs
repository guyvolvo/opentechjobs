// The contact page at the widths it changes shape at, plus the two
// states the form can end in.
//
//   node contact_shot.mjs
//
// This checkout's contact.html/style.css/app.js against the live site,
// the same file swap board_layout_shot.mjs makes.
import { chromium } from "playwright";

const SHOTS = [
  { name: "contact-desktop", w: 1440, h: 1000, dark: false },
  { name: "contact-desktop-dark", w: 1440, h: 1000, dark: true },
  { name: "contact-laptop", w: 1024, h: 900, dark: false },
  { name: "contact-phone", w: 390, h: 844, dark: false },
];

const browser = await chromium.launch();
const errs = [];

for (const shot of SHOTS) {
  const page = await browser.newPage({ viewport: { width: shot.w, height: shot.h } });
  page.on("pageerror", (e) => errs.push(`${shot.name}: ${e.message}`));
  await page.addInitScript((dark) => {
    localStorage.setItem("iljobs_geo_asked", "1");
    if (dark) localStorage.setItem("iljobs_theme", "dark");
  }, shot.dark);
  await page.route(/\/contact(\?|$)/, (r) => r.fulfill({ path: "../../frontend/contact.html", contentType: "text/html" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.goto("https://opentechjobs.org/contact", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".contact-topic", { timeout: 15000 });
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${shot.name}.png`, fullPage: shot.w < 900 });

  if (shot.name === "contact-desktop") {
    // Bug report, which is the one topic that adds a row.
    await page.click('.contact-topic[data-topic="Bug report"]');
    await page.fill("#contact-message", "The board hangs when I sort by listing.");
    await page.waitForTimeout(300);
    await page.screenshot({ path: "contact-bug.png" });
    // Validation, then the sent panel, without posting anything.
    await page.fill("#contact-email", "not-an-address");
    await page.click("#contact-send");
    await page.waitForTimeout(200);
    await page.screenshot({ path: "contact-invalid.png" });
    await page.evaluate(() => {
      document.getElementById("contact-sent-where").textContent = "Thanks. The reply will go to you@example.com.";
      document.getElementById("contact-form").hidden = true;
      document.getElementById("contact-sent").hidden = false;
    });
    await page.waitForTimeout(200);
    await page.screenshot({ path: "contact-sent.png" });
  }
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.close();
}

await browser.close();
console.log(errs.length ? `page errors:\n  ${errs.join("\n  ")}` : "no page errors");
