---
name: OpenTechJobs
description: Swiss-grid job board for the Israeli tech market, ground-truthed data, zero decoration, forest-green ink on neutral paper.
colors:
  paper: "#f2f0ef"
  paper-fixed: "#f2f0ef"
  ink: "#40513b"
  signal-green: "#609966"
  muted-grey: "#40513b"
  hairline-grey: "#d2cfcb"
  alert-red: "#b8362c"
  hover-tint: "#e8e5e2"
  row-hover: "#e6e3df"
  row-selected: "#dce8d4"
  green-text: "#3f6f45"
typography:
  display-wordmark:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.02em"
  display-error-code:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "clamp(96px, 22vw, 160px)"
    fontWeight: 400
    lineHeight: 0.85
    letterSpacing: "-0.02em"
  display-hero:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "calc(100cqi / 6.1)"
    fontWeight: 500
    lineHeight: 0.84
    letterSpacing: "-0.04em"
  display-hero-ticker:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "clamp(52px, 9vw, 10rem)"
    fontWeight: 400
    lineHeight: 1.04
    letterSpacing: "-0.03em"
  display-feature:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "clamp(32px, 4.6vw, 4.5rem)"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-0.03em"
  display-section-title:
    fontFamily: "Overused Grotesk, \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "34px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "normal"
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
  ui:
    fontFamily: "\"Source Sans 3\", \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "normal"
  input-touch:
    fontFamily: "\"Source Sans 3\", \"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "normal"
  label:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "normal"
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

# Design System: OpenTechJobs

## Overview

**Creative North Star: "The Exchange Ticker"**

A market terminal, not a careers site: neutral paper, deep forest-green
ink, a single lighter green reserved for the one number or status that matters right
now, and a scrolling headline strip up top like a ticker tape. Everything
reads as a live instrument panel over a job market, not as a brand
experience. Density and legibility win over warmth every time there's a
conflict between them. The one indulgence is a single display face
(Overused Grotesk, weight 400), kept to two places (the wordmark and the
two section titles) so it reads as a signature, not a typeface choice
bleeding into body text.

Confirmed visual rejections: no box-shadow used for depth, no decorative
color, no third accent hue, no drop-in UI framework look. Every discrete
bordered box (buttons, chips, badges, inputs, floating panels) uses a
flat 4px radius. See Shapes below for exactly what stays sharp instead
(grid-tile cards, the table, dividers) and why.

**Key Characteristics:**
- Flat, two-tone (neutral paper/forest-green ink) surfaces with color used as signal, not decoration
- Depth built entirely from 2px hairline grids, never shadows
- One bold display face for brand moments; Helvetica for everything data-dense
- Light and dark are two deliberately tuned palettes, not a hex inversion
- A live-status vocabulary (the ticker, the status glyph) borrowed from terminals/dashboards and service-health pages, not marketing sites

## Colors

Two-tone by design, neutral paper, deep forest-green ink,
with exactly one accent color and one reserved alert color, both used
sparingly enough that their rarity is the signal. In light mode the
structure is neutral (paper, dividers, borders, hover tints) and green is
kept for signals: LIVE, active filters, Apply, salary badges, positive
metrics, links and chart emphasis. The structure used to be sage too,
which made the whole page read green before any green that meant
anything. Dark mode is a plain near-black/white inversion.

### Primary
- **Signal Green** (`#609966`, `--green`; dark mode `#2fae60`): the one
  color that means "this matters more than what's around it." Live status
  (online indicator), the accent letters in the wordmark, active
  filter/sort state, hover state on buttons and links, the single
  highlighted metric tile, the most-recent bar in the new-listings chart.
  Never used decoratively. If a green appears, it is pointing at
  something.
- **Green Text** (`#3f6f45`, `--green-text`; dark mode same as Signal
  Green): Signal Green wherever text is involved, meaning green text on
  paper and green fills with paper text on them (Apply, chips, the
  salary estimate). Signal Green itself is 2.96:1 on light paper, under
  the 4.5:1 text needs; this is 5.18:1. Bars, chart lines, focus rings
  and checkboxes have no text in them and keep `--green`.

