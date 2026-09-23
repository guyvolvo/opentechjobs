# Design sync notes

- The frontend has no component package, so the automated converter doesn't apply. `node .design-sync/build.mjs` builds `ds-bundle/` from `frontend/style.css`, `frontend/fonts/` and the cards defined in the script. Upload `ds-bundle/` as-is.
- Cards are hand-copied from the markup `app.js` and `board.html` render. When a renderer changes (`renderJobRows`, `renderJobDetailBody`, `railOptionHtml`, `jobSalaryChip`), update the matching card in `build.mjs`.
- Check cards before uploading: `cd tests/e2e && node ds_cards_shot.mjs <out-dir>`. It screenshots every card, plus the job row in dark mode and at 390px, and reports failed requests.
- There's no `_ds_sync.json`. The anchor recipe needs a component bundle, so every sync re-verifies all 8 cards. At this size that takes minutes.
- `.btn-quiet` only has styles inside `.alert-create` and `.geo-prompt`. `.seniority` on badges is a JS hook with no CSS. Keep both out of the conventions.
- The row title is `600 15px var(--font)` (Helvetica). On Windows it falls back to Arial, so a card screenshot won't match a Mac capture of the live site.
- `styles.css` references `/img/hills.webp` for the landing hero. It isn't uploaded, so that background won't load in Claude Design.
