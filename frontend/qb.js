// The query builder: a small state object in, one SQL string out.
//
// Its own file, with no DOM in it, so the same code runs under node for
// the tests and in the browser for the page. Everything the builder can
// express is here; everything it cannot is what the SQL mode is for.
//
// The shape it produces is deliberately plain SQL a person could have
// typed: named columns, one GROUP BY, an ORDER BY on the metric. Switching
// to SQL mode shows exactly this text, so the builder doubles as a way to
// learn the schema.

(function (root) {
  "use strict";

  // Fields a visitor can filter on. kind decides the control: a picker
  // (multi-select over the column's distinct values), free text matched
  // with LIKE, or a date range.
  const FIELDS = {
    category:      { label: "Category",   kind: "pick", col: "j.category" },
    seniority:     { label: "Seniority",  kind: "pick", col: "j.seniority" },
    workplace:     { label: "Workplace",  kind: "pick", col: "j.workplace" },
    ats:           { label: "ATS",        kind: "pick", col: "j.ats" },
    salary_source: { label: "Pay info",   kind: "pick", col: "j.salary_source" },
    company:       { label: "Company",    kind: "pick", col: "j.company", searchable: true },
    skill:         { label: "Skill",      kind: "pick", col: null },   // via job_skills
    location:      { label: "Location",   kind: "text", col: "j.location" },
    title:         { label: "Title",      kind: "text", col: "j.title" },
    first_seen:    { label: "First seen", kind: "date", col: "j.first_seen" },
  };

  // What a result can be grouped by. expr is the SELECT expression;
  // join says what it needs on the FROM side.
  const GROUPS = {
    none:          { label: "Listings (no grouping)" },
    category:      { label: "Category",      expr: "COALESCE(j.category, 'other')" },
    seniority:     { label: "Seniority",     expr: "COALESCE(j.seniority, 'unstated')" },
    workplace:     { label: "Workplace",     expr: "COALESCE(j.workplace, 'unstated')" },
    ats:           { label: "ATS",           expr: "j.ats" },
    salary_source: { label: "Pay info",      expr: "COALESCE(j.salary_source, 'none')" },
    company:       { label: "Company",       expr: "COALESCE(c.name, j.company)", join: "companies" },
    skill:         { label: "Skill",         expr: "j.skill", skill: true },
    day:           { label: "Day first seen",   expr: "substr(j.first_seen, 1, 10)" },
    week:          { label: "Week first seen",  expr: "strftime('%Y-W%W', j.first_seen)" },
    month:         { label: "Month first seen", expr: "substr(j.first_seen, 1, 7)" },
  };

  const METRICS = {
    count:         { label: "Listings",        col: "listings" },
    avg_days_open: { label: "Average days open", col: "avg_days_open" },
    share:         { label: "Share of listings %", col: "share_pct" },
  };

  const STATUS = {
    open:   { label: "Open now",  where: "j.closed_at IS NULL" },
    closed: { label: "Closed",    where: "j.closed_at IS NOT NULL" },
    all:    { label: "Open and closed", where: null },
  };

  function defaultState() {
    return { status: "open", filters: [], group: "category", metric: "count", limit: 25 };
  }

  // A SQL string literal. Values come from the page's own controls and
  // run read-only in the visitor's tab, so this is about producing valid
  // SQL from a value containing a quote, not about defending a server.
  function lit(v) {
    return "'" + String(v).replace(/'/g, "''") + "'";
  }

  // One filter as SQL. `on` says what "j" is: the jobs table, or
  // job_skills, which carries the same filter columns plus the skill.
  function filterSql(f, on) {
    const spec = FIELDS[f.field];
    if (!spec) return null;
    if (spec.kind === "pick") {
      const vals = (f.values || []).filter((v) => v !== "" && v != null);
      if (!vals.length) return null;
      const inList = " IN (" + vals.map(lit).join(", ") + ")";
      if (f.field === "skill") {
        // On job_skills the skill is right there. On jobs it is the set
        // of listings that have it, which the skill index answers on its
        // own. Never a test run once per listing: that was 115,000
        // lookups into a table read over HTTP, and it did not finish.
        return on === "skills" ? "j.skill" + inList
          : "j.rowid IN (SELECT job_rowid FROM job_skills WHERE skill" + inList + ")";
      }
      return (on === "skills" ? "j." + f.field : spec.col) + inList;
    }
    if (spec.kind === "text") {
      const t = (f.text || "").trim();
      if (!t) return null;
      return spec.col + " LIKE " + lit("%" + t + "%");
    }
    if (spec.kind === "date") {
      const col = on === "skills" ? "j.first_seen" : spec.col;
      const parts = [];
      if (f.from) parts.push(col + " >= " + lit(f.from));
      if (f.to) parts.push(col + " < " + lit(f.to));
      return parts.length ? parts.join(" AND ") : null;
    }
    return null;
  }

  // The columns job_skills copies from the listing. A question that
  // filters by skill and groups by something else reads the distinct
  // listings out of these, so nothing has to touch the jobs table.
  const SKILL_COLS = ["job_rowid", "category", "seniority", "workplace", "ats",
                      "salary_source", "company", "first_seen", "days_open"];

  function buildSql(state) {
    const st = Object.assign(defaultState(), state || {});
    const status = STATUS[st.status] || STATUS.open;
    const group = GROUPS[st.group] || GROUPS.none;
    const metric = METRICS[st.metric] || METRICS.count;
    const limit = Math.max(1, Math.min(5000, parseInt(st.limit, 10) || 25));
    const filters = st.filters || [];

    // Which table answers this. job_skills carries every filter column
    // the listing has, so a question about skills is read entirely from
    // it. The two things it lacks are the free text and the row-mode
    // columns, so those questions read jobs instead.
    const hasText = filters.some((f) => (FIELDS[f.field] || {}).kind === "text" && (f.text || "").trim());
    const hasSkillFilter = filters.some((f) => f.field === "skill" && (f.values || []).filter((v) => v !== "" && v != null).length);
    const rowMode = st.group === "none";
    const skillsMode = !rowMode && !hasText && (group.skill || hasSkillFilter);
    const on = skillsMode ? "skills" : "jobs";

    const where = [];
    if (status.where) where.push(status.where);
    for (const f of filters) {
      const w = filterSql(f, on);
      if (w) where.push(w);
    }
    const whereSql = where.length ? "\nWHERE " + where.join("\n  AND ") : "";

    // Row mode: the listings themselves. The rows to show are chosen in
    // a subquery an index answers, and only those few are then read from
    // the table for their salary text and URL. Written flat, SQLite
    // would fetch every matching row before sorting.
    if (rowMode) {
      const pick = "SELECT rowid FROM jobs j" + whereSql.replace(/\n/g, "\n  ")
        + "\n  ORDER BY j.first_seen DESC LIMIT " + limit;
      return "SELECT j.title, COALESCE(c.name, j.company) AS company, j.location,\n"
        + "       j.seniority, j.salary_text, substr(j.first_seen, 1, 10) AS first_seen, j.url\n"
        + "FROM jobs j\nLEFT JOIN companies c ON c.domain = j.company"
        + "\nWHERE j.rowid IN (\n  " + pick + "\n)"
        + "\nORDER BY j.first_seen DESC";
    }

    // Grouping by skill counts one row per listing per skill, which is
    // what "how many listings ask for this" means. Grouping by anything
    // else has to see each listing once, so the distinct listings come
    // out of job_skills first.
    const dedupe = skillsMode && !group.skill;
    let from;
    if (dedupe) {
      from = "FROM (SELECT DISTINCT " + SKILL_COLS.join(", ") + "\n"
        + "      FROM job_skills j" + whereSql.replace(/\n/g, "\n      ") + ") j";
    } else {
      from = "FROM " + (skillsMode ? "job_skills j" : "jobs j");
    }
    if (group.join === "companies") from += "\nLEFT JOIN companies c ON c.domain = j.company";

    const cols = [group.expr + " AS " + st.group, "COUNT(*) AS listings"];
    if (st.metric === "avg_days_open") {
      cols.push("ROUND(AVG(j.days_open), 1) AS avg_days_open");
    } else if (st.metric === "share") {
      // Share of everything in the same status, not of the filtered set,
      // so "senior roles as a share of open listings" means what it says.
      const denom = "SELECT COUNT(*) FROM jobs j" + (status.where ? " WHERE " + status.where : "");
      cols.push("ROUND(100.0 * COUNT(*) / (" + denom + "), 1) AS share_pct");
    }
    const orderCol = st.metric === "count" ? "listings" : metric.col;
    const timeGroup = st.group === "day" || st.group === "week" || st.group === "month";
    const order = timeGroup ? st.group + " ASC" : orderCol + " DESC";
    return "SELECT " + cols.join(",\n       ") + "\n" + from + (dedupe ? "" : whereSql)
      + "\nGROUP BY 1\nORDER BY " + order + "\nLIMIT " + limit;
  }

  // The state, as a compact string for the URL. JSON is fine: the object
  // is small and every value is user-typed text or a picker value.
  function encodeState(state) {
    return btoa(unescape(encodeURIComponent(JSON.stringify(state))));
  }
  function decodeState(s) {
    try {
      return JSON.parse(decodeURIComponent(escape(atob(s))));
    } catch (e) {
      return null;
    }
  }

  const api = { FIELDS, GROUPS, METRICS, STATUS, defaultState, buildSql, encodeState, decodeState };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.QB = api;
})(typeof window !== "undefined" ? window : globalThis);
