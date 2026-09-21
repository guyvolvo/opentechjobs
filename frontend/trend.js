// Roles posted per day, for a country and a window. One question, two
// controls, the Explore page's own line chart (.xline). The dates are
// the employers' posting dates, not when the board first saw a listing,
// which is why the last few days read low: a board that says "posted
// this week" resolves days later, and a tenant not re-read yet has not
// told us about today. /api/trend says how many days at the tail are
// still filling in, and the chart draws those dashed and says so.
(function () {
  const $ = (id) => document.getElementById(id);
  if (!$("trend-form")) return;
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // The countries the board actually has listings in, newest answer
  // first: asking for a country with nothing in it is a wasted question.
  async function fillCountries() {
    try {
      const d = await fetch("/api/facets?confidence=all").then((r) => r.json());
      const opts = (d.locations || []).slice(0, 40)
        .map((c) => `<option value="${esc(c.value)}">${esc(c.label || c.value)}</option>`).join("");
      $("trend-country").insertAdjacentHTML("beforeend", opts);
      if ([...$("trend-country").options].some((o) => o.value === "IL")) $("trend-country").value = "IL";
    } catch {
      // The picker still works with Everywhere alone.
    }
  }

  function chart(series, settling) {
    const w = 640, h = 160, pad = 2;
    const ys = series.map((p) => p.n);
    const max = Math.max(1, ...ys);
    const step = series.length > 1 ? (w - pad * 2) / (series.length - 1) : 0;
    const x = (i) => (pad + i * step).toFixed(1);
    const y = (v) => (h - pad - (v / max) * (h - pad * 2)).toFixed(1);
    const cut = Math.max(0, series.length - 1 - settling);
    const line = (from, to) => series.slice(from, to + 1).map((p, k) => `${k ? "L" : "M"}${x(from + k)},${y(p.n)}`).join(" ");
    const tick = (f) => fmt(Math.round(max * f));
    const mid = series[Math.floor(series.length / 2)].date;
    return `<div class="xline">
      <div class="xline-y">${[1, 0.75, 0.5, 0.25, 0].map((f) => `<span>${tick(f)}</span>`).join("")}</div>
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="xline-svg" aria-label="Roles posted per day">
        ${[1, 0.75, 0.5, 0.25].map((f) => `<line class="xline-grid" x1="0" x2="${w}" y1="${y(max * f)}" y2="${y(max * f)}"/>`).join("")}
        <line class="xline-base" x1="0" x2="${w}" y1="${h - pad}" y2="${h - pad}"/>
        <path class="xline-path" d="${line(0, cut)}"/>
        ${settling > 0 ? `<path class="xline-path xline-settling" d="${line(cut, series.length - 1)}"/>` : ""}
      </svg>
      <div class="xline-x"><span>${esc(series[0].date)}</span><span>${esc(mid)}</span><span>${esc(series[series.length - 1].date)}</span></div>
    </div>`;
  }

  let seq = 0;
  async function load() {
    const mine = ++seq;
    const country = $("trend-country").value;
    const days = $("trend-days").value;
    const tech = $("trend-tech").checked;
    $("trend-summary").textContent = "Counting…";
    try {
      const q = new URLSearchParams({ days });
      if (country) q.set("country", country);
      if (tech) q.set("roles", "tech");
      const d = await fetch(`/api/trend?${q}`).then((r) => r.json());
      if (mine !== seq) return;
      if (d.error) throw new Error(d.error);
      const where = country ? `in ${$("trend-country").selectedOptions[0].textContent}` : "everywhere";
      const per = Math.round(d.total / Math.max(1, d.days - d.settling_days));
      $("trend-summary").innerHTML = `<b>${fmt(d.total)}</b> ${tech ? "tech roles" : "roles"} posted ${esc(where)} over ${d.days} days, about <b>${fmt(per)}</b> a day. `
        + `<span class="trend-note">The last ${d.settling_days} days are still filling in, drawn dashed: a posting date reaches us days after it is set.</span>`;
      $("trend-chart").innerHTML = chart(d.series, d.settling_days);
    } catch (e) {
      if (mine !== seq) return;
      $("trend-summary").textContent = `Could not load that: ${e.message}`;
      $("trend-chart").innerHTML = "";
    }
  }

  $("trend-form").addEventListener("change", load);
  fillCountries().then(load);
})();
