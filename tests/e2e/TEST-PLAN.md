# Board end-to-end test plan

Covers the job board's filter and search flow, the API contract behind
it, and how both behave when things go wrong. 77 tests, run against two
viewports.

## Running it

```
cd tests/e2e
npm install
npx playwright test                      # both viewports
npx playwright test --project=desktop    # one
BASE_URL=http://localhost:8000 npx playwright test
```

Production is the default target. Several assertions are about CDN and
cache behaviour that a local static server does not reproduce.

## Scope

| Layer | File | What it proves |
|---|---|---|
| Filters | `specs/filters.spec.ts` | Each control narrows, reaches the URL, survives a share-and-reload, and clears |
| Resilience | `specs/resilience.spec.ts` | Malformed input, network failure, dead sessions, blocked storage |
| Contract | `specs/api-contract.spec.ts` | The API alone, and that the board's count is the API's |

Deliberately out of scope: the alert flow past the sign-in wall, which
needs a Cognito account and would make this suite stateful; and the
Explore page, which has its own harness.

## Layers

### Edge cases and negative scenarios

Malformed URL parameters are table-driven in `fixtures/filter-cases.ts`:
non-numeric, negative and zero ages, unknown sort keys and directions,
negative and enormous offsets, repeated parameters, unknown parameters.
Each asserts the board still renders and that a value the app cannot
honour is **dropped rather than clamped**. Clamping `max_age_days=-5` to
30 would show results the URL never asked for.

Failure injection uses route interception rather than a proxy:

- the jobs request aborted mid-flight, and recovery once it returns
- a 500, which must be reported and must not render the empty state,
  since "no listings match" is a lie when nothing is known
- a four-second response, which must not render the empty state while
  in flight
- `localStorage` that throws on access, as in private mode
- corrupt saved filters
- an expired JWT, which must not break a board whose listings are public
- a 401 from the alerts route, which must not take the board with it

One of these is a regression test for a bug this suite found. See below.

### Data-driven inputs

`SEARCH_TERMS` covers ampersands, percent signs, quotes, plus signs,
hashes, slashes, Hebrew, an emoji, a 300-character string, a SQL
fragment and a script tag. Every term asserts three things: the box
keeps exactly what was typed, no error appears, and the term survives a
share-and-reload round trip. The script tag additionally asserts no
script element was created from it.

`FACETS` drives the five multiselects through one body. `AGE_OPTIONS`
drives the date window and asserts the totals grow monotonically, which
catches an off-by-one in the cutoff that a single spot check would not.

### Locators

Role and accessible name first, then the element's id where the app
already treats it as a contract, then `data-testid`. No CSS descendant
chains and no XPath, because the class names here are presentational and
move with the design system. The multiselects are custom widgets with no
native role, so those go through ids.

## What this found

**A bad link could lock a browser out of the board permanently.**
`?max_age_days=abc` reached the API, which answers 400, so the board
showed "Could not load jobs". But that filter is persisted across
sessions, so the junk was saved and every later visit to the plain board
reloaded it and failed again. The only escape was a Reset button inside
a panel nobody opens while looking at an error. Fixed by validating both
sources through one gate; two tests here are the regression, including
one that simulates a browser already holding a poisoned value and
asserts it heals unaided.

**`?sort=banana` threw an uncaught TypeError.** The header lookup
returned null and was used unguarded. It ran after the rows rendered, so
the board looked fine while the sort UI sat with nothing active.

**Company logos fail loudly.** A normal session logs 44 to 62 failed
asset requests, all of them rows whose company has no resolved logo
falling back to guessing an icon off the company's domain. Not a test
failure, so it is counted and printed rather than asserted, and it is
the measurement behind resolving logos server-side.

## Notes for whoever runs this next

Three failures in the first run were the harness, not the app, and all
three are worth knowing about:

- **Filters persist across sessions on purpose.** A spec that does not
  clear storage inherits the previous one's filters. `BoardPage.goto`
  clears by default.
- **The ticker calls the same route with `limit=10`.** Waiting on the
  jobs route alone resolves with the ticker's ten results while the
  table shows fifty. Predicates pin the page size.
- **Waiting for the page to look idle is not enough.** Between a click
  and the new request the previous count is still on screen and no
  skeleton has appeared, so a naive wait reads a stale number. This
  passed on desktop and failed under mobile emulation, producing the
  impossible claim that a 3-day window held fewer listings than a 1-day
  one. Use `BoardPage.applyFilter`, which waits for the board's own
  request for the new set.

Retries are off locally on purpose: a test that only passes on the
second run is a bug report, not a pass. CI gets one.
