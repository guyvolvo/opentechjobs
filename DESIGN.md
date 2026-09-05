---
name: OpenMarketIL
description: Swiss-grid job board for the Israeli tech market — ground-truthed data, zero decoration, on an earthy sage-and-cream ground.
colors:
  paper: "#f3f6e4"
  ink: "#40513b"
  signal-green: "#609966"
  muted-grey: "#40513b"
  hairline-grey: "#c7d9b3"
  alert-red: "#b8362c"
  hover-tint: "#e9efdb"
typography:
  display-wordmark:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.02em"
  display-section-title:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "34px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.1em"
  title-metric:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "30px"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "normal"
  body:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.08em"
rounded:
  none: "0px"
  default: "4px"
spacing:
  gutter: "clamp(20px, 4vw, 64px)"
  rule: "2px"
  hairline: "1px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.default}"
    padding: "0 16px"
    height: "38px"
  button-primary-hover:
    backgroundColor: "{colors.signal-green}"
  button-ghost:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.default}"
    padding: "0 16px"
    height: "38px"
  chip:
    backgroundColor: "{colors.signal-green}"
    textColor: "{colors.paper}"
    rounded: "{rounded.default}"
    padding: "6px 10px"
---

# Design System: OpenMarketIL

## Overview

**Creative North Star: "The Exchange Ticker"**

A market terminal, not a careers site: sage-cream paper, deep forest-green
ink, a single lighter green reserved for the one number or status that matters right
now, and a scrolling headline strip up top like a ticker tape. Everything
reads as a live instrument panel over a job market, not as a brand
experience — density and legibility win over warmth every time there's a
conflict between them. The one indulgence is a single display face
(Overused Grotesk, weight 400), kept to two places (the wordmark and the
two section titles) so it reads as a signature, not a typeface choice
bleeding into body text.

Confirmed visual rejections: no box-shadow used for depth, no decorative
color, no third accent hue, no drop-in UI framework look. Every discrete
bordered box (buttons, chips, badges, inputs, floating panels) uses a
flat 4px radius — see Shapes below for exactly what stays sharp instead
(grid-tile cards, the table, dividers) and why.

**Key Characteristics:**
- Flat, two-tone (sage-cream/forest-green) surfaces with color used as signal, not decoration
- Depth built entirely from 2px hairline grids, never shadows
- One bold display face for brand moments; Helvetica for everything data-dense
- Light and dark are two deliberately tuned palettes, not a hex inversion
- A live-status vocabulary (the ticker, the status-dot torch flicker) borrowed from terminals/dashboards, not marketing sites

## Colors

Two-tone by design — sage-tinted cream paper, deep forest-green ink —
with exactly one accent color and one reserved alert color, both used
sparingly enough that their rarity is the signal. Light mode is a fully
earthy, monochromatic-green palette; dark mode is untouched, still a
plain near-black/white inversion.

### Primary
- **Signal Green** (`#609966`, `--green`; dark mode `#2fae60`): the one
  color that means "this matters more than what's around it." Live status
  (online indicator), the accent letters in the wordmark, active
  filter/sort state, hover state on buttons and links, the single
  highlighted metric tile, the most-recent bar in the new-listings chart.
  Never used decoratively — if a green appears, it is pointing at
  something.

### Neutral
- **Paper** (`#f3f6e4`, `--white`; dark mode `#17181c`): the base
  surface — a sage-tinted cream, not pure white; dark mode is a tuned
  near-black, not pure black. Never applied as a "panel" color — every
  ink surface (buttons, the metrics/panel grid background, the topbar
  hover) is a deliberate component, not the page.
- **Ink** (`#40513b`, `--black`; dark mode `#ededec`): body text, borders,
  and every "component" surface (buttons, cards' outer frame, the
  topbar) — a deep forest green standing in for black, not a literal
  black anywhere in light mode. Dark mode's ink is a soft off-white to
  match, unrelated to this light-mode swap.
