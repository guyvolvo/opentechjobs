/**
 * Data-driven inputs. Each row is one scenario, so a new case is a line
 * here rather than a new test body.
 */

export type Expectation = "narrows" | "unchanged" | "empty";

/** Search terms that must survive typing, sharing and reloading. */
export const SEARCH_TERMS: Array<{ name: string; term: string; expect: Expectation }> = [
  { name: "a plain word", term: "engineer", expect: "narrows" },
  { name: "two words", term: "data engineer", expect: "narrows" },
  // Characters that break a naively built query string.
  { name: "an ampersand", term: "r&d", expect: "narrows" },
  { name: "a percent sign", term: "100%", expect: "narrows" },
  { name: "a double quote", term: 'say "hi"', expect: "empty" },
  { name: "a plus sign", term: "c++", expect: "narrows" },
  { name: "a hash", term: "c#", expect: "narrows" },
  { name: "a slash", term: "ui/ux", expect: "narrows" },
  { name: "hebrew", term: "מהנדס", expect: "narrows" },
  { name: "an emoji", term: "🚀", expect: "narrows" },
  // SQL and HTML shaped input. The board must treat these as text; the
  // API binds parameters, so the expectation is "no results", never an
  // error and never a rendered tag.
  { name: "a SQL fragment", term: "' OR 1=1 --", expect: "empty" },
  { name: "a script tag", term: "<script>alert(1)</script>", expect: "empty" },
  { name: "a long string", term: "a".repeat(300), expect: "empty" },
];

/** URL parameters a user might hand-edit, share stale, or truncate. */
export const MALFORMED_PARAMS: Array<{ name: string; query: string; ignored: string[] }> = [
  { name: "a non-numeric age", query: "?max_age_days=abc", ignored: ["max_age_days"] },
  { name: "a negative age", query: "?max_age_days=-5", ignored: ["max_age_days"] },
  { name: "a zero age", query: "?max_age_days=0", ignored: ["max_age_days"] },
  { name: "an enormous age", query: "?max_age_days=99999999999999", ignored: [] },
  { name: "an unknown sort key", query: "?sort=banana", ignored: ["sort"] },
  { name: "an unknown direction", query: "?dir=sideways", ignored: ["dir"] },
  { name: "an unknown seniority", query: "?seniority=notalevel", ignored: [] },
  { name: "an unknown workplace", query: "?workplace=moon", ignored: [] },
  { name: "a negative offset", query: "?offset=-10", ignored: ["offset"] },
  { name: "an enormous offset", query: "?offset=99999999", ignored: [] },
  { name: "a bare equals", query: "?q=", ignored: [] },
  { name: "a repeated parameter", query: "?q=one&q=two", ignored: [] },
  { name: "an unknown parameter", query: "?totally_made_up=1", ignored: [] },
];

/** Multiselect facets and the URL key each writes. */
export const FACETS: Array<{ name: "department" | "seniority" | "company" | "location" | "workplace"; param: string }> = [
  { name: "department", param: "department" },
  { name: "seniority", param: "seniority" },
  { name: "company", param: "company" },
  { name: "location", param: "location" },
  { name: "workplace", param: "workplace" },
];

/** Age options the date-posted select offers, newest first. */
export const AGE_OPTIONS = ["1", "3", "7", "14", "30"] as const;
