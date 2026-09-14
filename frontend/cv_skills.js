// The CV analyser's engine. The same algorithm as api/skills.py, which tags
// every job description, run here in the reader's own browser so a CV is
// never uploaded. The rules are not copied into this file: they arrive from
// the API as skills.spec(), so there is one vocabulary.
//
// Two engines for one algorithm is a drift risk, and the tests are how it
// is held: tests/test_cv_skills.mjs runs this file and api/skills.py over
// the shared fixtures and real job descriptions, and fails if either misses
// a case or if their outputs differ at all. Change one engine, change the
// other, run both. The comments on the algorithm live in api/skills.py.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CvSkills = api;
})(typeof self !== "undefined" ? self : this, function () {
  const ch = (...codes) => String.fromCharCode(...codes);
  const DASHES = new RegExp("[" + ch(0x2010) + "-" + ch(0x2015) + ch(0x2212) + "]", "g");
  const DROP = new RegExp("[" + ch(0x00ad, 0xfeff) + "]", "g");
  const LINE_SEPS = new RegExp("[" + ch(0x0085, 0x2028, 0x2029) + "]", "g");
  const ASTRAL = new RegExp("[" + String.fromCodePoint(0x10000) + "-" + String.fromCodePoint(0x10ffff) + "]", "gu");
  const BULLET_CHARS = ch(0x2022, 0x00b7, 0x25aa, 0x25e6, 0x25cf);
  const DEHYPHEN = /([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])/g;
  // No lookbehind: Safari only has it from iOS 16.4, and on anything older
  // this line was a SyntaxError that stopped the whole analyser loading.
  // The character before the run is captured and put back instead.
  const LETTER_SPACED = /(^|[^A-Za-z0-9])((?:[A-Za-z] ){2,}[A-Za-z])(?![A-Za-z0-9])/g;
  const BOUNDARY = /\n|[.;!?](?=\s|$)/g;
  const BULLETS = new RegExp("^[\\s" + BULLET_CHARS + "*\\-]+");

  function normalize(text) {
    let t = String(text || "").normalize("NFKC");
    t = t.replace(/\r\n?/g, "\n").replace(DROP, "").replace(DASHES, "-");
    t = t.replace(ASTRAL, ch(0xfffd)).replace(LINE_SEPS, "\n");
    t = t.replace(DEHYPHEN, "$1$2");
    return t.replace(LETTER_SPACED, (_, before, g) => before + g.replace(/ /g, ""));
  }

  const asciiLower = (t) => t.replace(/[A-Z]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 32));
  const isAlnum = (c) => c !== undefined && ((c >= "a" && c <= "z") || (c >= "A" && c <= "Z") || (c >= "0" && c <= "9"));
  const isAlpha = (c) => (c >= "a" && c <= "z") || (c >= "A" && c <= "Z");
  const isDigit = (c) => c !== undefined && c >= "0" && c <= "9";

  function occurrences(hay, needle, text, whole) {
    const out = [];
    const n = needle.length, size = text.length;
    let start = 0;
    for (;;) {
      const i = hay.indexOf(needle, start);
      if (i < 0) return out;
      start = i + 1;
      if (i > 0 && isAlnum(needle[0]) && isAlnum(text[i - 1])) continue;
      let end = i + n;
      if (isAlnum(needle[n - 1]) && end < size && isAlnum(text[end])) {
        if (whole) continue;
        const nxt = end + 1 < size && isAlnum(text[end + 1]);
        if (isAlpha(needle[n - 1]) && (text[end] === "s" || text[end] === "S") && !nxt) {
          end += 1;
        } else if (n >= 4 && isDigit(text[end])) {
          let j = end;
          while (j < size && isDigit(text[j])) j++;
          if (j < size && isAlnum(text[j])) continue;
          end = j;
        } else {
          continue;
        }
      }
      out.push([i, end]);
    }
  }

  const compiled = new WeakMap();
  function compile(spec) {
    if (compiled.has(spec)) return compiled.get(spec);
    const rx = {};
    for (const [k, v] of Object.entries(spec.cues)) rx[k] = new RegExp(v, k.startsWith("proper") ? "" : "i");
    const ruleRx = spec.rules.map((r) => ({
      notAfter: r.not_after ? new RegExp(r.not_after, "i") : null,
      notBefore: r.not_before ? new RegExp(r.not_before, "i") : null,
    }));
    const order = new Map(spec.labels.map((l, i) => [l, i]));
    const c = { rx, ruleRx, order, role: new Set(spec.role_words) };
    compiled.set(spec, c);
    return c;
  }

  const bisectLeft = (arr, x) => { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; };
  const bisectRight = (arr, x) => { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m + 1; else hi = m; } return lo; };

  function extract(rawText, spec) {
    const { rx, ruleRx, order, role } = compile(spec);
    const text = normalize(rawText);
    if (!text.trim()) return [];
    const lower = asciiLower(text);
    const bounds = [...text.matchAll(BOUNDARY)].map((m) => m.index);
    const lineStarts = [0, ...[...text.matchAll(/\n/g)].map((m) => m.index + 1)];
    const seg = (pos) => bisectLeft(bounds, pos);
    const segEnd = (pos) => { const k = bisectLeft(bounds, pos); return k < bounds.length ? bounds[k] : text.length; };
    const lineOf = (pos) => bisectRight(lineStarts, pos) - 1;
    const lineText = (li) => text.slice(lineStarts[li], li + 1 < lineStarts.length ? lineStarts[li + 1] - 1 : text.length);

    const negRx = new RegExp(rx.negation.source, "gi");
    const negated = [...text.matchAll(negRx)].map((m) => [m.index + m[0].length, segEnd(m.index + m[0].length)]);

    const cands = [];
    spec.rules.forEach((rule, ri) => {
      const whole = !!rule.whole;
      for (const needle of rule.ci || []) for (const [s, e] of occurrences(lower, needle, text, whole)) cands.push([ri, s, e, false]);
      for (const needle of rule.cs || []) for (const [s, e] of occurrences(text, needle, text, whole)) cands.push([ri, s, e, true]);
    });
    cands.sort((a, b) => a[1] - b[1] || a[0] - b[0]);

    const valid = new Array(cands.length).fill(false);
    const pending = [];
    cands.forEach(([ri, s, e, cs], k) => {
      if (negated.some(([a, b]) => a <= s && s < b)) return;
      const r = ruleRx[ri];
      if (r.notAfter && r.notAfter.test(text.slice(e, e + 40))) return;
      if (r.notBefore && r.notBefore.test(text.slice(Math.max(0, s - 40), s))) return;
      if (cs && spec.rules[ri].context) pending.push(k);
      else valid[k] = true;
    });

    // Header evidence counts only after the proper-name check (see api/skills.py).
    const strongCue = (s, e) => rx.after.test(text.slice(e, e + 40)) || rx.before.test(text.slice(Math.max(0, s - 40), s));
    const headerCue = (s) => rx.header.test(text.slice(lineStarts[lineOf(s)], s));
    const properName = (s, e) => {
      let m = rx.proper_before.exec(text.slice(Math.max(0, s - 30), s));
      if (m && !role.has(m[1].toLowerCase())) return true;
      m = rx.proper_after.exec(text.slice(e, e + 30));
      return !!(m && !role.has(m[1].toLowerCase()));
    };

    let still = [];
    for (const k of pending) {
      const [, s, e] = cands[k];
      if (strongCue(s, e)) valid[k] = true;
      else if (properName(s, e)) continue;
      else if (headerCue(s)) valid[k] = true;
      else still.push(k);
    }

    // A neighbour has to be a separate match (see api/skills.py).
    const near = (k) => {
      const [, s, e] = cands[k];
      const sk = seg(s);
      return cands.some(([, s2, e2], j) => j !== k && valid[j] && (s2 >= e || e2 <= s) && seg(s2) === sk
        && Math.max(s2 - e, s - e2) <= spec.neighbor_chars);
    };
    const listed = (k) => {
      const [, s] = cands[k];
      const li = lineOf(s);
      const item = lineText(li).replace(BULLETS, "").trim();
      if (!item || item.length > spec.list_line_max) return false;
      for (const step of [-1, 1]) {
        let lj = li + step;
        while (lj >= 0 && lj < lineStarts.length && !lineText(lj).trim()) lj += step;
        if (lj >= 0 && lj < lineStarts.length && cands.some((c, j) => valid[j] && lineOf(c[1]) === lj)) return true;
      }
      return false;
    };

    let changed = true;
    while (changed && still.length) {
      changed = false;
      for (const k of [...still]) {
        if (near(k) || listed(k)) {
          valid[k] = true;
          still = still.filter((x) => x !== k);
          changed = true;
        }
      }
    }

    const found = new Map();
    cands.forEach(([ri, s], k) => {
      if (!valid[k]) return;
      for (const label of spec.rules[ri].labels) {
        const slot = found.get(label) || [s, 0];
        slot[0] = Math.min(slot[0], s);
        slot[1] += 1;
        found.set(label, slot);
      }
    });
    const items = [...found.entries()].map(([label, [index, count]]) => ({ label, index, count }));
    items.sort((a, b) => a.index - b.index || order.get(a.label) - order.get(b.label));
    return items;
  }

  const extractLabels = (text, spec, limit) => {
    const labels = extract(text, spec).map((d) => d.label);
    return limit ? labels.slice(0, limit) : labels;
  };

  // Text out of a PDF with its words and lines intact. pdf.js hands back
  // runs of glyphs, not words: a word can arrive split across runs
  // ("Kuber" "netes"), a letter-spaced heading as one run per letter, and
  // line ends only as a flag. Joining every run with a space, as this used
  // to, broke the first and lost the last: on four test CVs printed to PDF
  // it missed 7 of 33 skills and invented one. Runs are joined with a space
  // only where there is a visible gap, and a line ends wherever pdf.js says
  // one did or the baseline moves.
  async function textFromPdf(pdfjs, data) {
    const doc = await pdfjs.getDocument({ data }).promise;
    const pages = [];
    for (let p = 1; p <= doc.numPages; p++) {
      const content = await (await doc.getPage(p)).getTextContent();
      let out = "";
      let lastY = null, lastEnd = null;
      for (const it of content.items) {
        if (typeof it.str !== "string") continue;
        const [, , c, d, x, y] = it.transform;
        const h = Math.hypot(c, d) || it.height || 10;
        if (lastY !== null && it.str) {
          if (Math.abs(y - lastY) > h * 0.5) out += "\n";
          else if (x - lastEnd > h * 0.15 && !/\s$/.test(out) && !/^\s/.test(it.str)) out += " ";
        }
        out += it.str;
        if (it.hasEOL) {
          out += "\n";
          lastY = null;
        } else if (it.str) {
          lastY = y;
          lastEnd = x + it.width;
        }
      }
      pages.push(out);
    }
    return pages.join("\n");
  }

  return { normalize, extract, extractLabels, textFromPdf };
});
