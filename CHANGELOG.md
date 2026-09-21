# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as closely as a
CLI toolkit can. Read a **patch** as "fixes" rather than as a promise that
every flag and default is frozen: a default that changes behaviour can ship in
one, and when it does the release notes say so first.

## [Unreleased]

> **Two changes here affect an existing user before anything else does.**
>
> **A run that never got its page now exits 5, not 4.** Exit 4 has always
> meant "ran fine, found nothing", and a navigation timeout or a dead proxy
> returned it too — so a pipeline could not tell an empty search from a
> transport failure, which want opposite responses. If you branch on exit
> codes, 5 is now also "we never obtained the page" and not only "the
> Scraper API errored". A run that gathered rows and THEN timed out is still
> exit 6, unchanged.
>
> **The `variations` column is gone**, replaced by `parent_asin`,
> `variation_dimensions`, `variation_count` and `selected_variation`. It read
> the rendered size/colour picker and was null on every row of every run —
> measured on three live `/dp/` pages on 2026-09-21, where both of its
> selectors matched zero elements on all three. If you parse `--mode product`
> output by column name, read the four new ones; if you read it positionally,
> the row is four columns longer.

### Changed

- **A page that was never fetched reports `EXIT_FETCH_FAILED` (5) instead of
  `EXIT_NO_PRODUCTS` (4)** when the run holds no rows. A live run through a
  misconfigured proxy failed to load for 60 seconds and exited 4, telling a
  caller the catalogue had been read and was bare. The precedence is
  unchanged otherwise: a named challenge still outranks it (3), and rows plus
  a later failure is still a partial run (6). 5 rather than a new code
  because the family's contract already reserves it for a transport failure,
  every repo in the family spells 4 the same way and none defines a 7 — a new
  code here would make this the only one whose callers need a per-repo table.
- **The sidecar counts `records` and says what one record is.** A reviews run
  recorded `"products": 13` for thirteen reviews of one product, which reads
  as thirteen products to anything summing the field. `records` is the row
  count and `record_type` is `product` or `review`, derived from the row
  class so a new mode cannot forget to declare it. `products` is still
  written and still identical, but is deprecated — read `records`.
- **`save()` names what it saved.** "Saved 13 products" for thirteen reviews
  was the same wrong claim in the place a human reads; a single row is now
  "1 product" rather than "1 products".
- **Out-of-range numeric arguments are refused** (exit 2) rather than acted
  on. `--retries 0` was the worst: the navigation loop is
  `range(1, retries + 1)`, so it never ran, and because `load_failed` is
  pre-set to False just above it nothing was recorded as wrong either — the
  run classified a page it had never fetched and reported success. `--pages 0`
  fetched page 1 anyway (it is fetched outside the page loop) and produced a
  sidecar saying `pages_requested: 0` against `pages_completed: 1`. A negative
  delay reached `time.sleep` and became a traceback. The bounds live in one
  shared, pure helper, and all three engines consult it, so they cannot
  disagree about the same input. `--concurrency` was NOT one of these: the
  engine that implements it already clamped it.
- **The Scraper API client goes through the shared `finish_run`.** It called
  `save` and hand-spelled its own 3 and 4, so a run through it wrote no
  run-metadata sidecar at all — the rows were readable but the status, stop
  reason and marketplace were not. It is still not a fourth engine: one page,
  no pagination, and success reports `single_page_mode`. `EXIT_API_ERROR` is
  now an alias of the shared constant rather than a second spelling of 5.

### Removed

- **The `variations` column.** A column that is null on every row of every
  run is worse than a missing one — the same rule that removed `prime` from
  this schema, with the measurement written down. Both of its selectors
  (`#twister .a-button-text`, `#twisterContainer .a-button-text`) matched
  zero elements on three live `/dp/` pages on 2026-09-21. Replaced, not
  repaired: see Added.

### Added

- **Four variation columns on a `--mode product` row**, read from the detail
  page's own variation state rather than from the rendered picker:
  `parent_asin`, `variation_dimensions` (the dimensions as the site labels
  them for a human, in the site's own order), `variation_count` (the number
  the **page itself states**, so it can be compared against what was
  extracted — the parser warns when they disagree) and `selected_variation`
  (which variant this row is). Measured 2026-09-21: 1, 776 and 9 variants on
  the three pages tested, matching the site's stated totals exactly.
  The container that replaced the old one is an empty mount point in the
  served HTML, so it is only populated after the page's own JavaScript runs;
  the state object is in the served bytes either way, which is why it is what
  gets read. Scalars rather than the whole variant table because one measured
  product publishes 776 variants, and 776 objects in a CSV cell is not a
  column anyone can use.
- **`<out>.last_attempt.json`, written on every run.** A failed run
  deliberately writes no `<out>.meta.json` — it also does not overwrite the
  previous run's output, and a `"failed"` sidecar beside good data would
  contradict it — which until now left the caller nothing at all to read, so
  the exit code was the whole story. A separate filename lets the last good
  sidecar and the most recent attempt both be true. Rewritten on success too,
  so it is never a stale relic of an old failure.
