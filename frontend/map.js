// The map: every open listing with a city we could place, as a circle
// where that city is, bigger for more. Zoomed out, one circle per
// country; from CITY_ZOOM in, one per city, and a click on a city asks
// the API for its newest roles. Coordinates come from geo/cities.json,
// which loader/geocode_cities.py filled from OpenStreetMap; counts come
// from /api/map. The base map is OpenFreeMap's rendering of
// OpenStreetMap, vector tiles drawn by MapLibre, which needs no key.
(async function () {
  const $ = (id) => document.getElementById(id);
  const THEME_KEY = "iljobs_theme";
  const CITY_ZOOM = 4.5;
  const STYLES = { light: "https://tiles.openfreemap.org/styles/positron", dark: "https://tiles.openfreemap.org/styles/dark" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
  const ink = () => getComputedStyle(document.documentElement).getPropertyValue("--green").trim() || "#2fae60";

  const map = new maplibregl.Map({
    container: "map",
    style: STYLES[isDark() ? "dark" : "light"],
    center: [12, 26],
    zoom: 1.3,
    minZoom: 1,
    maxZoom: 12,
    attributionControl: { compact: false },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  window.otjMap = map; // for the e2e probes, which have no other handle on it

  let geo, data;
  try {
    [geo, data] = await Promise.all([
      fetch("/geo/cities.json").then((r) => r.json()),
      fetch("/api/map").then((r) => r.json()),
    ]);
  } catch {
    $("map-count").textContent = "The map's data did not load. Reload to try again.";
    return;
  }
  const labels = data.labels || {};
  const roles = (n) => `${fmt(n)} open ${n === 1 ? "role" : "roles"}`;

  const cities = { type: "FeatureCollection", features: [] };
  const countries = { type: "FeatureCollection", features: [] };
  let placedRoles = 0;
  const countriesSeen = new Set();
  const centre = {}; // per country: the placed cities' coordinates weighted by their counts
  for (const [cc, city, n] of data.cities) {
    const p = geo[`${cc}|${city}`];
    if (!p) continue;
    placedRoles += n; countriesSeen.add(cc);
    cities.features.push({ type: "Feature", geometry: { type: "Point", coordinates: [p[1], p[0]] }, properties: { cc, city, n, name: `${city}, ${labels[cc] || cc}` } });
    const c = centre[cc] || (centre[cc] = { lat: 0, lon: 0, w: 0 });
    c.lat += p[0] * n; c.lon += p[1] * n; c.w += n;
  }
  for (const [cc, n] of data.countries) {
    // The country's own point when OpenStreetMap gave one; otherwise
    // where its listings are, which is often the better place anyway.
    const p = geo[cc] || (centre[cc] && [centre[cc].lat / centre[cc].w, centre[cc].lon / centre[cc].w]);
    if (p) countries.features.push({ type: "Feature", geometry: { type: "Point", coordinates: [p[1], p[0]] }, properties: { cc, n, name: labels[cc] || cc } });
  }
  $("map-count").textContent = `${fmt(placedRoles)} open roles in ${fmt(cities.features.length)} cities across ${fmt(countriesSeen.size)} countries, drawn where they are.`;

  // Area follows the count, so four times the roles is twice the
  // radius; a floor so one role is still a dot, a ceiling so New York
  // does not cover New Jersey.
  const radius = (k, floor, ceil) => ["min", ceil, ["max", floor, ["*", k, ["sqrt", ["get", "n"]]]]];
  function draw() {
    const color = ink();
    if (map.getSource("cities")) return;
    map.addSource("countries", { type: "geojson", data: countries });
    map.addSource("cities", { type: "geojson", data: cities });
    map.addLayer({ id: "countries", type: "circle", source: "countries", maxzoom: CITY_ZOOM,
      paint: { "circle-radius": radius(0.34, 5, 46), "circle-color": color, "circle-opacity": 0.32, "circle-stroke-width": 0 } });
    map.addLayer({ id: "cities", type: "circle", source: "cities", minzoom: CITY_ZOOM,
      paint: { "circle-radius": radius(0.5, 3, 34), "circle-color": color, "circle-opacity": 0.42, "circle-stroke-width": 0 } });
  }
  // style.load fires for the first style and again after a theme change
  // empties it. The data arrives on its own clock, so if the style got
  // here first, draw now rather than wait for an event already gone.
  map.on("style.load", draw);
  if (map.isStyleLoaded()) draw();

  // A name and a count under the pointer, the pointer itself a hand.
  const hover = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "map-tip", offset: 8 });
  for (const layer of ["countries", "cities"]) {
    map.on("mousemove", layer, (e) => {
      const f = e.features[0];
      map.getCanvas().style.cursor = "pointer";
      hover.setLngLat(f.geometry.coordinates).setHTML(`${esc(f.properties.name)} · ${roles(f.properties.n)}`).addTo(map);
    });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; hover.remove(); });
  }
  map.on("click", "countries", (e) => {
    hover.remove();
    map.easeTo({ center: e.features[0].geometry.coordinates, zoom: CITY_ZOOM + 0.5 });
  });
  map.on("click", "cities", (e) => {
    hover.remove();
    const f = e.features[0];
    openCity(f.geometry.coordinates, f.properties.cc, f.properties.city, f.properties.n);
  });

  function syncHint() {
    $("map-hint").textContent = map.getZoom() >= CITY_ZOOM ? "Click a city for its newest roles." : "Zoom in to see cities, then click one for its newest roles.";
  }
  map.on("zoomend", syncHint);
  syncHint();

  // A click on a city: the five newest roles there, each its own page,
  // and the board filtered to the city for the rest.
  let open = null;
  async function openCity(lngLat, cc, city, n) {
    const boardUrl = `/board?country=${encodeURIComponent(cc)}&city=${encodeURIComponent(city)}`;
    const head = `<div class="map-pop"><b class="map-pop-title">${esc(city)}, ${esc(labels[cc] || cc)}</b><div class="map-pop-sub">${roles(n)}</div>`;
    const foot = `<a class="btn map-pop-all" href="${boardUrl}">All ${fmt(n)} on the board</a></div>`;
    if (open) open.remove();
    open = new maplibregl.Popup({ maxWidth: "340px", offset: 10 }).setLngLat(lngLat)
      .setHTML(head + `<div class="map-pop-list"><span class="map-pop-sub">Loading the newest…</span></div>` + foot).addTo(map);
    const q = new URLSearchParams({ country: cc, city, limit: "5", sort: "age", dir: "asc" });
    try {
      const d = await fetch(`/api/jobs?${q}`).then((r) => r.json());
      const rows = (d.jobs || []).map((j) =>
        `<div class="map-pop-row"><a class="link" href="/job/${esc(j.id)}">${esc(j.title)}</a><span class="map-pop-co">${esc(j.company_name || j.company_domain || "")}</span></div>`).join("");
      open.setHTML(head + `<div class="map-pop-list">${rows || '<span class="map-pop-sub">Nothing open right now.</span>'}</div>` + foot);
    } catch {
      open.setHTML(head + `<div class="map-pop-list"><span class="map-pop-sub">Could not load them. The board still can.</span></div>` + foot);
    }
  }

  // The same toggle the other pages have; the base map follows it and
  // the circles are drawn again on the new style.
  const themeBtn = $("theme-toggle");
  const paintTheme = () => { themeBtn.textContent = isDark() ? "Light" : "Dark"; };
  themeBtn.addEventListener("click", () => {
    if (isDark()) document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", "dark");
    try { localStorage.setItem(THEME_KEY, isDark() ? "dark" : "light"); } catch {}
    paintTheme();
    map.setStyle(STYLES[isDark() ? "dark" : "light"]);
  });
  paintTheme();
})();
