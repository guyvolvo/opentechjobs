// The query builder's SQL, checked two ways.
//
// First, structurally under node: every field kind, every group, every
// metric, and the edge cases that produce nothing (empty pickers, blank
// text, a limit outside its range). Second, by emitting every generated
// query on stdout so tests/test_qb_runs.py can execute each one against a
// real explore.db and prove SQLite accepts it. A builder that produces
// plausible-looking SQL that does not parse is worse than no builder.
//
//   node tests/test_qb.js            run the checks
//   node tests/test_qb.js --emit     print one generated query per line

"use strict";

const path = require("path");
const QB = require(path.join(__dirname, "..", "frontend", "qb.js"));

const failures = [];
function check(name, ok, detail) {
  console.log((ok ? "PASS" : "FAIL") + ": " + name + (ok ? "" : "  -- " + detail));
  if (!ok) failures.push(name);
}

const emit = process.argv.includes("--emit");
const generated = [];
function sql(state) {
  const s = QB.buildSql(state);
  generated.push(s);
  return s;
}

// Defaults: open listings by category, counted.
let q = sql(QB.defaultState());
check("default is open listings grouped by category", /closed_at IS NULL/.test(q) && /GROUP BY 1/.test(q) && /category/.test(q), q);
check("default orders by the count, descending", /ORDER BY listings DESC/.test(q), q);

// Every status.
for (const status of Object.keys(QB.STATUS)) sql({ status, group: "category" });
check("'all' status adds no WHERE on closed_at", !/closed_at/.test(sql({ status: "all", group: "category" })));

// Every group, including the ones that need a join.
for (const group of Object.keys(QB.GROUPS)) sql({ group });
check("grouping by skill joins job_skills", /JOIN job_skills s/.test(sql({ group: "skill" })));
check("grouping by company joins companies for the name", /LEFT JOIN companies c/.test(sql({ group: "company" })) && /COALESCE\(c\.name, j\.company\)/.test(sql({ group: "company" })));
check("time groups order chronologically, not by count", /ORDER BY day ASC/.test(sql({ group: "day" })));

// Every metric.
for (const metric of Object.keys(QB.METRICS)) sql({ metric });
check("average days open is rounded and ordered on", /AVG\(j\.days_open\)/.test(sql({ metric: "avg_days_open" })) && /ORDER BY avg_days_open DESC/.test(sql({ metric: "avg_days_open" })));
q = sql({ metric: "share", status: "open" });
check("share divides by the same status, not the filtered set", /\(SELECT COUNT\(\*\) FROM jobs j WHERE j\.closed_at IS NULL\)/.test(q), q);

// Row mode.
q = sql({ group: "none", filters: [{ field: "seniority", values: ["senior"] }] });
check("no grouping lists the rows themselves with a company name", /SELECT j\.title, COALESCE\(c\.name, j\.company\)/.test(q) && !/GROUP BY/.test(q), q);
check("row mode orders newest first", /ORDER BY j\.first_seen DESC/.test(q), q);

// Filters, one of each kind.
q = sql({ filters: [{ field: "category", values: ["Software Engineering", "Data & AI"] }] });
check("a picker becomes IN with quoted values", /j\.category IN \('Software Engineering', 'Data & AI'\)/.test(q), q);
q = sql({ filters: [{ field: "skill", values: ["python"] }] });
check("a skill filter is an EXISTS, so it does not multiply rows", /EXISTS \(SELECT 1 FROM job_skills x/.test(q) && !/JOIN job_skills s/.test(q), q);
q = sql({ filters: [{ field: "location", text: "Tel Aviv" }] });
check("text becomes a LIKE with wildcards both sides", /j\.location LIKE '%Tel Aviv%'/.test(q), q);
q = sql({ filters: [{ field: "first_seen", from: "2026-09-01", to: "2026-09-08" }] });
check("a date range is a half-open interval", /j\.first_seen >= '2026-09-01'/.test(q) && /j\.first_seen < '2026-09-08'/.test(q), q);
q = sql({ filters: [{ field: "first_seen", from: "2026-09-05" }] });
check("a date range with only a start still works", />= '2026-09-05'/.test(q) && !/</.test(q.split("WHERE")[1].split("GROUP")[0].replace(/<=/g, "")), q);

// Quoting: a value containing a quote produces valid SQL.
q = sql({ filters: [{ field: "company", values: ["o'reilly.com"] }] });
check("a quote in a value is doubled", /'o''reilly\.com'/.test(q), q);

// Things that must produce no clause rather than a broken one.
q = sql({ filters: [{ field: "category", values: [] }, { field: "title", text: "   " }, { field: "first_seen" }, { field: "nonsense", values: ["x"] }] });
check("empty, blank, and unknown filters are dropped", !/AND/.test(q) && /closed_at IS NULL/.test(q), q);

// Limit is clamped.
check("limit clamps high", /LIMIT 5000$/.test(sql({ limit: 999999 })));
// Zero and non-numbers fall back to the default rather than clamping to
// one: nobody who types 0 wants one row, and 25 is the answer the page
// opened with.
check("limit falls back to the default for zero and non-numbers", /LIMIT 25$/.test(sql({ limit: "abc" })) && /LIMIT 25$/.test(sql({ limit: 0 })));
check("limit clamps a negative to one", /LIMIT 1$/.test(sql({ limit: -5 })));

// Round trip through the URL encoding.
const st = { status: "closed", filters: [{ field: "title", text: "Ré/sumé 'x'" }], group: "month", metric: "share", limit: 40 };
check("state survives the URL encoding", JSON.stringify(QB.decodeState(QB.encodeState(st))) === JSON.stringify(st));
check("garbage decodes to null rather than throwing", QB.decodeState("not base64!") === null);

if (emit) {
  // One query per line for the runner; newlines inside a query become
  // spaces, which SQLite does not mind.
  for (const s of generated) console.log("SQL\t" + s.replace(/\s+/g, " "));
}

console.log("");
if (failures.length) {
  console.log(failures.length + " failed:");
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("all passed");
