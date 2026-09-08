# amazon-scraper

[![release](https://img.shields.io/github/v/release/2scraper/amazon-scraper?sort=semver)](https://github.com/2scraper/amazon-scraper/releases)
[![tests](https://github.com/2scraper/amazon-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/amazon-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/amazon-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/amazon-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#what-works-with-nothing-at-all)

Scrapes Amazon search results, best-seller grids, product pages and the
reviews Amazon shows to visitors with no account — on any of its 21
marketplaces. JSON or CSV, one row schema per mode, and a run-metadata
sidecar that says whether the result is complete.

Four engines: **Playwright** (primary), **Selenium**, **pyppeteer**, or a
remote browser over CDP such as the **Scraping Browser API**.

---

## What works with nothing at all

The most useful thing this README can tell you is where the paid path is
unnecessary. Measured on **2026-09-08**, from a residential exit IP in
Europe, with a plain local Chromium — **no API key, no proxy, no account**:

```
$ python3 playwright_scraper.py \
    --url "https://www.amazon.com/s?k=bluetooth+headphones&i=electronics" \
    --pages 3

Parsed 30 rows from page 1.   Price coverage on page 1: 24/30 (80%).
Parsed 30 rows from page 2.   Price coverage on page 2: 22/30 (73%).
Parsed 30 rows from page 3.   Price coverage on page 3: 25/30 (83%).
Saved 77 products -> amazon_products.json        # 90 rows, 13 duplicates dropped
Wrote run metadata -> amazon_products.meta.json (status=complete)
$ echo $?
0
```

Amazon's front door met that run twice and neither time cost anything: an
**AWS WAF JavaScript challenge**, which a real browser solves by itself
(measured: 3.9 s on amazon.co.uk), and a **503 throttle** on amazon.de, which
a retry cleared.

So what do the 2Captcha products actually buy here?

| Product | What it is for on Amazon |
|---|---|
| **Proxies** (`--proxy-file`) | Volume. One address doing 50 pages an hour gets scored; twenty doing five each do not. Also the only way to choose an exit country. |
| **Scraping Browser API** (`--cdp-endpoint`) | A browser you do not run or patch, with a persistent profile and a chosen exit country. Also `Captcha.setAutoSolve` inside the browser. |
| **Captcha solving** (`--twocaptcha-key`) | One page: Amazon's own image captcha, "Enter the characters you see below". Not the WAF challenge, not a 503, not a sign-in wall. |
| **Fingerprints** (`--fingerprint`) | A consistent device identity across runs, matched to the exit country. |

---

## Install

```bash
git clone https://github.com/2scraper/amazon-scraper
cd amazon-scraper
pip install -r requirements.txt          # core: beautifulsoup4, requests

# then ONE engine
pip install -r requirements-playwright.txt && playwright install chromium
# or  pip install -r requirements-selenium.txt      (needs a local Chrome)
# or  pip install -r requirements-puppeteer.txt     (downloads its own Chromium)
```

**Install exactly one engine.** playwright and pyppeteer declare
mutually unsatisfiable pins (`pyee` <12 vs ≥13), and pyppeteer and selenium
collide on `urllib3` (<2.0 vs ≥2.6). All three do run side by side in
practice, because neither library touches the incompatible part — but
`pip check` reports the conflict and pip may resolve it by downgrading
something you wanted. Use a virtualenv per engine if you need more than one.

---

## Three modes

```bash
# 1. LISTING (default) — search results or a best-seller grid, paginated
python3 playwright_scraper.py --url "https://www.amazon.com/s?k=wireless+headphones" --pages 3
python3 playwright_scraper.py --url "https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/" --pages 2

# 2. PRODUCT — one /dp/{ASIN} page, with brand, seller, bullets and images
python3 playwright_scraper.py --mode product --url "https://www.amazon.com/dp/B07K5214NZ"

# 3. REVIEWS — the reviews Amazon renders to a visitor with no account
python3 playwright_scraper.py --mode reviews --url "https://www.amazon.com/dp/B07K5214NZ"
```

The marketplace comes from the URL's hostname. There is no `--marketplace`
flag, deliberately: one fewer way for a flag and a URL to disagree about
which site a run is reading.

### Why `--mode reviews` has no `--pages`

`/product-reviews/{ASIN}` — the paginated review history — **redirects to
`/ap/signin` on page 1**, not on page 2. An anonymous visitor cannot read it
at all. What this mode returns is the reviews rendered on the product page
itself: **13 per product** in every measurement here, complete with author,
rating, date, verified-purchase flag, helpful votes and body text. There is
no second page to fetch, so there is no flag to ask for one.

If you need the full history, you need an account, and this repo does not go
there: scraping from a logged-in session risks the account, and a canary
could not cover the path.

---

## Output

One row per product, same field order in JSON and CSV. The **first sixteen
columns are shared with every other scraper in this family**, so a consumer
written against one of them reads these unchanged; Amazon's own columns come
after.

| Column | Notes |
|---|---|
| `source` | The marketplace host the row came from: `amazon.com`, `amazon.co.jp`, … |
| `scraped_at` | UTC, ISO 8601 |
| `url` | Rebuilt as `https://www.{host}/dp/{ASIN}` — see [below](#the-url-column-is-rebuilt-not-read) |
| `sku` | The ASIN |
| `title`, `brand`, `price`, `currency`, `original_price`, `discount_pct` | `brand` is null in listing mode — see [the traps](#traps-that-look-like-bugs) |
| `rating`, `review_count`, `in_stock`, `image_url`, `category` | |
| `price_source` | `offscreen` / `split` / `detail` — which node the price was read from |
| `page`, `position` | Which listing page, and the position within it. On a best-seller grid `position` is Amazon's published rank (#1–#50) |
| `sponsored`, `badge`, `coupon` | Sponsored placements are flagged, not dropped |
| `seller`, `availability`, `bullets`, `images`, `variations` | `--mode product` only; null on a listing run |

`--mode reviews` writes a different schema: `sku`, `review_id`, `title`,
`rating`, `author`, `review_date`, `verified_purchase`, `helpful_votes`,
`variant`, `variant_asin`, `body`.

See [`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv) — both cut from a real run, not
written by hand. CI checks their columns against the schema.

### The run-metadata sidecar

Every successful run writes `<out>.meta.json` beside its output:

```json
{
  "source": "amazon.com",
  "mode": "listing",
  "status": "complete",
  "stop_reason": "completed",
  "pages_requested": 3,
  "pages_completed": 3,
  "pages_failed": [],
  "products": 77,
  "start_url": "https://www.amazon.com/s?k=bluetooth+headphones&i=electronics",
  "final_url": "https://www.amazon.com/s?k=bluetooth+headphones&i=electronics&page=3",
  "finished_at": "2026-09-08T13:23:23.751597+00:00"
}
```

`status` is the field to branch on: `complete`, `partial` or `failed`.
`pages_failed` names WHICH pages produced nothing, by number — a count stops
being a description once page 3 can fail while 4 and 5 succeed.

A **failed** run writes no sidecar at all, because it also does not overwrite
the previous run's output, and a `"failed"` sidecar sitting beside good data
would contradict it.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Rows written |
| 1 | Crash |
| 2 | Bad usage (including a credentialed `--cdp-endpoint` given to Selenium) |
| 3 | Blocked before parsing: Amazon's image captcha, an unresolved throttle, or a sign-in wall |
| 4 | Ran fine, found nothing |
| 5 | Remote API error |
| 6 | Partial: some pages gathered, then the run stopped early |

Exit 3 covers the sign-in wall as well as a captcha, because the family's
codes have one slot for "something stood between the run and the content".
The sidecar's `stop_reason` (`needs_signin` vs `blocked_amazon-captcha`)
keeps them apart for anyone who needs to know which it was.

### A run that finds nothing writes nothing

By default, zero rows means **no file is written** and the exit code is 4.
That is deliberate: a page-load failure that writes `[]` over last night's
good output destroys the last known good data, and a consumer cannot tell an
empty result from a failed run. Pass `--allow-empty` when an empty result is
genuinely the answer you want recorded.

---

## Marketplaces

All 21, selected by the URL you pass:

`amazon.com` · `co.uk` · `de` · `fr` · `it` · `es` · `nl` · `com.be` · `pl` ·
`se` · `co.jp` · `ca` · `com.au` · `in` · `com.br` · `com.mx` · `sg` · `ae` ·
`sa` · `com.tr` · `eg`

A hostname not in that table still parses; it only loses the currency hint
used to disambiguate a bare local symbol.

---

## Measured results

Everything below is a number from a real run on **2026-09-08**, from a
European residential exit IP. Nothing here is an aspiration.

| What | Measured |
|---|---|
| Search page, amazon.com | 16–30 organic tiles per page (it varies by query) |
| Search page, amazon.de | 16 tiles |
| Search page, amazon.co.jp | 60 tiles |
| Best-seller grid | 50 cards per page, ranks 1–100 across two pages, contiguous |
| Reviews on a product page | 13 |
| Price present, amazon.com search | 71–83% of rows (16/22, 24/30, 22/30, 25/30) |
| Price present, best-seller grid | 100% (50/50, 49/50) |
| `rating`, `review_count`, `image_url`, `title` | 100% of rows on every page measured |
| `brand`, listing mode | 0% — Amazon renders no brand field on a search tile |
| AWS WAF challenge clearing itself | 3.9 s |
| Pages of a 3-page run completed | 3 of 3, `status: complete`, exit 0 |

---

## Traps that look like bugs

Every one of these was hit during development. A reader who meets one
unwarned concludes the tool is broken.

### `brand` is null on every listing row

Amazon's search markup has no brand field. The brand is inside the title text
and nowhere else. Splitting the title on its first word would be wrong often
enough to be worse than a null, so the column stays empty in listing mode. It
**is** populated by `--mode product`, from the byline (`Visit the ZIHNIC
Store` → `ZIHNIC`).

### About a quarter of search rows have no price

On one measured page, 16 of 22 tiles carried a price and six did not — real
products (Sony WH-CH520, Bose QuietComfort) for which Amazon simply renders
no price to that visitor. `price`, `currency` and `price_source` are all null
on such a row, and the row is still kept. Each run logs its price coverage;
below 50% it warns, because that is low enough to suspect the snapshot rather
than the catalogue.

### The currency follows the exit IP, not the domain

Served to a visitor whose delivery country differs from the marketplace,
Amazon **converts** the price. From a European exit, amazon.co.uk and
amazon.co.jp both quote `EUR`. So:

* never derive the currency from the hostname — this scraper does not, and
  `currency` is null rather than guessed when the page does not say;
* **the converted price drifts with the exchange rate.** Two runs of the same
  command ten minutes apart disagreed on 10 of 43 shared ASINs, every one by
  the same relative amount (0.039–0.058%: `25.80 → 25.81`, `51.61 → 51.63`,
  `1117.53 → 1118.00`). Nothing about the offers changed; the rate ticked.
  `diff_runs.py --price-tolerance-pct 0.1` classifies those as FX noise
  instead of price changes. It defaults to 0 — report every cent — because a
  run against a marketplace in its own currency has no conversion in it.

### `--mode reviews` sometimes needs several tries

Amazon serves two variants of the same `/dp/` URL: one with the reviews in
the markup, one where the widget is present and empty. The variant is
**assigned per session** — a review-less browser context stayed review-less
across six consecutive reloads — and a fresh session re-rolls it, at a
measured hit rate of roughly 55%. So the engines start a new browser rather
than reloading, up to five times, which puts the chance of an empty result
under one in fifty. Scrolling does not help and is not tried: six scroll
passes left an empty widget empty.

Over `--cdp-endpoint` this cannot be fixed, because the remote profile's
cookies are not the client's to discard. Use a different `pid`.

### Best-seller grids lazy-load

A grid holds 30 cards on first render and 50 after scrolling to the bottom.
The engines scroll until the count stops growing, and then check the ranks:
because a chart publishes contiguous ranks, 30 rows spanning ranks 1–50 is
arithmetic proof that 20 cards never loaded, and the run says so.

### The `url` column is rebuilt, not read

A sponsored tile's link is a click tracker with the product path
percent-encoded inside it (`/sspa/click?...&url=%2F...%2Fdp%2FB0C3HCD34R...`),
so reading hrefs both misses sponsored products and stores trackers. Since
the ASIN is on the tile and `https://www.{host}/dp/{ASIN}` always resolves,
the canonical URL is rebuilt from it. That also keeps `url` stable between
runs, which Amazon's `qid`/`xpid`/`ref` parameters are not — otherwise every
row would read as changed on every run.

### Best-seller titles are sometimes lower-cased

`blink plus plan with monthly auto-renewal` is what the grid publishes. That
is the site's own text, not a normalisation this scraper applies.

### There is no `prime` column

Amazon rendered no Prime marker on any tile captured from a cross-border exit
(0 of 22 on .com, 0 of 16 on .de, 0 of 60 on .co.jp), so the column would
have been null on every row of every run. A field that looks available and
never is costs more than a missing one. If a domestic exit does render one,
open an issue with the capture and it goes back in.

---

## How it parses

**Amazon publishes no JSON-LD.** Zero `<script type="application/ld+json">`
blocks on search pages, best-seller grids or product pages. So unlike its
siblings in this family, this scraper's primary path is the site's own data
attributes:

| Page | Anchor |
|---|---|
| Search | `div[data-component-type="s-search-result"][data-asin]` |
| Best sellers | `[id^="p13n-asin-index-"]` |
| Reviews | `div[data-hook="reviewContainer"]` |

with a `/dp/{ASIN}` URL-pattern fallback that runs only if those yield
nothing. The fallback deliberately over-collects — a captured search page
carries 143 `/dp/` links for 22 organic products, the rest in carousels — so
it warns when it runs, and scopes each row to the outermost ancestor holding
exactly one product link.

Where a CSS class is unavoidable it is matched on a substring:
Amazon's own class names are partly build hashes
(`_cDEzb_p13n-sc-price_3mJ9Z`), and matching the whole name would break on
the next deploy.

Prices are read from `.a-price .a-offscreen` — the screen-reader copy — in
preference to anything visible, because `.a-price`'s own text is the price
concatenated with itself (`EUR 85.15EUR85.15`).

---

## Pagination and concurrency

Three layers, weakest signal last:

1. `link[rel="next"]` — the durable, standards-based form. **Amazon serves
   none** (checked on both listing kinds); it leads the list because it is
   the form that would survive a redesign.
2. `a.s-pagination-next` — a class Amazon can rename any day.
3. **The URL convention**, which is what actually carries pagination here:
   `?page=N` on `/s`, `?pg=N` on `/zgbs`. Note they differ; using one for the
   other silently re-fetches page 1.

And the terminating condition is **data, not markup**: a page that adds no
ASIN not already seen ends the run. That matters more on Amazon than
elsewhere, because `&page=400` keeps returning a valid page rather than a
404.

`--concurrency N` (Playwright engine only) fetches pages 2..N in parallel,
one browser and one proxy exit per worker. Page 1 is always fetched alone,
because its content is what decides whether the rest can be addressed
independently — and that is checked, not assumed: if page 1's own next-link
disagrees with what `?page=N` would build, the run falls back to following
links one page at a time and says so.

Concurrency is refused with `--cdp-endpoint` (one live connection per
profile) and warned about with no proxy pool (N workers from one address is a
faster way to get that address scored than to gather data).

---

## Engines

| | Playwright | Selenium | pyppeteer | Scraping Browser (CDP) |
|---|---|---|---|---|
| Primary | ✅ | | | |
| All three modes | ✅ | ✅ | ✅ | ✅ (via any engine's `--cdp-endpoint`) |
| `--concurrency` | ✅ | flag accepted, ignored | flag accepted, ignored | refused |
| Authenticated proxy | ✅ | ❌ | ✅ | n/a |
| Authenticated CDP endpoint | ✅ | ❌ | ✅ | — |

All three engines share the page-state policy (`page_flow.py`) and the
status/exit-code mapping (`output_writer.finish_run`), so they cannot drift
apart on what a page means or what a run reports. The offline suite asserts
they take the same flags.

Limits worth knowing before you pick one:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. Given one anyway, `selenium_scraper.py` exits 2 and says
  which engine to use instead.
* **Selenium cannot authenticate a proxy at all.** Credentials are stripped
  and a warning says so, rather than letting you believe a `user:pass` URL is
  doing something.
* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. On an Apple Silicon Mac it also downloads an x86_64 Chromium
  117 that runs far enough under Rosetta to print `--version` and then fails
  to open its DevTools socket — so `--chromium-path` exists to point it at a
  browser that works.

Credentials never reach a command line. `--proxy-server=` becomes part of the
browser's argv, readable by anything that can run `ps`, so the address goes
there and the password goes through the driver's own channel.

---

## Comparing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json --fail-on-change
```

Reports added, removed and changed ASINs, and refuses to compare runs that
are not both `complete` — a run cut short on page 3 of 10 is missing every
product on pages 4–10, and diffing it against a full run reports all of them
as delisted. `--force` overrides. It also refuses two runs of different
modes, and a reviews run outright (many rows per ASIN, no price).

A price change that comes with a `price_source` change is reported as
`source_changed`, not `changed`, and is ignored by `--fail-on-change`: that
says something about our own two snapshots, not about Amazon. Same for
`--price-tolerance-pct` moves.

---

## Testing

```bash
python3 smoke_test.py     # 228 offline checks, no network, no browser
pytest                    # the same suite, as one test
python3 env_config.py     # what configuration was picked up (prints no secrets)
```

The offline suite passes with no engine library installed at all, and
**reports** the groups it skipped. CI's `engine-smoke` job installs all three
and fails if anything reports skipped, because "skipped, engine absent" reads
identically to a passing run.

Its fixtures are real captures with `<script>`/`<style>` stripped and nothing
else changed — the parser produces identical rows from the trimmed and
untrimmed forms, which is checked before they are committed. Exactly one
fixture is not a capture: Amazon's image captcha page, written from its
documented markup because no run during development was ever served one. That
is stated in the file, and it means the captcha **solve** path is unproven
against the live site.

Two workflows: `tests.yml` is offline only and never touches amazon.com or a
credential; `canary.yml` runs one real 3-page listing daily and asserts
`pages_completed`, `status == "complete"`, a product floor and a price-coverage
floor. Three pages, not one, because with one page pagination is never
exercised at all.

---

## Contributing, security, licence

* [CONTRIBUTING.md](CONTRIBUTING.md) — how to report that Amazon changed its
  markup, and what a fix needs to include.
* [SECURITY.md](SECURITY.md) — how to report a vulnerability. Note that
  "Amazon changed its markup" and "the scraper is blocked from a datacenter
  IP" are not vulnerabilities.
* [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — the failure modes, by symptom.
* MIT — see [LICENSE](LICENSE).

This repo reads **public pages**. It does not log in, does not touch a cart or
an order, and does not attempt to defeat Amazon's protections: the AWS WAF
challenge is waited out exactly as a browser would, and the only thing that
gets solved is a captcha you pay 2Captcha to read. Respect Amazon's terms and
the law where you operate; scraping at volume from one address will get that
address blocked, which is the system working as designed.
