// The browser engine (frontend/cv_skills.js) against the labelled cases and
// held-out CVs, and against the Python engine (api/skills.py) for identical
// output.
//
// The spec and the Python results come from running api/skills.py itself,
// so this cannot pass against a stale copy of the rules. Set
// SKILL_PARITY_FILE to a JSON array of extra strings (real job descriptions,
// say) to hold the parity check to those too.
//
// Run:  node tests/test_cv_skills.mjs
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const engine = require(join(root, "frontend", "cv_skills.js"));

const python = [join(root, ".venv", "Scripts", "python.exe"), join(root, ".venv", "bin", "python")].find(existsSync) || "python";
const fixtures = JSON.parse(readFileSync(join(root, "tests", "fixtures", "skill_cases.json"), "utf8")).cases;
const docs = JSON.parse(readFileSync(join(root, "tests", "fixtures", "cv_documents.json"), "utf8")).documents;
const extra = process.env.SKILL_PARITY_FILE ? JSON.parse(readFileSync(process.env.SKILL_PARITY_FILE, "utf8")) : [];
const texts = [...fixtures.map((c) => c.text), ...docs.map((d) => d.text), ...extra];

const py = [
  "import json, sys",
  `sys.path.insert(0, ${JSON.stringify(join(root, "api"))})`,
  "import skills",
  "texts = json.loads(sys.stdin.read())",
  "sys.stdout.write(json.dumps({'spec': skills.spec(), 'results': [skills.extract(t) for t in texts]}))",
].join("\n");
const out = JSON.parse(execFileSync(python, ["-c", py], {
  input: JSON.stringify(texts), maxBuffer: 512 * 1024 * 1024, env: { ...process.env, PYTHONIOENCODING: "utf-8" },
}).toString("utf8"));
const spec = out.spec;

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`);
  if (!ok) failures.push(name);
};

let expectedTotal = 0, hits = 0, forbiddenHits = 0;
const score = (kind, item) => {
  const found = new Set(engine.extractLabels(item.text, spec));
  const missing = (item.expect || []).filter((l) => !found.has(l));
  const wrong = (item.forbid || []).filter((l) => found.has(l));
  expectedTotal += (item.expect || []).length;
  hits += (item.expect || []).length - missing.length;
  forbiddenHits += wrong.length;
  check(`${kind} ${item.id}`, !missing.length && !wrong.length,
    `missing ${JSON.stringify(missing)} wrongly found ${JSON.stringify(wrong)}`);
};
fixtures.forEach((c) => score("case", c));
docs.forEach((d) => score("cv", d));
console.log("");
console.log(`recall ${hits}/${expectedTotal} = ${(100 * hits / expectedTotal).toFixed(1)}%, forbidden labels found: ${forbiddenHits}`);

let mismatches = 0;
texts.forEach((t, i) => {
  const js = JSON.stringify(engine.extract(t, spec));
  const pyr = JSON.stringify(out.results[i]);
  if (js !== pyr) {
    mismatches++;
    if (mismatches <= 5) console.log(`  parity mismatch on text ${i}: ${JSON.stringify(t.slice(0, 80))}\n    js ${js.slice(0, 300)}\n    py ${pyr.slice(0, 300)}`);
  }
});
check(`the two engines agree on all ${texts.length} texts`, mismatches === 0, `${mismatches} differ`);

console.log("");
if (failures.length) {
  console.log(`${failures.length} failed:`);
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log("all passed");
