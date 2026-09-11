# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as closely as a
CLI toolkit can. Read a **patch** as "fixes" rather than as a promise that
every flag and default is frozen: a default that changes behaviour can ship in
one, and when it does the release notes say so first.

## [0.1.2] — 2026-09-11

### Fixed

- **`--fingerprint` dropped `deviceScaleFactor`, so the identity
  contradicted itself.** `playwright_context_kwargs` mapped the user agent,
  the locale, the timezone and the screen onto the browser context and
  ignored the scale factor the fingerprint API returns beside them. Measured
  2026-09-11 against the live API and a live browser: a fingerprint stating
  `deviceScaleFactor: 1.25` produced a browser reporting
  `window.devicePixelRatio === 1` — the paid identity saying one thing and
  the browser another, on every run, silently, on an axis any fingerprinter
  reads for free. Playwright takes it as its own context option, so the fix
  is to pass it; verified in a live browser both ways and pinned in the
  offline suite.

- **This repo had no check for fingerprint APPLICATION at all**, which is how
  the defect above survived here. The new test covers the user agent, the
  locale, the timezone and the viewport as well as the scale factor.

  Found while auditing a new sibling repo against the family notes. All five
  repos in this family had it.

---

## [0.1.1] — 2026-09-09

Everything here landed after `v0.1.0` was tagged, so the 0.1.0 artifact does
not contain it. Nothing changes for a caller: no flag, column, exit code or
output shape moved.

### Fixed

- **The fixtures carried session material and one real person's data.** Real
  page captures bring the session that fetched them (three anonymous, expired
  `sessionId` values and their CSRF tokens) and, on a review, a real
  customer's display name, profile permalink, review id, photo ids and words.
  All of it is now replaced with obvious placeholders — the checks read the
  structure of a review, not the person — and a new guard fails the suite if
  any of it arrives with a future capture. Found while reviewing what going
  public would actually expose.
- **Both Claude workflows failed instead of skipping when their token is
  absent.** They were inherited from the sibling repo, where
  `CLAUDE_CODE_OAUTH_TOKEN` is a repo secret; unset here, the action's own
  environment validation failed the job and put a permanently red check on
  every pull request — and a check that is always red teaches everyone to
  ignore checks. Setting the secret turns the review back on with no further
  change.

## [0.1.0] — 2026-09-08

First release. The repository previously held a different, unrelated Amazon
scraper (a `requests` + `lxml` client with its own output format); this
version replaces it entirely with the architecture used across this family of
scrapers. **Nothing from the previous code is source- or output-compatible.**

### Added

- **Three modes.** `--mode listing` (default) reads paginated search results
  and best-seller grids; `--mode product` reads one `/dp/{ASIN}` page with
  brand, seller, bullets, images and availability; `--mode reviews` reads the
  reviews Amazon renders to a visitor with no account.
- **All 21 marketplaces**, selected by the hostname of `--url`. There is no
  `--marketplace` flag, so a flag and a URL cannot disagree.
- **Four engines**: `playwright_scraper.py` (primary), `selenium_scraper.py`,
  `puppeteer_scraper.py`, and `scraper_api_client.py` (browserless, via the
  2Captcha Scraper API). All of them agree on exit codes and run status.
- **The family's output contract**: the `Product` row schema with the shared
  sixteen columns first, JSON and CSV in the same field order, a
  `<out>.meta.json` run sidecar, and exit codes 0/1/2/3/4/5/6.
- **`page_flow.py`**, a shared page-state policy, so the three engines cannot
  drift on what a page means. Amazon answers a request in five ways and four
  of them want different responses:
  - the **AWS WAF JavaScript challenge** (HTTP 202) is waited out, not
    reported and not solved — a browser clears it in about four seconds, and
    handing it to a captcha solver would bill for nothing;
  - the **503 throttle** is retried, then answered with a different exit;
  - Amazon's **image captcha** is the only path a 2Captcha key is for;
  - a **sign-in redirect** stops the run, because no proxy or solve changes
    it.
- **`--concurrency N`** in the Playwright engine, one browser and one exit per
  worker, with page 1 always fetched alone.
- **`--price-tolerance-pct`** in `diff_runs.py`, for runs whose exit country
  differs from the marketplace: Amazon converts the price and the rate drifts,
  measured at 0.04% over ten minutes on 10 of 43 ASINs. Defaults to 0, so
  every cent is reported unless you ask otherwise.
- **`--chromium-path`** in the pyppeteer engine, for platforms where its own
  bundled Chromium will not start (an x86_64 build under Rosetta on Apple
  Silicon).
- **A `page` column** beside `position`, because Amazon's `data-index`
  restarts on every page and position alone is ambiguous across a paginated
  run.
- 244 offline checks in `smoke_test.py`, with fixtures cut from real captures;
  a daily canary against a real 3-page listing; `tests.yml` on Python 3.9 and
  3.12 with an `engine-smoke` job that fails on any unexpected skip.

### Fixed before release

- **`engine-smoke` was green against a stub.** The job installed the three
  engines unpinned into one environment, and pip resolved their mutually
  unsatisfiable pins by reaching for **pyppeteer 0.0.25** — not the `>=1.0.2`
  `requirements-puppeteer.txt` asks for. It now installs each engine from its
  own requirements file into its own venv, which is both what the README tells
  users to do and the only way to get the versions they get; `pip check` runs
  per venv as well.
- **The pyppeteer import was inside the launch path**, so `puppeteer_scraper`
  imported cleanly with no pyppeteer installed at all. That made the offline
  suite's skip reporting — and therefore CI's "fail on any skip" guard —
  vacuous for that engine, and is why the stub above went unnoticed. The
  import is now at module level like its two siblings, and a new check asserts
  all three stay that way.

### Notes on what this does NOT do

- **No JSON-LD parsing**, because Amazon publishes none — zero
  `application/ld+json` blocks on any page kind measured. Rows come from the
  site's own `data-asin` attributes, with a `/dp/{ASIN}` URL-pattern fallback.
- **No `brand` on listing rows.** Amazon's search markup has no brand field;
  guessing from the title would be wrong often enough to be worse than a null.
  `--mode product` does populate it.
- **No `prime` column.** No Prime marker appeared on any tile captured from a
  cross-border exit, so the column would have been null on every row.
- **No `--pages` for `--mode reviews`.** `/product-reviews/{ASIN}` redirects
  to `/ap/signin` on page 1, so the paginated history is not available to an
  anonymous visitor at all. What this mode returns is the ~13 reviews the
  product page itself renders.
- **No logged-in scraping.** Reviews beyond those, and order or cart data, are
  out of scope.
- **The image-captcha solve path is unproven against the live site.** No run
  during development was served one; it is exercised only by an offline
  fixture written from Amazon's documented markup.

[0.1.2]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.2
[0.1.1]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.1
[0.1.0]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.0
