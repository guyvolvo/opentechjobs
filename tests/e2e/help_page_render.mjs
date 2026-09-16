// Renders the API reference the Lambda serves, so it can be looked at
// rather than assumed. The HTML lives in api/help_page.py and links
// /style.css absolutely, which only resolves when it is served from
// inside frontend/, so this drops a preview file there and removes it
// again on the way out.
import { chromium } from "playwright";
import { spawn, execSync } from "node:child_process";
import { writeFileSync, unlinkSync } from "node:fs";

const PORT = 8849;
const OUT = process.argv[2] || "tests/e2e";
const PREVIEW = "frontend/_help_preview.html";

const html = execSync(
  `python -X utf8 -c "import importlib.util;s=importlib.util.spec_from_file_location('hp','api/help_page.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);import sys;sys.stdout.write(m.HELP_HTML)"`,
  { encoding: "utf-8", maxBuffer: 10 * 1024 * 1024 }
);
writeFileSync(PREVIEW, html, "utf-8");

const srv = spawn("python", ["-m", "http.server", String(PORT)], { cwd: "frontend", stdio: "ignore" });
await new Promise(r => setTimeout(r, 1200));

const browser = await chromium.launch();
const shots = [
  { name: "help-desktop-light", width: 1280, height: 1000, dark: false },
  { name: "help-desktop-dark", width: 1280, height: 1000, dark: true },
  { name: "help-phone-light", width: 390, height: 844, dark: false },
  { name: "help-phone-dark", width: 390, height: 844, dark: true },
];

const errors = [];
let overflow = null;

for (const s of shots) {
  const ctx = await browser.newContext({ viewport: { width: s.width, height: s.height },
                                         deviceScaleFactor: 2 });
  if (s.dark) {
    await ctx.addInitScript(() => localStorage.setItem("iljobs_theme", "dark"));
  }
  const page = await ctx.newPage();
  page.on("pageerror", e => errors.push(`${s.name}: ${e}`));
  await page.goto(`http://127.0.0.1:${PORT}/_help_preview.html`, { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/${s.name}.png`, fullPage: true });

  if (s.width === 390) {
    // A curl block or a param row is an unbreakable-width child. If one
    // pushes the document wider than the viewport the whole page scrolls
    // sideways, which is the classic way this page breaks on a phone.
    overflow = await page.evaluate(() => ({
      docWidth: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      widest: [...document.querySelectorAll(".api-help *")]
        .map(el => ({ t: el.tagName + "." + (el.className || ""), w: el.scrollWidth }))
        .sort((a, b) => b.w - a.w)[0],
    }));
  }
  await ctx.close();
}

console.log("shots:", shots.map(s => s.name).join(", "));
console.log("phone overflow check:", JSON.stringify(overflow));
console.log(overflow && overflow.docWidth <= overflow.viewport + 1
  ? "PASS: no horizontal page scroll at 390px"
  : "FAIL: page scrolls sideways at 390px");
console.log(errors.length ? "page errors: " + errors.join(" | ") : "PASS: no page errors");

await browser.close();
srv.kill();
unlinkSync(PREVIEW);
