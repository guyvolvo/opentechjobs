// A QA sweep of the live site: every page type at desktop and phone, light
// and dark. Records console errors, failed requests, broken images, phone
// overflow, leftover brand strings, head tags, and collects every internal
// link for a status check. Screenshots land in qa/.
//
//   node tests/e2e/qa_sweep.mjs [base]      (default https://oceanofjobs.com)
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "fs";

const BASE = process.argv[2] || "https://oceanofjobs.com";
mkdirSync("qa", { recursive: true });

const jobs = await (await fetch(`${BASE}/api/jobs?limit=1&country=IL`)).json();
const JOB = jobs.jobs[0];
const PAGES = [
  ["home", "/"], ["board", "/board"], ["board-il", "/board?country=IL"], ["stats", "/stats"],
  ["account", "/account"], ["contact", "/contact"], ["privacy", "/privacy"], ["api-help", "/api/help"],
  ["job", `/job/${JOB.id}`], ["company", `/company/${JOB.company_domain}`], ["notfound", "/no-such-page"],
];
const VIEWS = [["desk", 1440, 900, "light"], ["desk-dark", 1440, 900, "dark"], ["phone", 390, 844, "light"], ["phone-dark", 390, 844, "dark"]];
const OLD_BRAND = /open ?tech ?jobs|opentechjobs|openmarket/i;

const browser = await chromium.launch();
const report = [];
const links = new Set();
for (const [name, path] of PAGES) {
  for (const [view, w, h, theme] of VIEWS) {
    const page = await browser.newPage({ viewport: { width: w, height: h }, colorScheme: theme });
    const issues = [];
    page.on("console", (m) => { if (m.type() === "error") issues.push(`console: ${m.text().slice(0, 160)}`); });
    page.on("pageerror", (e) => issues.push(`pageerror: ${e.message.slice(0, 160)}`));
    page.on("requestfailed", (r) => {
      const u = r.url();
      if (!/google-analytics|googletagmanager|doubleclick/.test(u)) issues.push(`requestfailed: ${u.slice(0, 120)} ${r.failure()?.errorText || ""}`);
    });
    page.on("response", (r) => {
      const u = r.url();
      if (r.status() >= 400 && u.startsWith(BASE) && !u.includes("/no-such-page") && !u.includes("/api/me/"))
        issues.push(`http ${r.status()}: ${u.slice(0, 120)}`);
    });
    await page.addInitScript((t) => { try { localStorage.setItem("iljobs_theme", t); localStorage.setItem("iljobs_geo_asked", "1"); } catch {} }, theme);
    const resp = await page.goto(BASE + path, { waitUntil: "load", timeout: 45000 }).catch((e) => { issues.push(`goto: ${e.message.slice(0, 100)}`); return null; });
    if (theme === "dark") await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    await page.waitForTimeout(1500);
    const facts = await page.evaluate((oldSrc) => {
      const OLD = new RegExp(oldSrc, "i");
      const meta = (sel, attr = "content") => document.querySelector(sel)?.getAttribute(attr) || null;
      const text = document.body.innerText;
      const oldHits = [...new Set((text.match(new RegExp(oldSrc, "gi")) || []))];
      const headOld = [...document.head.querySelectorAll("*")].map((e) => e.outerHTML).filter((h) => OLD.test(h)).map((h) => h.slice(0, 120));
      const brokenImgs = [...document.images].filter((i) => i.complete && i.naturalWidth === 0 && i.getAttribute("src") && !i.closest("[hidden]"))
        .map((i) => i.getAttribute("src").slice(0, 100));
      const docW = document.documentElement.scrollWidth, winW = window.innerWidth;
      const wide = docW > winW + 1 ? [...document.querySelectorAll("body *")].filter((e) => {
        const r = e.getBoundingClientRect(); return r.right > winW + 1 && r.width > 0 && getComputedStyle(e).position !== "fixed";
      }).slice(0, 4).map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join(".")} right=${Math.round(e.getBoundingClientRect().right)}`) : [];
      const hrefs = [...document.querySelectorAll("a[href]")].map((a) => a.href).filter((u) => u.startsWith(location.origin));
      const words = { signIn: /\bsign in\b/i.test(text), logIn: /\blog in\b/i.test(text) };
      return {
        title: document.title, canonical: meta('link[rel="canonical"]', "href"), ogImage: meta('meta[property="og:image"]'),
        ogTitle: meta('meta[property="og:title"]'), icon: meta('link[rel="icon"]', "href"), desc: meta('meta[name="description"]'),
        font: getComputedStyle(document.body).fontFamily.split(",")[0], bg: getComputedStyle(document.body).backgroundColor,
        oldHits, headOld, brokenImgs, overflow: docW > winW + 1 ? `${docW} > ${winW}` : null, wide, hrefs, words,
        h1: document.querySelector("h1")?.innerText.slice(0, 60) || null,
      };
    }, OLD_BRAND.source);
    facts.hrefs.forEach((u) => links.add(u.split("#")[0]));
    delete facts.hrefs;
    if (facts.oldHits.length) issues.push(`old brand in text: ${facts.oldHits.join(", ")}`);
    facts.headOld.forEach((h) => issues.push(`old brand in head: ${h}`));
    facts.brokenImgs.forEach((s) => issues.push(`broken img: ${s}`));
    if (facts.overflow) issues.push(`horizontal overflow ${facts.overflow}: ${facts.wide.join(" | ")}`);
    report.push({ page: name, view, status: resp?.status(), ...facts, issues: [...new Set(issues)] });
    await page.screenshot({ path: `qa/${name}-${view}.png`, fullPage: false });
    await page.close();
  }
}
await browser.close();

const linkStatus = {};
for (const u of links) {
  if (/\/api\/me|logout|signout/.test(u)) continue;
  try { const r = await fetch(u, { method: "GET", redirect: "follow" }); linkStatus[u] = r.status; } catch (e) { linkStatus[u] = String(e).slice(0, 60); }
}
writeFileSync("qa/report.json", JSON.stringify({ report, linkStatus }, null, 1));
for (const r of report) {
  console.log(`\n## ${r.page} ${r.view} [${r.status}] title="${r.title}" h1="${r.h1}" font=${r.font} bg=${r.bg}`);
  console.log(`   canonical=${r.canonical} og=${r.ogImage} icon=${r.icon} signIn=${r.words.signIn} logIn=${r.words.logIn}`);
  r.issues.forEach((i) => console.log("   ! " + i));
}
console.log("\n## links");
Object.entries(linkStatus).filter(([, s]) => s !== 200).forEach(([u, s]) => console.log(`   ${s} ${u}`));
console.log(`${Object.keys(linkStatus).length} internal links checked`);
