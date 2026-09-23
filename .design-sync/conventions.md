# OpenTechJobs design system

A job board for the Israeli tech market. Swiss grid, forest-green ink on warm off-white paper, and no decoration. There are no React components. The system is one stylesheet, `styles.css`, and you build with plain HTML and its classes. Copy markup from the preview cards, which use the same markup as the live board.

## Setup

Link `styles.css`. It loads its own fonts from `fonts/`. Don't add a reset or another font. Dark mode is `data-theme="dark"` on `<html>`, not `prefers-color-scheme`. The stylesheet ignores the OS setting.

## Rules the stylesheet assumes

- The page background is `var(--white)` and text is `var(--black)`. Both flip in dark mode, so never write a literal hex.
- Green marks exactly one thing that matters per view, like a fresh age, a salary range or the active filter tick. Never use it as decoration.
- A 4px radius goes on every discrete bordered box (buttons, chips, badges, inputs, floating panels). The table, grid tiles and dividers stay sharp. No box-shadow anywhere.
- There are two type voices. `var(--font)` (Helvetica) is for everything dense. `var(--font-display)` (Overused Grotesk, weight 700 to 800) is only for the topbar and section titles. `var(--font-ui)` (Source Sans 3) is for form controls and titles in the list.
- Rules are 1px `var(--grey-line)` hairlines. A structural edge is `var(--rule)` (2px) in `var(--black)`.

## Tokens

Colors: `--white`, `--black`, `--green`, `--green-text` (green text that passes contrast), `--green-bright`, `--grey`, `--grey-line`, `--hover-bg`, `--row-hover`, `--row-selected`, `--red`, `--logo-band`, `--scrim`. Layout: `--gutter` (page side padding), `--rule`.

## Class vocabulary

| Family | Classes |
|---|---|
| Buttons | `btn` (primary, solid ink), `btn ghost` (secondary), `btn-small`, `btn ghost btn-danger`, `btn btn-danger-solid`. Only one primary per view |
| Segmented control | `seg` with `seg-btn` children, `active` on the current one |
| Filter chips | `active-chips` holding `chip` > `chip-label` + `svg.chip-x` |
| Badges | `badge` (seniority label, the board adds a `seniority` hook with no styles), `badge best-effort`, `badge closed` |
| Row chips | `job-chip salary` (with `salary-est-label` "Est." for estimates, then `salary-range`), `job-chip skill` |
| Board layout | `board-shell` > `board-bar` (`board-bar-row`) + `board-grid` > `filter-rail`, `board-list`, `job-detail` |
| Job list | `table.jobs` rows: `star-cell`, `logo-cell` (`img.company-logo.listing`), `main-cell` (`job-card-title`, `job-meta` with `job-company`, `job-chips`), `age-cell` (`fresh` when 3 days or newer). Row states `selected`, `star-btn on` |
| Filter rail | `rail-group` > `h3.rail-title` (+ `rail-summary`) + `rail-body` > `label.rail-option` (`on`) with `rail-box`, `rail-option-label`, `rail-count`, then `rail-more` |
| Detail pane | `job-detail-close`, `job-detail-company`, `h2.job-detail-title`, `job-detail-actions` (`job-detail-apply`, `job-detail-star`), `job-detail-facts` > `fact` (`fact-label`, `fact-value`, `fact-absent`), `job-detail-section-title`, `job-detail-description` |
| States | `empty-state`, `error-state`, `skeleton` + `sk-line` |

The board grid is three columns above 1100px (rail, list, detail). Between 800px and 1100px the detail pane becomes a fixed sheet that opens with `.open`. Below 800px the rail is also a sheet. Below 640px table rows turn into cards, and that layout only applies to `tr[data-id]`, so every real row needs a `data-id`.

## Example

```html
<div class="board-list">
  <table class="jobs"><tbody>
    <tr data-id="1">
      <td class="star-cell"><button class="star-btn" aria-label="Save">...</button></td>
      <td class="logo-cell"><img class="company-logo listing" src="logo.png" alt=""></td>
      <td class="main-cell">
        <div class="job-card-title"><span class="job-title-text">Data Engineer</span> <span class="badge seniority">Senior</span></div>
        <div class="job-meta"><span class="job-company">Wix</span> · R&amp;D · Tel Aviv (Hybrid)</div>
        <div class="job-chips">
          <span class="job-chip salary"><span class="salary-est-label">Est.</span> <span class="salary-range">₪32k–40k</span></span>
          <button type="button" class="job-chip skill">Python</button>
        </div>
      </td>
      <td class="age-cell fresh">2d</td>
    </tr>
  </tbody></table>
</div>
```

Before you invent a class, read `styles.css`. Its comments give the reason for most rules.