### Neutral
- **Paper** (`#f2f0ef`, `--white`; dark mode `#17181c`): the base
  surface, a warm neutral, not pure white; dark mode is a tuned
  near-black, not pure black. Never applied as a "panel" color. Every
  ink surface (buttons, the metrics/panel grid background, the topbar
  hover) is a deliberate component, not the page.
- **Ink** (`#40513b`, `--black`; dark mode `#ededec`): body text, borders,
  and every "component" surface (buttons, cards' outer frame, the
  topbar), a deep forest green standing in for black, not a literal
  black anywhere in light mode. Dark mode's ink is a soft off-white to
  match, unrelated to this light-mode swap.
- **Muted Grey** (`#40513b`, `--grey`; dark mode `#9a9a9a`): secondary
  text, labels, metadata, placeholders, the offline-state wordmark.
  Light mode reads this at full ink darkness (the earlier softer tint
  wasn't legible enough); dark mode keeps its own true muted grey.
- **Hairline Grey** (`#d2cfcb`, `--grey-line`; dark mode `#2b2c31`): the
  quiet dividers, table row separators, bar-chart tracks, input borders
  one step down from a full 2px rule, and (reported live, too
  high-contrast at full `--black`) the Statistics grid's own gap/border
  color, `var(--rule)`'s 2px width kept, just recolored. Neutral in light
  mode, 1.37:1 on paper. It was the sage `#c7d9b3` (and before that
  `#dce8ce`, too faint to see), and a green tint on every divider and
  border was most of why the page read green.
- **Hover Tint** (`#e8e5e2`, `--hover-bg`; dark mode `#1e1f24`): the one
  soft, non-binary surface in the system, reserved for menu option hover
  states where a hard color flip would be too loud.
- **Row Hover** (`#e6e3df`, `--row-hover`; dark mode `#23252b`) and
  **Row Selected** (`#dce8d4`, `--row-selected`; dark mode `#1d3024`):
  job rows only. Hover, and keyboard focus inside a row, is neutral;
  the open listing's row carries the green tint. They used to share the
  Hover Tint, which at 1.04:1 against paper was barely visible and made
  hovering and selecting look the same.
- **Scrim** (`rgba(38,36,34,0.32)`, `--scrim`; dark mode
  `rgba(0,0,0,0.58)`): dims the board behind the job sheet,
  and nothing else. It gets its own token instead of reusing `--black`
  because it has to darken in both themes, and `--black` is a light color
  in dark mode. This is the one translucent value in the system, and it
  marks a surface as inert, not as raised. It is not elevation and it is
  not a shadow.

### Alert Red (reserved, not decorative)
- **Alert Red** (`#b8362c`, `--red`; dark mode `#ff6b5e`): errors and the
  controls that undo or remove something. Error messages and banners,
  the offline wordmark, sign-out and delete hovers, the danger button,
  and the filter bar's Reset, whose text is red on the ghost button's ink
  border (by request). Never decoration, never a status. Contrast is
  5.1:1 on light paper and 6.4:1 on the dark surface; the dark value is
  lifted because the light red was 3.0:1 there, too faint for text.

### Status Palette (pipeline state only)
Borrowed wholesale from AWS's service-health vocabulary rather than
invented, because the states are the same ones and a reader who has seen
a status page already knows what the shapes mean.

Deliberately outside the two-tone palette and outside the One Voice
Rule. Signal Green means "this matters more than what is around it,"
which is a claim about attention. These four mean "operational,"
"degraded," "disrupted," and nothing else, which is a claim about a
fact. Reusing `--green` and `--red` for both would make each of them
mean two things.

- **Operational** (`#248823`, `--aws-operational`; dark mode `#3fb950`)
- **Degraded** (`#ff9900`, `--aws-degraded`; dark mode `#ffab33`)
- **Disrupted** (`#d13212`, `--aws-outage`; dark mode `#ff5d47`)
- **Scheduled** (`#0073bb`, `--aws-maintenance`; dark mode `#539fe5`):
  unused so far, and defined anyway so a planned pause has somewhere to
  go rather than borrowing the warning colour.

Dark mode is lifted rather than inverted. AWS tunes these for a white
page and the green in particular reads closer to black than to a signal
on `#17181c`.

### Named Rules
**The One Voice Rule.** Green marks exactly one "this matters" element at
a time. If everything is green, nothing is. It never appears as
decoration, only on live status, primary metrics, and the handful of
buttons/links that actually do something.

**The Token Rule.** Light and dark mode are two independently tuned
palettes, not a hex inversion of each other, but every rule in the
system still reads `var(--white)`/`var(--black)` (never a literal hex),
so retuning either palette only ever means editing the two `:root`
blocks. A hardcoded `#fff`/`#000` anywhere breaks that.

## Typography

**Display Font:** Overused Grotesk, weight 400 (self-hosted, variable font spanning 300–900), falling back to Helvetica Neue / Helvetica / Arial
**Body Font:** Helvetica Neue, falling back to Helvetica, Arial, sans-serif
**UI Font:** Source Sans 3 (self-hosted, variable 200–900) for controls: buttons, inputs, selects, the view switch, chips and badges

**Character:** A dense, no-serif system voice everywhere text-heavy
(tables, filters, panels), broken only at brand moments by one bold,
grotesque-sans display face, never the reverse.

### Hierarchy
- **Display / Wordmark** (400, 15px, 0.02em tracking, Overused Grotesk): the
  topbar wordmark only ("OpenMarket.IL"). 13px below the 640px breakpoint.
- **Display / Section Title** (400, 34px, sentence case, 1 line-height,
  Overused Grotesk): page titles only. Inside the
  Statistics column it drops to 24px (1.1 line-height),
  because there it sits beside 13px filter controls and at 34px was the
  loudest thing on the page. Never used at table-row or data-dense sizes.
- **Display / Error Code** (400, 96px at phone width to 160px, 0.85
  line-height, Overused Grotesk): the 404 numeral only, the one oversized
  display moment. At least 2.8 times the section title under it at every
  width.
- **Title/Metric** (800, 30px, tabular-nums): the large number on a
  metric tile, the one place body copy gets genuinely large.
- **Subtitle** (700, 17px): the job-detail panel's title (`.job-detail-title`),
  needs to read as more prominent than table-row body text without
  competing with the two genuine display-face headings above it.
- **Row Title** (600, 15px): a job listing's title in the board table
  (`.job-card-title`). The one step between Body and Subtitle, and it
  exists because a row is scanned title-first: at Body size the title sat
  level with the company and location beneath it, so nothing in the row
  led. Below Subtitle deliberately, so opening a listing still promotes
  its title rather than repeating it at the same weight.
- **Body** (400, 13–14px): filters, table cells, panel prose, buttons,
  bar-chart labels.
- **Input / Touch** (400, 16px): every text field, select and textarea on
  a touch screen (`pointer: coarse`). Safari on iPhone zooms the whole
  page when a field under 16px takes focus and leaves it zoomed, so this
  is a floor set by the platform, not a type choice. Desktop fields keep
  the 13px Body/UI size.
- **Label** (700, 12px, sentence case, normal tracking): column
  headers, panel titles, the result count. Controls carrying label-style
  text (buttons, the view switch, chips) are 13px in Source Sans 3.

### Named Rules
**The Three-Voice Rule.** Helvetica (via `--font`) carries everything
dense. Source Sans 3 (via `--font-ui`) is for controls: buttons, inputs,
selects, the view switch, chips and badges. Overused Grotesk (via
`--font-display`) is reserved for the wordmark, section titles, the 404
numeral and the /hero page, which is set in it throughout. No fourth
typeface.

**No forced capitals.** Nothing is uppercased by CSS, and labels are
written in sentence case, status words included ("Live", "Degraded").
Uppercase with wide tracking read as shouting across a dense page, and
at 10 to 11px it was the smallest text on it. Where other sections of
this file still describe a label as uppercase or tracked, they predate
this rule.

### Open inconsistency (flagged, not fixed here)
`.api-path` and `.param` in the footer's API Reference (added this
session) use `"Courier New", monospace`, a third font-family the
top-of-file design-principles comment doesn't account for. It's a
narrow, defensible choice for literal code tokens, but it's a real drift
from the documented "two voices" rule as written. Worth a decision:
fold it into the Two-Voice Rule as a named third case, or replace it with
`--font` + a `.param` background treatment to keep the claim literally
true. Left for `/impeccable audit` / a deliberate call, not silently
changed here.