- **Muted Grey** (`#40513b`, `--grey`; dark mode `#9a9a9a`): secondary
  text — labels, metadata, placeholders, the offline-state wordmark.
  Light mode reads this at full ink darkness (the earlier softer tint
  wasn't legible enough); dark mode keeps its own true muted grey.
- **Hairline Grey** (`#c7d9b3`, `--grey-line`; dark mode `#2b2c31`): the
  quiet dividers — table row separators, bar-chart tracks, input borders
  one step down from a full 2px rule, and (reported live, too
  high-contrast at full `--black`) the Statistics grid's own gap/border
  color, `var(--rule)`'s 2px width kept, just recolored. Darkened from an
  earlier `#dce8ce` in light mode, same reasoning as Muted Grey above:
  too close to `--white`'s `#f3f6e4` to read as a visible line at all.
- **Hover Tint** (`#e9efdb`, `--hover-bg`; dark mode `#1e1f24`): the one
  soft, non-binary surface in the system, reserved for row/option hover
  states where a hard color flip would be too loud.

### Alert Red (reserved, not decorative)
- **Alert Red** (`#b8362c`, `--red`): exactly two uses in the whole
  system — the offline status dot (torch "gone out," animation killed
  outright) and the error-state banner. Never a third context; adding a
  red anywhere else would dilute what it means here.

### Named Rules
**The One Voice Rule.** Green marks exactly one "this matters" element at
a time. If everything is green, nothing is — it never appears as
decoration, only on live status, primary metrics, and the handful of
buttons/links that actually do something.

**The Token Rule.** Light and dark mode are two independently tuned
palettes, not a hex inversion of each other — but every rule in the
system still reads `var(--white)`/`var(--black)` (never a literal hex),
so retuning either palette only ever means editing the two `:root`
blocks. A hardcoded `#fff`/`#000` anywhere breaks that.

## Typography

**Display Font:** Overused Grotesk, weight 400 (self-hosted, variable font spanning 300–900), falling back to Helvetica Neue / Helvetica / Arial
**Body Font:** Helvetica Neue, falling back to Helvetica, Arial, sans-serif

**Character:** A dense, no-serif system voice everywhere text-heavy
(tables, filters, panels), broken only at brand moments by one bold,
grotesque-sans display face — never the reverse.

### Hierarchy
- **Display / Wordmark** (400, 15px, 0.02em tracking, Overused Grotesk): the
  topbar wordmark only ("OpenMarket.IL"). 13px below the 640px breakpoint.
- **Display / Section Title** (400, 34px, uppercase, 1 line-height,
  0.1em tracking, Overused Grotesk): the two section titles ("Job Board" /
  "Statistics") only. Never used at table-row or data-dense sizes.
- **Title/Metric** (800, 30px, tabular-nums): the large number on a
  metric tile — the one place body copy gets genuinely large.
- **Subtitle** (700, 17px): the job-detail panel's title (`.job-detail-title`)
  — needs to read as more prominent than table-row body text without
  competing with the two genuine display-face headings above it.
- **Body** (400, 13–14px): filters, table cells, panel prose, buttons.
- **Label** (700, 10–12px, uppercase, 0.06–0.1em tracking): column
  headers, panel titles, chip text, the result count, the topnav.

### Named Rules
**The Two-Voice Rule.** Helvetica (via `--font`) carries everything
dense; Overused Grotesk (via `--font-display`) is reserved for exactly two
brand-level spots. No third typeface is part of the system.

### Open inconsistency (flagged, not fixed here)
`.api-path` and `.param` in the footer's API Reference (added this
session) use `"Courier New", monospace` — a third font-family the
top-of-file design-principles comment doesn't account for. It's a
narrow, defensible choice for literal code tokens, but it's a real drift
from the documented "two voices" rule as written. Worth a decision:
fold it into the Two-Voice Rule as a named third case, or replace it with
`--font` + a `.param` background treatment to keep the claim literally
true. Left for `/impeccable audit` / a deliberate call, not silently
changed here.

`footer code` (the API-Reference `curl` example) uses literal
`#0a0a0a`/`#2fae60`, not `var(--black)`/`var(--green)`, with its own
inline comment explaining why: a terminal/code block reads as an actual
terminal, so it stays fixed-dark regardless of the page's own light/dark
state, the same way a real code sample in a README doesn't re-theme
itself. A deliberate, narrow exception to the Token Rule below, not
drift — noted here so it doesn't get "fixed" back to a token by mistake.

## Layout

