// Builds the OpenTechJobs design system in the current file: styles,
// one component per piece of the board, and a Board frame made of
// instances. Values come from DESIGN.md and frontend/style.css.

const LIGHT = { paper: "#f2f0ef", ink: "#40513b", green: "#609966", greenText: "#3f6f45", grey: "#40513b", line: "#d2cfcb", red: "#b8362c", hover: "#e8e5e2", rowHover: "#e6e3df", rowSelected: "#dce8d4", logoBand: "#edf0ec" };
const DARK = { paper: "#17181c", ink: "#ededec", green: "#2fae60", greenText: "#2fae60", grey: "#9a9a9a", line: "#2b2c31", red: "#ff5d47", hover: "#1e1f24", rowHover: "#23252b", rowSelected: "#1d3024", logoBand: "#23252b" };
const T = LIGHT;

const rgb = (hex) => { const n = parseInt(hex.slice(1), 16); return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255 }; };
const solid = (hex) => [{ type: "SOLID", color: rgb(hex) }];

// The site's faces, with Inter standing in for any not installed.
const FACES = { display: ["Overused Grotesk", "Inter"], ui: ["Source Sans 3", "Inter"], body: ["Helvetica Neue", "Inter"] };
const loaded = new Map();
async function font(kind, style) {
  const key = kind + "/" + style;
  if (loaded.has(key)) return loaded.get(key);
  const candidates = style === "Bold" ? ["Bold", "Semi Bold", "SemiBold"]
    : style === "Semi" ? ["Semi Bold", "SemiBold", "Medium", "Bold"]
    : style === "Black" ? ["Extra Bold", "ExtraBold", "Black", "Bold"]
    : ["Regular"];
  for (const family of FACES[kind]) {
    for (const s of candidates) {
      try {
        await figma.loadFontAsync({ family, style: s });
        loaded.set(key, { family, style: s });
        return loaded.get(key);
      } catch (e) { /* not installed in that weight, try the next */ }
    }
  }
  await figma.loadFontAsync({ family: "Inter", style: "Regular" });
  loaded.set(key, { family: "Inter", style: "Regular" });
  return loaded.get(key);
}

async function text(chars, opts) {
  const { kind = "body", style = "Regular", size = 13, color = T.ink, name } = opts || {};
  const t = figma.createText();
  t.fontName = await font(kind, style);
  t.characters = chars;
  t.fontSize = size;
  t.fills = solid(color);
  t.textAutoResize = "WIDTH_AND_HEIGHT";
  if (name) t.name = name;
  return t;
}

function box(name, opts) {
  const { dir = "HORIZONTAL", pad = [0, 0, 0, 0], gap = 0, fill = null, stroke = null, strokeWeight = 0, radius = 0, align = "CENTER", w = null, h = null, component = false } = opts || {};
  const f = component ? figma.createComponent() : figma.createFrame();
  f.name = name;
  f.layoutMode = dir;
  f.primaryAxisSizingMode = "AUTO";
  f.counterAxisSizingMode = "AUTO";
  f.counterAxisAlignItems = align;
  f.paddingTop = pad[0]; f.paddingRight = pad[1]; f.paddingBottom = pad[2]; f.paddingLeft = pad[3];
  f.itemSpacing = gap;
  f.fills = fill ? solid(fill) : [];
  if (stroke) { f.strokes = solid(stroke); f.strokeWeight = strokeWeight; f.strokeAlign = "INSIDE"; }
  f.cornerRadius = radius;
  f.clipsContent = false;
  if (w != null) {
    f.resize(w, f.height);
    if (dir === "HORIZONTAL") f.primaryAxisSizingMode = "FIXED"; else f.counterAxisSizingMode = "FIXED";
  }
  if (h != null) {
    f.resize(f.width, h);
    if (dir === "HORIZONTAL") f.counterAxisSizingMode = "FIXED"; else f.primaryAxisSizingMode = "FIXED";
  }
  return f;
}

// A bottom rule only: the top bar, the table head and each row draw one.
function bottomRule(node, hex, weight) {
  node.strokes = solid(hex);
  node.strokeAlign = "INSIDE";
  node.strokeTopWeight = 0; node.strokeLeftWeight = 0; node.strokeRightWeight = 0;
  node.strokeBottomWeight = weight;
}
function fixedWidth(node, w) { node.resize(w, node.height); node.layoutSizingHorizontal = "FIXED"; }
function fillWidth(node) { node.layoutSizingHorizontal = "FILL"; }
function setLabel(instance, chars) { const t = instance.findOne((n) => n.type === "TEXT"); if (t) t.characters = chars; }