Now a second site, which makes the decision more pressing rather than
less: the Explore page's SQL editor and its results-table code cells use
the same `"Courier New", monospace`. An SQL editor is the one place a
proportional face would be wrong, because alignment is meaning, so this
is not a case that can be folded back into `--font`. The honest reading
of the system as built is two voices plus one utility face for literal
code, and the rule should say so. Until it does, both sites stay on the
same family so it remains one exception used twice, not two exceptions.

`footer code` (the API-Reference `curl` example) uses literal
`#0a0a0a`/`#2fae60`, not `var(--black)`/`var(--green)`, with its own
inline comment explaining why: a terminal/code block reads as an actual
terminal, so it stays fixed-dark regardless of the page's own light/dark
state, the same way a real code sample in a README doesn't re-theme
itself. A deliberate, narrow exception to the Token Rule below, not
drift, noted here so it doesn't get "fixed" back to a token by mistake.

## Layout

A permanent two-column workspace above 960px, Job Board (flexible,
`1fr`) beside Market Stats (fixed `clamp(360px, 28vw, 460px)`), never a
drawer or a toggle; they stack (stats below board) under 960px. No
`max-width` container anywhere: both the workspace and its inner
container use `--gutter` (`clamp(20px, 4vw, 64px)`) for side padding, so
the page keeps scaling with viewport width all the way to ultra-wide
instead of plateauing inside a fixed box.

