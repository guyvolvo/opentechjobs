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
    skill:         { label: "Skill",         expr: "j.skill", from: "skills" },
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

  function filterSql(f, idCol) {
    const spec = FIELDS[f.field];
    if (!spec) return null;
    if (spec.kind === "pick") {
      const vals = (f.values || []).filter((v) => v !== "" && v != null);
      if (!vals.length) return null;
      if (f.field === "skill") {
        return "EXISTS (SELECT 1 FROM job_skills x WHERE x.job_rowid = " + idCol + " AND x.skill IN ("
          + vals.map(lit).join(", ") + "))";
      }
      return spec.col + " IN (" + vals.map(lit).join(", ") + ")";
    }
    if (spec.kind === "text") {
      const t = (f.text || "").trim();
      if (!t) return null;
      return spec.col + " LIKE " + lit("%" + t + "%");
    }
    if (spec.kind === "date") {
      const parts = [];
      if (f.from) parts.push(spec.col + " >= " + lit(f.from));
      if (f.to) parts.push(spec.col + " < " + lit(f.to));
      return parts.length ? parts.join(" AND ") : null;
    }
    return null;
  }

  function buildSql(state) {
    const st = Object.assign(defaultState(), state || {});
    const status = STATUS[st.status] || STATUS.open;
    const group = GROUPS[st.group] || GROUPS.none;
    const metric = METRICS[st.metric] || METRICS.count;
    const limit = Math.max(1, Math.min(5000, parseInt(st.limit, 10) || 25));

    // Grouping by skill reads job_skills as "j": it carries every filter
    // column, so the question never touches the wide jobs table. The
    // one thing it lacks is the free text (title, location); a text
    // filter brings the jobs table back in through a join.
    const textFilter = (st.filters || []).some((f) => (FIELDS[f.field] || {}).kind === "text" && (f.text || "").trim());
    const skillsAlone = group.from === "skills" && !textFilter;
    const idCol = skillsAlone ? "j.job_rowid" : "j.rowid";

    const where = [];
    if (status.where) where.push(status.where);
    for (const f of st.filters || []) {
      const w = filterSql(f, idCol);
      if (w) where.push(w);
    }

    const joins = [];
    const needCompanies = group.join === "companies" || st.group === "none";
    if (needCompanies) joins.push("LEFT JOIN companies c ON c.domain = j.company");
    if (group.from === "skills" && !skillsAlone) joins.push("JOIN job_skills s ON s.job_id = j.id");

    const from = (skillsAlone ? "FROM job_skills j" : "FROM jobs j") + (joins.length ? "\n" + joins.join("\n") : "");
    const groupExpr = group.from === "skills" && !skillsAlone ? "s.skill" : group.expr;
    const whereSql = where.length ? "\nWHERE " + where.join("\n  AND ") : "";

    // Row mode: the listings themselves. The rows to show are chosen in
    // a subquery the wide index answers on its own; only those few are
    // then read from the table for their salary text and URL. Written
    // flat, SQLite would fetch every matching row before sorting.
    if (st.group === "none") {
      const pick = "SELECT rowid FROM jobs j" + whereSql.replace(/\n/g, "\n  ") + "\n  ORDER BY j.first_seen DESC LIMIT " + limit;
      return "SELECT j.title, COALESCE(c.name, j.company) AS company, j.location,\n"
        + "       j.seniority, j.salary_text, substr(j.first_seen, 1, 10) AS first_seen, j.url\n"
        + from + "\nWHERE j.rowid IN (\n  " + pick + "\n)"
        + "\nORDER BY j.first_seen DESC";
    }

    // Aggregate mode.
    const cols = [groupExpr + " AS " + st.group, "COUNT(*) AS listings"];
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
    return "SELECT " + cols.join(",\n       ") + "\n" + from + whereSql
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