async function main() {
  const page = figma.createPage();
  page.name = "OpenTechJobs";
  await figma.setCurrentPageAsync(page);

  // Paint styles, both themes.
  for (const pair of [["Light", LIGHT], ["Dark", DARK]]) {
    for (const entry of Object.entries(pair[1])) {
      const s = figma.createPaintStyle(); s.name = pair[0] + "/" + entry[0]; s.paints = solid(entry[1]);
    }
  }
  // Text styles, from the hierarchy in DESIGN.md.
  const styles = [["Display/Section title", "display", "Regular", 34], ["Display/Section title (column)", "display", "Regular", 24], ["Title/Metric", "body", "Black", 30], ["Subtitle", "body", "Bold", 17],
    ["Row title", "body", "Semi", 15], ["Body", "body", "Regular", 13], ["Body/Small", "body", "Regular", 12], ["UI/Control", "ui", "Bold", 13], ["UI/Badge", "ui", "Bold", 12]];
  for (const st of styles) {
    const s = figma.createTextStyle(); s.name = st[0]; s.fontName = await font(st[1], st[2]); s.fontSize = st[3];
  }

  const components = [];
  let cursorY = 0;
  const place = (node, x) => { node.x = x || 0; node.y = cursorY; cursorY += node.height + 40; components.push(node); return node; };

  // Buttons
  const btn = box("Button/Primary", { pad: [0, 16, 0, 16], h: 38, fill: T.ink, stroke: T.ink, strokeWeight: 2, radius: 4, component: true });
  btn.appendChild(await text("Apply ↗", { kind: "ui", style: "Bold", color: T.paper }));
  place(btn);
  const ghost = box("Button/Ghost", { pad: [0, 16, 0, 16], h: 38, fill: T.paper, stroke: T.ink, strokeWeight: 2, radius: 4, component: true });
  ghost.appendChild(await text("Clear all", { kind: "ui", style: "Bold", color: T.ink }));
  place(ghost, 160);

  // Fields
  const search = box("Field/Search", { pad: [0, 10, 0, 10], h: 38, w: 420, fill: T.paper, stroke: T.ink, strokeWeight: 1.5, radius: 4, component: true });
  search.appendChild(await text("Search", { color: T.grey }));
  place(search);
  const select = box("Field/Select", { pad: [0, 10, 0, 10], h: 38, w: 200, fill: T.paper, stroke: T.ink, strokeWeight: 1.5, radius: 4, component: true });
  select.primaryAxisAlignItems = "SPACE_BETWEEN";
  select.appendChild(await text("Categories", { kind: "ui", color: T.ink }));
  const chevron = figma.createVector(); chevron.name = "chevron";
  chevron.vectorPaths = [{ windingRule: "NONZERO", data: "M 0 0 L 5 5 L 10 0" }];
  chevron.strokes = solid(T.ink); chevron.strokeWeight = 1.5; chevron.strokeCap = "ROUND"; chevron.strokeJoin = "ROUND"; chevron.fills = [];
  chevron.resize(10, 5);
  select.appendChild(chevron);
  place(select);

  // Chip, view switch, badge
  const chip = box("Chip/Active filter", { pad: [6, 10, 6, 10], gap: 8, fill: T.greenText, stroke: T.greenText, strokeWeight: 2, radius: 4, component: true });
  chip.appendChild(await text("5 companies", { kind: "ui", style: "Bold", color: T.paper }));
  chip.appendChild(await text("✕", { kind: "ui", style: "Bold", color: T.paper }));
  place(chip);
  const seg = box("View switch", { fill: T.paper, stroke: T.ink, strokeWeight: 1.5, radius: 4, component: true });
  seg.clipsContent = true;
  const labels = ["Show all", "Best matches", "★ Saved"];
  for (let i = 0; i < labels.length; i++) {
    const b = box("Segment/" + labels[i], { pad: [0, 10, 0, 10], h: 28, fill: i === 0 ? T.ink : T.paper });
    b.appendChild(await text(labels[i], { kind: "ui", style: "Bold", color: i === 0 ? T.paper : T.ink }));
    if (i < 2) { b.strokes = solid(T.ink); b.strokeAlign = "INSIDE"; b.strokeTopWeight = 0; b.strokeBottomWeight = 0; b.strokeLeftWeight = 0; b.strokeRightWeight = 1.5; }
    seg.appendChild(b);
  }
  place(seg, 200);
  const badge = box("Badge/Level", { pad: [2, 6, 2, 6], stroke: T.ink, strokeWeight: 1, radius: 4, component: true });
  badge.appendChild(await text("Senior", { kind: "ui", style: "Bold", size: 12, color: T.ink }));
  place(badge, 520);

  // Top bar
  const topbar = box("Top bar", { pad: [18, 40, 18, 40], w: 1440, fill: T.paper, component: true });
  topbar.primaryAxisAlignItems = "SPACE_BETWEEN";
  bottomRule(topbar, T.line, 1);
  const status = box("Status", { gap: 6 });
  const dot = figma.createEllipse(); dot.resize(12, 12); dot.fills = []; dot.strokes = solid(T.greenText); dot.strokeWeight = 1.5; dot.name = "status icon";
  status.appendChild(dot); status.appendChild(await text("Live", { kind: "display", size: 14 }));
  topbar.appendChild(status);
  const nav = box("Nav", { gap: 24 });
  for (const label of ["Sign in", "Dark", "Map", "API"]) nav.appendChild(await text(label, { kind: "ui", style: "Bold", color: T.ink }));
  topbar.appendChild(nav);
  place(topbar);

  // Job row
  const row = box("Job row", { pad: [14, 10, 14, 10], gap: 12, w: 920, fill: T.paper, align: "MIN", component: true });
  bottomRule(row, T.line, 1);
  row.appendChild(await text("☆", { size: 14, color: T.grey, name: "star" }));
  const logo = box("Company logo", { w: 52, h: 52, fill: T.logoBand }); logo.primaryAxisAlignItems = "CENTER";
  logo.appendChild(await text("A", { kind: "display", size: 22, color: T.ink }));
  row.appendChild(logo);
  const bodyCol = box("Body", { dir: "VERTICAL", gap: 3, align: "MIN" });
  const titleLine = box("Title line", { gap: 8 });
  titleLine.appendChild(await text("Senior Software Engineer", { style: "Semi", size: 15, name: "title" }));
  titleLine.appendChild(badge.createInstance());
  bodyCol.appendChild(titleLine);
  bodyCol.appendChild(await text("apple.com · Hardware · Tel Aviv, Israel (Hybrid)", { size: 13, color: T.grey, name: "meta" }));
  const actions = box("Actions", { gap: 12, pad: [8, 0, 0, 0] });
  actions.appendChild(btn.createInstance());
  const save = await text("Save link", { size: 12, color: T.ink, name: "Save link" }); save.textDecoration = "UNDERLINE";
  actions.appendChild(save);
  bodyCol.appendChild(actions);
  row.appendChild(bodyCol); fillWidth(bodyCol);
  const salary = box("Salary", { w: 160, align: "MIN" }); salary.appendChild(await text("Undisclosed", { size: 13, color: T.grey })); row.appendChild(salary);
  const age = box("Age", { w: 110, align: "MIN" }); age.appendChild(await text("2H 4M", { style: "Bold", size: 13, color: T.greenText })); row.appendChild(age);
  place(row);

  // Metric tile and panel
  const tile = box("Metric tile", { dir: "VERTICAL", pad: [18, 16, 18, 16], gap: 10, w: 160, fill: T.paper, align: "MIN", component: true });
  tile.appendChild(await text("Open roles", { size: 12, color: T.grey, name: "label" }));
  tile.appendChild(await text("282,732", { style: "Black", size: 30, color: T.ink, name: "value" }));
  tile.appendChild(await text("1 more unverified", { size: 12, color: T.grey, name: "sub" }));
  place(tile);
  const panel = box("Panel", { dir: "VERTICAL", pad: [18, 20, 20, 20], gap: 10, w: 320, fill: T.paper, stroke: T.line, strokeWeight: 2, radius: 4, align: "MIN", component: true });
  panel.appendChild(await text("Companies with most open roles", { style: "Bold", size: 13 }));
  const bars = [["amazon.com", "14,236", 1], ["aws.amazon.com", "8,310", 0.58], ["apple.com", "4,840", 0.34]];
  for (const bar of bars) {
    const r = box("Bar row", { gap: 10 }); panel.appendChild(r); fillWidth(r);
    const nameText = await text(bar[0], { size: 12, color: T.greenText }); r.appendChild(nameText); fillWidth(nameText);
    const track = figma.createFrame(); track.name = "Bar"; track.resize(100, 6); track.fills = solid(T.line);
    const barFill = figma.createRectangle(); barFill.resize(Math.round(100 * bar[2]), 6); barFill.fills = solid(T.greenText); track.appendChild(barFill);
    r.appendChild(track);
    r.appendChild(await text(bar[1], { size: 12, color: T.ink }));
  }
  place(panel, 220);

  // The Board, assembled from instances.
  const board = box("Board / 1440", { dir: "VERTICAL", w: 1440, fill: T.paper, align: "MIN" });
  board.appendChild(topbar.createInstance());
  const bodyRow = box("Body", { pad: [16, 40, 40, 40], gap: 40, align: "MIN" }); board.appendChild(bodyRow); fillWidth(bodyRow);
  const list = box("List", { dir: "VERTICAL", gap: 12, align: "MIN" }); bodyRow.appendChild(list); fillWidth(list);
  const searchRow = box("Search row", { gap: 10 }); list.appendChild(searchRow); fillWidth(searchRow);
  const si = search.createInstance(); searchRow.appendChild(si); fillWidth(si);
  const go = btn.createInstance(); setLabel(go, "Search"); searchRow.appendChild(go);
  searchRow.appendChild(await text("Showing 1–50 of 282,732 roles", { size: 12, color: T.grey }));
  const sort = select.createInstance(); setLabel(sort, "Newest"); searchRow.appendChild(sort); fixedWidth(sort, 130);
  searchRow.appendChild(ghost.createInstance());
  const split = box("Rail and listings", { gap: 40, align: "MIN" }); list.appendChild(split); fillWidth(split);
  const rail = box("Filter rail", { dir: "VERTICAL", gap: 10, w: 200, align: "MIN" }); split.appendChild(rail);
  for (const label of ["Categories", "Levels", "Companies", "Locations", "Workplace", "Date posted"]) { const f = select.createInstance(); setLabel(f, label); rail.appendChild(f); fillWidth(f); }
  const listings = box("Listings", { dir: "VERTICAL", gap: 0, align: "MIN" }); split.appendChild(listings); fillWidth(listings);
  listings.appendChild(seg.createInstance());
  const head = box("Table head", { pad: [12, 10, 10, 10], gap: 12 }); listings.appendChild(head); fillWidth(head);
  bottomRule(head, T.ink, 2);
  const hl = await text("Listing", { style: "Bold", size: 12, color: T.grey }); head.appendChild(hl); fillWidth(hl);
  head.appendChild(await text("Age ↑", { style: "Bold", size: 12, color: T.greenText }));
  for (let i = 0; i < 5; i++) { const r = row.createInstance(); listings.appendChild(r); fillWidth(r); }
  const stats = box("Statistics", { dir: "VERTICAL", gap: 12, w: 320, align: "MIN" }); bodyRow.appendChild(stats);
  stats.appendChild(await text("Statistics", { kind: "display", size: 24 }));
  const grid = box("Metrics", { gap: 2, fill: T.line }); stats.appendChild(grid); fillWidth(grid);
  const tiles = [["Open roles", "282,732"], ["Companies hiring", "8,897"]];
  for (const tl of tiles) { const t = tile.createInstance(); grid.appendChild(t); fillWidth(t); const texts = t.findAll((n) => n.type === "TEXT"); texts[0].characters = tl[0]; texts[1].characters = tl[1]; }
  const p = panel.createInstance(); stats.appendChild(p); fillWidth(p);
  board.x = 1000; board.y = 0;

  figma.viewport.scrollAndZoomIntoView(components.concat([board]));
  figma.notify("OpenTechJobs: " + components.length + " components, styles for both themes, and a Board frame.");
  figma.closePlugin();
}

main().catch((e) => { figma.notify("Failed: " + e.message); figma.closePlugin(); });