The job detail panel is always a sheet over the board, never in the
page's flow. Above 960px it slides in from the right, as wide as the
Market Stats column plus 260px, so it covers the statistics and the
Saved and Reset filters; below 960px it is full-screen and
swipe-to-dismiss. Both dim the board with `--scrim` without locking it:
the wheel scrolls the board when the pointer is over the board and the
sheet when it is over the sheet. Above 1300px it used to be a sticky
column squeezed in beside the list, which left the description too
little room, and before that a stacked panel below the entire list,
which scrolled the reader to the footer. Being out of the flow is the
point: the page's height never changes, so there is no jump to correct.

The filter row stays a single line above the mobile breakpoint (flex
`nowrap`, matching the table's own width), shrinking each field rather
than wrapping to a second row; below 640px it wraps and every field
takes a full-width row instead. IL-only lives as a pinned first option
inside the Locations dropdown rather than as its own filter slot,
keeping the row to one line without dropping a filter.

Two dedicated card grids (metrics, market panels) share one motif: white
cards laid edge-to-edge on a black background with a `2px` (`--rule`)
gap, so the black shows through as a hairline grid between cards,
depth and separation from spacing and contrast alone, never a shadow.

## Elevation & Depth

Flat. Zero `box-shadow` anywhere in the system. The one historical
exception (a circular, smoothly-pulsing status dot with a soft glow) was
corrected specifically because the shadow and the easing were where the
system's own flatness rule got broken.

The status glyph that replaced it is drawn as a ring, which is not a
walking back of that rule. What was wrong was the shadow and the soft
pulse, not the curve: the ring is a flat outlined mark with no fill, no
shadow and no easing, and it is round because a tick inside a circle is
the shape a reader already recognises as a status mark.
All depth and grouping comes from the 2px black rule grid (metrics/panel
cards) and from solid borders, never from a shadow standing in for
elevation.

### Named Rules
**The No-Shadow Rule.** The black rules between cards are the only depth
cue this design uses. A shadow anywhere is a bug, not a style choice.

## Shapes

4px radius is the default for every discrete bordered box, buttons
(`.btn`, `.job-detail-apply`, `.job-detail-star`, `.job-detail-icon-btn`),
chips and badges (`.chip`, `.badge`, `.job-salary.estimate`), pagination
buttons, inputs/toggles (filter bar, alert-create form, auth email/social
buttons), and every floating panel/dropdown (`.job-detail`, `.ms-menu`,
`.auth-panel`, `.ms-search`/`.ms-clear` inside it), and the
empty/error/loading placeholder boxes. Borders otherwise stay one of
exactly two weights: `var(--rule)` (2px, solid, ink or accent-colored) or
a 1px hairline (`var(--grey-line)`, table row separators); the filter
bar's own inputs/toggles are the one exception at 1.5px, by request.

Sharp corners remain only where there's no real enclosed box to round:
the job table itself and its row separators (a hairline divider, not a
shape with corners), the topbar/section dividers, and the Statistics
grid's individual metric/panel tiles. They share one grid box already
rounded at its outer edge (see below), not individual boxes of their
own. Company logos and skill-chip text are images/plain text, not
bordered boxes, so nothing to round there either.

The Statistics grid (`#metrics-grid` + `#panel-grid`) keeps `var(--rule)`'s
2px width for its card-grid gaps/border but in `--grey-line`, not ink,
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
  inverts fully to ink background, paper text, the mirror of primary's
  hover, not a separate treatment

### Chips
- **Style** (`.chip`): signal-green background, paper text, uppercase,
  700 weight, 12px, 0.06em tracking, 4px radius, a single filter-state
  pill (e.g. the active company filter). Hover inverts to ink.

### View switch and match explanation
Show all, Best matches and Saved are one segmented control above the
result count, the same `.seg` control as the Explore page. Filters apply
in every view. Best matches narrows what they leave to listings that share
at least one CV skill, and the count says how many that is. In that view
a panel names the CV skills, folded under a "My skills" toggle with the
count and a chevron (folded by default, the choice kept per browser), each
skill removable once open. Every matching row says how many of them it
shares, which ones, and which of its own listed skills the CV lacks
(labelled "Missing skills"). Missing skills are plain comma-separated text at
70% opacity, faded behind the matched chips. Each of those is one line that
never wraps.
The matched line shows at most three skills, then a grey "+N more ›"
button that opens the line in place, wrapping, with "Show less" to fold it
back. When even three do not fit the row, skills are hidden from the end
until it fits, always keeping the first, and counted in the same button.
The missing-skills line has no cap, only that fit rule. Hidden names are
in the button's tooltip. Plain counts, no
percentage and no "AI" language: the ranking is a count of shared skill
tags and says so. The order counts recency as well: every two weeks since
posting costs one matched skill, so a fresh close match outranks a stale
slightly closer one. The row still shows the plain skill count. The
panel says "Ranked by relevance" and explains the rule in its tooltip.

### Cards / Containers
- **Corner style:** square (0px) for a grid-tile card (metric/panel tile,
  supplied by the shared grid's own border, not the tile. See Shapes
  above for why these specifically stay sharp). A standalone floating
  box with its own border (job detail panel, `.ms-menu`, `.auth-panel`)
  rounds all four corners at 4px.
- **Background:** paper, laid on a black `--rule`-width gap grid (see
  Layout). The grid supplies the border, not the card itself
- **Shadow strategy:** none. See Elevation & Depth
- **Internal padding:** 18px/16–20px depending on card type (metric tile
  vs. market panel)

### Inputs / Fields
- **Style** (`.filters input`, `.ms-toggle`): 1.5px solid ink border
  (every other input/toggle in the system uses `var(--rule)`'s 2px, see
  Shapes above), paper background, 38px height, 4px radius
- **Focus:** 2px signal-green outline, inset (`outline-offset: -2px`) so
  it reads as a border-color change rather than a halo
- **Multi-select** (`.ms-*`): a hand-built checkbox dropdown, not a
  native `<select multiple>`. Active state turns the toggle's text and
  border signal-green and bold; overflowing labels ellipsize rather than
  wrapping or spilling past the box

### Navigation
- **Topbar:** Overused Grotesk wordmark, uppercase Helvetica nav links, sticky
  to viewport top, 2px ink bottom rule. `flex-wrap: nowrap` by design,
  the scrolling ticker between wordmark and status absorbs all the
  squeeze via `min-width: 0`, so the whole bar never wraps to multiple
  lines above the mobile breakpoint.
- **Mobile:** nav wraps and the ticker hides outright below 960px rather
  than trying to keep a marquee legible at phone width.

### Favicon
Rounded square (not the system's usual sharp corners, an OS/browser-chrome
artifact, not page UI, same reasoning a favicon always sits outside the
sharp-corner rule), fixed dark-mode `--green` (`#2fae60`, not the light-mode
`#609966`) background regardless of the page's own theme toggle, same
"can't respond to the page's own theme, so pick one and hold it" reasoning
as the footer's fixed-dark `curl` block. A bold off-white (`#f3f6e4`) "O",
Helvetica/Arial Bold, centered. `favicon.svg` is the source of truth;
`favicon-{16,32,180}.png` and `favicon.ico` are pre-rendered for browsers
that don't take an SVG icon.

### The Ticker (signature component)
A `News headline`-style scrolling marquee between the wordmark and the
online/offline status, seamlessly looping the board's own most-recent
matching listings (not a static sitewide list, it re-queries with
whatever filters are currently active). Built from CSS alone: the item
list is duplicated once in the DOM, animated `translateX(0)` to
`translateX(-50%)`, over 70 seconds. It pauses on hover and on keyboard
focus, and sits at 80% opacity until the pointer or focus is on it, so it
stays ambient instead of being the first thing on the page anyone
notices. It keeps moving with reduced motion on: stopping it there left
the ticker frozen for anyone with Windows animation effects turned off,
and hover already gives every reader a way to pause it.

### The Status Glyph (signature component)
A 13px outlined mark in the topbar and a 24px one on the Data Health
tile, both drawn from one `<symbol>` sprite defined once per document
and referenced by `<use>`. One function paints both, so the two places
the state appears cannot drift apart.

Four states, not two. The snapshot is written every five minutes, so
one twelve minutes old has missed two cycles while still sitting inside
the twenty-minute threshold that decides whether the board claims to be
current at all. A binary indicator had nothing to say about that, which
is exactly where the interesting failures are.

- **Operational**, a tick in a ring
- **Degraded**, a bang in a ring, past two missed cycles
- **Disrupted**, a cross in a ring, past the freshness threshold
- **Updating**, a spinning ring with one quarter drawn solid, in ink
  rather than a status colour. Shown instead of Degraded or Disrupted
  while the merge has reported in within the last 10 minutes without an
  error, because then the data is late but on its way, and OFFLINE would
  scare a visitor over nothing. Capped at 45 minutes of old data, after
  which it is Disrupted regardless. With reduced motion it keeps turning
  at 2.4s a revolution, since a stopped spinner reads as a hung one.

**The shape carries the state, not the colour.** A tick, a bang, a
cross and a turning ring survive greyscale, a red-green colourblind reader, and the 13px
the topbar renders them at. Colour alone survives none of those, which
is the whole reason this is not still a coloured square.

Was a square block that "torch-flickered" on `steps(1)` timing with
hand-placed irregular opacity keyframes, closer to a Minecraft torch
than a smooth pulse, going offline-red with the animation killed
outright. The flicker was removed before this change, having read as
distracting rather than as character on something permanently on screen.
The stepped motion voice it belonged to still lives in the load bar's
own history (see Motion), and the square is gone: a ring reads as a
status mark where a square read as a decoration that happened to change
colour.

### Statistics column collapse
A toggle at the top of the Statistics column folds it away to the right
on desktop. The grid column eases (0.35s) from its normal width to a 62px
rail, and the column's content fades out. The rail holds a single 38px ‹
button whose top edge and height match the filter row's controls, with a
12px gap from the last of them. The choice is remembered per browser and applied
before first paint, so a returning visit does not animate. Below 960px
the column stacks under the board and has no toggle.

### Hero page (/hero, draft)
A landing page modelled on an editorial reference: a small utility row
(name, a comma-separated nav, GitHub), the OpenTechJobs wordmark set as
wide as the page (Display / Hero, sized from the hero's container width), which links back to the board, and below it a band in Green Text where the reference has a photograph,
running edge to edge of the screen with square corners. The band fits what it holds, with the same
green showing above and below. Its top line is a ticker of live board
numbers from `stats.json` in Display / Hero Ticker, drifting right over 60
seconds: global open jobs and how many are remote, separated by a drawn
dot. Under it, half that line's height, thirty company logos run left over
38 seconds, each on a tile of Paper Fixed with 4px corners: paper that
stays paper in dark mode, because logos are drawn for a light page. The
hand-picked big tech and startups in `api/hot_companies.py` come first,
busiest first, and the busiest other companies with a logo fill any
slots left. A logo that fails to load leaves the row. Either row can be
grabbed and thrown, with a mouse or a finger: it takes the speed and
direction of the throw, holds close to it, and eases back into its drift
over 2.6 seconds. Vertical swipes still scroll the page. Below the hero, the features as full-width
rows under a 2px rule, heading in Display / Feature beside a 16px
paragraph, each rising into place once as it scrolls into view on an
exponential ease-out; visible without script, and without that motion
when reduced motion is on, where the tickers slow to a third. It ends on
a large green "Search N open jobs" link, set at the Hero Ticker size. Overused Grotesk throughout,
colours from the board's own tokens, so it follows the theme.

### Not found
Any address the site does not have gets `404.html` with a real 404
status, served by the CloudFront function rather than S3's bare XML
AccessDenied. The 404 is the page's headline, in Overused Grotesk at
96 to 160px, above a 34px "Page not found". The heading
says what is missing when the address makes it clear (a listing under
`/jobs/`, a company under `/companies/`), one sentence says why, and the
page offers a search box, All listings, Best matches and Go back. A stale
link to a role usually still names it, so those words are pre-filled as
a search instead of guessing which current listing was meant. A `?job=`
link to a listing that has left the board gets the same message in the
job sheet. No illustration, no redirect, nothing that pretends to know
more than it does.

## Do's and Don'ts

### Do:
- **Do** use 4px radius on every discrete bordered box (see Shapes
  above); keep it at 0 only for the grid-tile cards, table, and dividers
  that stay sharp there. Check with the user before adding a new sharp
  exception, not before rounding something new.
- **Do** build all depth/separation from the 2px black rule grid or a
  1px hairline, never a shadow.
- **Do** read every color from a `var(--token)`, never a literal hex, so
  retuning a palette only ever means editing the two `:root` blocks.
- **Do** keep green to exactly one "this matters most" element at a time.
- **Do** keep Overused Grotesk (display) scoped to the wordmark and the two section
  titles only, never at data-dense sizes.
- **Do** let `--gutter`'s `clamp()` drive side padding instead of a fixed
  `max-width` container, so the page keeps scaling at ultra-wide widths.
- **Do** give any element holding an unbreakable-width child (a curl
  command, a long label) `min-width: 0`. This codebase has hit the
  grid/flex default-overflow bug repeatedly and the fix is always this.

### Don't:
- **Don't** add a second accent color. Signal Green is the only one; a
  second dilutes what green means everywhere else.
- **Don't** use Alert Red for anything but an error or a control that
  undoes or removes (Reset, delete, sign out). Not for emphasis, not for a
  status (that is the status palette).
- **Don't** introduce a third typeface without folding it into the
  Two-Voice Rule as a named, scoped exception (see the flagged
  `.api-path`/`.param` monospace usage in Typography above), an
  unscoped one-off is exactly how "two voices" quietly becomes three.
- **Don't** reach for `box-shadow` for elevation, ever, even subtly. The
  one prior violation (a glowing, smoothly-pulsing status dot) was
  treated as a bug and corrected, not kept as a soft exception. The
  status glyph is round now and still obeys this: no fill, no shadow, no
  easing.