A permanent two-column workspace above 960px — Job Board (flexible,
`1fr`) beside Market Stats (fixed `clamp(360px, 28vw, 460px)`) — never a
drawer or a toggle; they stack (stats below board) under 960px. No
`max-width` container anywhere: both the workspace and its inner
container use `--gutter` (`clamp(20px, 4vw, 64px)`) for side padding, so
the page keeps scaling with viewport width all the way to ultra-wide
instead of plateauing inside a fixed box.

The filter row stays a single line above the mobile breakpoint (flex
`nowrap`, matching the table's own width), shrinking each field rather
than wrapping to a second row; below 640px it wraps and every field
takes a full-width row instead. IL-only lives as a pinned first option
inside the Locations dropdown rather than as its own filter slot,
keeping the row to one line without dropping a filter.

Two dedicated card grids (metrics, market panels) share one motif: white
cards laid edge-to-edge on a black background with a `2px` (`--rule`)
gap, so the black shows through as a hairline grid between cards —
depth and separation from spacing and contrast alone, never a shadow.

## Elevation & Depth

Flat. Zero `box-shadow` anywhere in the system — the one historical
exception (a circular, smoothly-pulsing status dot) was corrected back to
a square block specifically because a rounded, softly-animated shape was
the one place the system's own flatness/sharp-corner rules were broken.
All depth and grouping comes from the 2px black rule grid (metrics/panel
cards) and from solid borders — never from a shadow standing in for
elevation.

### Named Rules
**The No-Shadow Rule.** The black rules between cards are the only depth
cue this design uses. A shadow anywhere is a bug, not a style choice.

## Shapes

4px radius is the default for every discrete bordered box — buttons
(`.btn`, `.job-detail-apply`, `.job-detail-star`, `.job-detail-icon-btn`),
chips and badges (`.chip`, `.badge`, `.job-salary.estimate`), pagination
buttons, inputs/toggles (filter bar, alert-create form, auth email/social
buttons), and every floating panel/dropdown (`.job-detail`, `.ms-menu`,
`.auth-panel`, `.ms-search`/`.ms-clear` inside it), and the
empty/error/loading placeholder boxes. Borders otherwise stay one of
exactly two weights: `var(--rule)` (2px, solid, ink or accent-colored) or
a 1px hairline (`var(--grey-line)` — table row separators); the filter
bar's own inputs/toggles are the one exception at 1.5px, by request.

Sharp corners remain only where there's no real enclosed box to round:
the job table itself and its row separators (a hairline divider, not a
shape with corners), the topbar/section dividers, and the Statistics
grid's individual metric/panel tiles — they share one grid box already
rounded at its outer edge (see below), not individual boxes of their
own. Company logos and skill-chip text are images/plain text, not
bordered boxes, so nothing to round there either.

The Statistics grid (`#metrics-grid` + `#panel-grid`) keeps `var(--rule)`'s
2px width for its card-grid gaps/border but in `--grey-line`, not ink —
full-strength `--black` read too bold for a dense grid of small tiles
(same "quiet divider" reasoning as table row separators, just at 2px
instead of 1px); this box (which shares one seamless visual outline via
the negative-margin overlap above) rounds only its four outer corners at
4px, with `overflow: hidden` so the corner cells' square backgrounds
actually follow the curve.

## Components

### Buttons
- **Shape:** 4px radius, 2px solid border, fixed 38px height
- **Primary** (`.btn`): ink background, paper text; hover flips to signal
  green (background + border)
- **Ghost** (`.btn.ghost`): paper background, ink text/border; hover
  inverts fully to ink background, paper text — the mirror of primary's
  hover, not a separate treatment

### Chips
- **Style** (`.chip`): signal-green background, paper text, uppercase,
  700 weight, 11px, 0.06em tracking, 4px radius — a single filter-state
  pill (e.g. the active company filter). Hover inverts to ink.

### Cards / Containers
- **Corner style:** square (0px) for a grid-tile card (metric/panel tile,
  supplied by the shared grid's own border, not the tile — see Shapes
  above for why these specifically stay sharp). A standalone floating
  box with its own border (job detail panel, `.ms-menu`, `.auth-panel`)
  rounds all four corners at 4px.
- **Background:** paper, laid on a black `--rule`-width gap grid (see
  Layout) — the grid supplies the border, not the card itself
- **Shadow strategy:** none — see Elevation & Depth
- **Internal padding:** 18px/16–20px depending on card type (metric tile
  vs. market panel)

### Inputs / Fields
- **Style** (`.filters input`, `.ms-toggle`): 1.5px solid ink border
  (every other input/toggle in the system uses `var(--rule)`'s 2px — see
  Shapes above), paper background, 38px height, 4px radius
- **Focus:** 2px signal-green outline, inset (`outline-offset: -2px`) so
  it reads as a border-color change rather than a halo
- **Multi-select** (`.ms-*`): a hand-built checkbox dropdown, not a
  native `<select multiple>` — active state turns the toggle's text and
  border signal-green and bold; overflowing labels ellipsize rather than
  wrapping or spilling past the box

### Navigation
- **Topbar:** Overused Grotesk wordmark, uppercase Helvetica nav links, sticky
  to viewport top, 2px ink bottom rule. `flex-wrap: nowrap` by design —
  the scrolling ticker between wordmark and status absorbs all the
  squeeze via `min-width: 0`, so the whole bar never wraps to multiple
  lines above the mobile breakpoint.
- **Mobile:** nav wraps and the ticker hides outright below 960px rather
  than trying to keep a marquee legible at phone width.

### Favicon
Rounded square (not the system's usual sharp corners — an OS/browser-chrome
artifact, not page UI, same reasoning a favicon always sits outside the
sharp-corner rule), fixed dark-mode `--green` (`#2fae60`, not the light-mode
`#609966`) background regardless of the page's own theme toggle — same
"can't respond to the page's own theme, so pick one and hold it" reasoning
as the footer's fixed-dark `curl` block. A bold off-white (`#f3f6e4`) "O",
Helvetica/Arial Bold, centered. `favicon.svg` is the source of truth;
`favicon-{16,32,180}.png` and `favicon.ico` are pre-rendered for browsers
that don't take an SVG icon.

### The Ticker (signature component)
A `News headline`-style scrolling marquee between the wordmark and the
online/offline status, seamlessly looping the board's own most-recent
matching listings (not a static sitewide list — it re-queries with
whatever filters are currently active). Built from CSS alone: the item
list is duplicated once in the DOM, animated `translateX(0)` to
`translateX(-50%)`, and pauses on hover.

### The Status Dot (signature component)
A square (not circular) block that "torch-flickers" — `steps(1)` timing
with hand-placed, irregular opacity keyframes, deliberately closer to a
Minecraft torch/redstone lamp than a smooth pulse — while the pipeline is
live. Goes offline-red with the animation killed outright (not still
pulsing) to read as "the light went out," not "warning, still breathing."

## Do's and Don'ts

### Do:
- **Do** use 4px radius on every discrete bordered box (see Shapes
  above); keep it at 0 only for the grid-tile cards, table, and dividers
  that stay sharp there — check with the user before adding a new sharp
  exception, not before rounding something new.
- **Do** build all depth/separation from the 2px black rule grid or a
  1px hairline — never a shadow.
- **Do** read every color from a `var(--token)`, never a literal hex, so
  retuning a palette only ever means editing the two `:root` blocks.
- **Do** keep green to exactly one "this matters most" element at a time.
- **Do** keep Overused Grotesk (display) scoped to the wordmark and the two section
  titles only — never at data-dense sizes.
- **Do** let `--gutter`'s `clamp()` drive side padding instead of a fixed
  `max-width` container, so the page keeps scaling at ultra-wide widths.
- **Do** give any element holding an unbreakable-width child (a curl
  command, a long label) `min-width: 0` — this codebase has hit the
  grid/flex default-overflow bug repeatedly and the fix is always this.

### Don't:
- **Don't** add a second accent color. Signal Green is the only one; a
  second dilutes what green means everywhere else.
- **Don't** use Alert Red outside the offline-status dot and the
  error-state banner — it has exactly two meanings today.
- **Don't** introduce a third typeface without folding it into the
  Two-Voice Rule as a named, scoped exception (see the flagged
  `.api-path`/`.param` monospace usage in Typography above) — an
  unscoped one-off is exactly how "two voices" quietly becomes three.
- **Don't** reach for `box-shadow` for elevation, ever, even subtly — the
  one prior violation (a smoothly-pulsing circular status dot) was
  treated as a bug and corrected, not kept as a soft exception.