- **A check that binds every call into a shared module against the callee's
  real signature** (§17's check #1), which this repo did not have. It catches
  two things nothing else here can: a call whose arguments do not fit the
  signature, and a call to a name the shared module does not define at all —
  both of which reach a live run as a crash on the first fetch while import,
  `--help`, `compileall` and the undefined-name walk all stay green. It skips
  calls using `*args`/`**kwargs` rather than guessing, treats a locally-bound
  name as shadowing a same-named module, and asserts it found calls to bind
  at all so it cannot pass by scanning nothing. Verified by control.
- **The first variation fixture this repo has had** — 1.5 KB carved verbatim
  out of a 992 KB live capture and verified to produce byte-identical columns
  to the untrimmed page before being committed. There was no twister fixture
  at all, which is how the selectors could die unnoticed. Its values are
  pinned, not merely counted.

### Fixed

- **The Scraper API's `x-debug` response header is redacted before it is
  logged.** `SECURITY.md` names that header as one of three places
  credentials reach a log unmasked, and the client logged it whole: the API
  echoes back the task it ran, so a run driven through a credentialed CDP
  endpoint put that endpoint's username and password into the log. Both
  patterns are global, because a masker that handles the first occurrence
  prints the password the other four times and looks like it is working.
- **`landingAsin` and `parentAsin` are read as an adjacent pair.**
  `parentAsin` also occurs about 11 KB earlier on a detail page, in an
  unrelated object, so reading the first occurrence of each was correct on
  all three measured pages by luck rather than by structure.

### CI

- **A check that binds every call into a shared module against the callee's
  real signature** (§17's check #1), which this repo did not have. It catches
  two things nothing else here can: a call whose arguments do not fit the
  signature, and a call to a name the shared module does not define at all —
  both of which reach a live run as a crash on the first fetch while import,
  `--help`, `compileall` and the undefined-name walk all stay green. It skips
  calls using `*args`/`**kwargs` rather than guessing, treats a locally-bound
  name as shadowing a same-named module, and asserts it found calls to bind
  at all so it cannot pass by scanning nothing. Verified by control.

### CI

- **A variation canary**, reading one real multi-variant product page and
  asserting all four columns are populated, that the count is not 1 on a
  product picked for having many, and that there is one selected value per
  dimension. A floor rather than the measured 776, because a catalogue
  legitimately changes size. **Dispatch-only until someone runs it from the
  default branch and sees which way it goes**: a `/dp/` page was served to a
  plain HTTP client from a datacenter address on 2026-09-21, which is a
  reason to expect it to pass from a runner and not evidence that it does,
  and an unverified live job on a schedule is how a badge goes permanently
  red. The promotion steps are written beside it.
- **The Python embedded in the workflows' heredocs is compiled by the offline
  suite.** Both canary jobs assert their results with inline Python, and
  nothing checked those bytes until a runner executed them — a syntax error
  there costs a full dispatch to discover and, on the scheduled job, reads as
  a site-side failure rather than a typo. Four blocks across the two
  workflows. Done by dedent rather than by parsing YAML, because PyYAML is
  not a dependency of this project.
- **The Docker image is now built in CI.** Nothing built it before, which is
  exactly how three repos in this family shipped an image that died with
  `ModuleNotFoundError` on every invocation, `--help` included — the
  Dockerfile COPYs an explicit list, which is right, and the list fell behind
  the imports. Four steps, each for a distinct failure: the image builds; its
  entrypoint runs (the `CMD` is `--help`, the invocation a missing module
  breaks); Chromium really launches, rather than the `playwright install
  --with-deps` layer merely exiting 0; and the image contains no `.env`, no
  test suite and no fixtures, because a `.env` baked into an image is a
  credential published to everyone who can pull it.
- **`claude.yml` pins the CLI to the `stable` channel.** The fix had already
  landed in `claude-code-review.yml` in this repo and never reached its twin,
  so `claude.yml` still let the action install `latest` — the release that on
  2026-09-08 installed no binary where the action looks, failing every run
  with "Claude Code native binary not found". The channel is pinned rather
  than a version, so a fixed upstream release needs no edit here.

## [0.1.3] — 2026-09-11

### Fixed

- **`fingerprint_client.py` could not read the key from `.env`.** `--key`
  defaulted to `os.environ.get("TWOCAPTCHA_KEY")` and only that, so a key put
  in `.env` — exactly as §3, the README and `.env.example` instruct — worked
  for every engine and failed HERE with "No API key". A documented mechanism
  not applied on one path, which is the shape of half the defects §16 lists.

  It now reads through `env_config.env_value`, calling `load_env()` itself
  because this is a standalone entry point that no engine has necessarily run
  first. Going through the loader rather than `os.environ` is measured rather
  than stylistic: with `TWOCAPTCHA_KEY=your_2captcha_api_key_here` exported,
  the old path sent the placeholder to the API and reported "Fingerprint API
  rejected the key (401) — note this is a separate subscription", sending the
  reader off to check a subscription they never needed; the loader says
  "still set to the placeholder from .env.example" instead.

  Found on a sibling repo's first live `--fingerprint` run, then checked
  across the family before patching, per §16: five repos had it and one had
  already fixed it. Pinned by a check verified to fail on the old code —
  including that the help string does not interpolate its default, which is
  one substring away from printing a live credential to anyone who types
  `--help`.

---

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

[0.1.3]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.3
[0.1.2]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.2
[0.1.1]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.1
[0.1.0]: https://github.com/2scraper/amazon-scraper/releases/tag/v0.1.0
