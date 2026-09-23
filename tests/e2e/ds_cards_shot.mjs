import { chromium } from "playwright";
import { readdirSync, statSync } from "node:fs";
import { join, resolve, basename } from "node:path";
import { pathToFileURL } from "node:url";

const OUT = process.argv[2];
const root = resolve("../../ds-bundle/components");
const files = [];
(function walk(d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) walk(p);
    else if (p.endsWith(".html")) files.push(p);
  }
})(root);

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1280, height: 800 } });
const errs = [];
p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
p.on("requestfailed", (r) => errs.push("FAIL " + r.url()));

for (const f of files) {
  const name = basename(f, ".html");
  await p.goto(pathToFileURL(f).href);
  await p.evaluate(() => document.fonts.ready);
  const fonts = await p.evaluate(() => [...document.fonts].filter((x) => x.status === "loaded").map((x) => x.family).join(","));
  await p.screenshot({ path: join(OUT, `card-${name}.png`), fullPage: true });
  console.log(name, "fonts:", fonts || "(none)");
}

const row = files.find((f) => f.includes("JobRow"));
await p.goto(pathToFileURL(row).href);
await p.evaluate(() => (document.documentElement.dataset.theme = "dark"));
await p.screenshot({ path: join(OUT, "card-JobRow-dark.png"), fullPage: true });

await p.setViewportSize({ width: 390, height: 800 });
await p.evaluate(() => delete document.documentElement.dataset.theme);
await p.screenshot({ path: join(OUT, "card-JobRow-phone.png"), fullPage: true });

console.log("errors:", errs.join("\n") || "none");
await b.close();
