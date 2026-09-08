# Troubleshooting

Ordered by how often each one actually happens.

## 0 products, exit 4

Nothing was written, so a previous good output file is still intact. The engine
also dumped `{out}_page1_debug.html` and `.png` next to itself — open them, they
answer this in one look.

| What the dump shows | Cause |
|---|---|
| A challenge or "verify you are human" page | Bot management. Use a browser engine (not a plain HTTP fetch), a residential IP, or a remote browser via `--cdp-endpoint`. |
| A real page, prices visible, still 0 rows | The JSON-LD path found nothing and the CSS fallback did not match. Check that product links still match `-item-<digits>.aspx`. |
| A real page in a different language, prices like `125 €` | Fine — that parses. If rows are still 0, it is not the locale. |
| A near-empty page | The hub URL. `/shopping/kids/items.aspx` has zero products in its JSON-LD; use a filtered category URL. |

## A local Selenium session will not start

Symptom: nothing happens for the whole `--driver-timeout`, then a message. The
timeout is a ceiling, not a diagnosis — the cause is almost always one of four
things, in this order.

**1. No browser on the machine.** chromedriver does not attach to a browser
locally, it *launches* one, and when there is none where it looks it waits rather
than reporting. Any Chromium-family build works:

```bash
ls -d /Applications/*.app ~/Applications/*.app 2>/dev/null | grep -i 'chrom\|brave\|edge\|arc'
mdfind -name 'Chrome.app' | head          # macOS

python3 selenium_scraper.py --url "$URL" \
  --chrome-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

No Chrome at all? Playwright's bundled Chromium is a real browser on disk:

```bash
python3 -c "from playwright.sync_api import sync_playwright as p; s=p().start(); print(s.chromium.executable_path)"
```

**2. Driver and browser majors differ.** chromedriver drives that exact binary,
so the majors must match. The scraper compares them before spending the timeout
and stops with both numbers. Get a matching driver from
[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/), or
pass `--disable-build-check` to try anyway — if the protocol really differs you
get an error naming the incompatibility, which beats a refusal to start.

**3. macOS quarantine.** A downloaded driver can be blocked with no visible
error: `xattr -d com.apple.quarantine <chromedriver>`.

**4. A stale chromedriver** from an earlier run still holding its port:
`pkill -f chromedriver`.

To see chromedriver's own words instead of our timeout, cut Selenium out:

```bash
<chromedriver> --port=9515 --verbose &
curl -s -X POST localhost:9515/session \
  -d '{"capabilities":{"alwaysMatch":{"browserName":"chrome"}}}'
```

Session creation legitimately takes ~50s when launching a fresh profile, so the
local budget defaults to 150s (60s with `--cdp-endpoint`, where waiting longer
is pointless).

## pyppeteer: "Browser closed unexpectedly"

pyppeteer downloads its own Chromium, and on an Apple Silicon Mac it downloads
an **x86_64 build of Chromium 117**. That binary runs far enough under Rosetta
to answer `--version` and then fails to open its DevTools socket, so every
launch dies with `BrowserError: Browser closed unexpectedly`. Reproduced with
no wrapper code at all — a three-line `await launch()` script fails the same
way — so it is the build, not this engine.

Point it at a browser that works:

```bash
python3 puppeteer_scraper.py --chromium-path \
  "$HOME/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
  --url "https://www.amazon.com/s?k=wireless+headphones"
```

Any Chrome or Chromium will do. And note pyppeteer is effectively
unmaintained; its own README points at Playwright.

## Selenium cannot reach an authenticated remote CDP endpoint

Not fixable at this layer, and worth understanding rather than retrying.
chromedriver implements `debuggerAddress` as exactly three steps: `GET
/json/version`, read `webSocketDebuggerUrl`, open that WebSocket. None of them
carries credentials, and there is no option to add them. Playwright and Puppeteer
take a whole `ws://user:pass@host:port` and authenticate on the upgrade.

Use `playwright_scraper.py` or `puppeteer_scraper.py` for a remote endpoint.
Selenium is fine against a browser it launches itself, or a local
`chrome --remote-debugging-port=9222`, where no auth is involved.

## Remote browser: `profile_locked`, `proxy_timeout`, HTTP 500

One live CDP connection per profile. A second connection to the same `-pid-` is
refused. Either wait for the first to finish, or use a different pid for parallel
work.

If a **brand-new** pid is also refused, the account may be out of profiles
(`ERROR_MAX_PROFILES`) — every distinct pid ever used created one. Rotating the
pid again makes that worse. Check the dashboard and remove profiles you no longer
need.

## The captcha is detected but never solved

Expected, and handled: keep the fallback path. Detection is not a promise of a
solve, so treat `Captcha.solveFinished` as the only success signal and let the
scraper's own solver (`--twocaptcha-key`) take over otherwise.

On Amazon the challenge that matters is not reCAPTCHA at all: it is the
first-party image captcha, "Enter the characters you see below", posted back to
`/errors/validateCaptcha`. `captcha_solver.py` reads the image through the
browser's own request context — an answer to an image fetched from a different
address is rejected — and submits the solution as a GET, echoing back both
hidden form fields.

**This path is unproven live.** No run during development was served an image
captcha: 47 navigations across five marketplaces produced WAF challenges and
503s and nothing else. It is exercised only by an offline fixture written from
Amazon's documented markup. If you hit one, an issue with the captured page
would be genuinely useful.

The reCAPTCHA detectors are still present and still run — Amazon uses reCAPTCHA
on account and payment flows, and different geos surface different challenges.
If a reCAPTCHA solve returns a token the site rejects, check the variant: a
widget labelled v3 whose loader is v2-invisible needs the v2 parameters, and
`captcha_solver.py` reconciles the two and prefers the loader.

## HTTP 202, and a page whose only content is a script

Not a block, and not something to solve. That is the **AWS WAF challenge**: it
ships `challenge.js`, takes a token, and reloads itself. A real browser clears
it in about four seconds (measured: 3.9 s on amazon.co.uk), so the three
browser engines wait it out and never report it. Nothing is billed.

You will only see this as a failure through `scraper_api_client.py`, which runs
no JavaScript and therefore cannot clear it. Use `--cdp-url` to route that
engine through a browser, or use a browser engine directly.

## "Sorry! Something went wrong" / "Tut uns Leid"

Amazon's 503 throttle. It is transient: amazon.de returned it on a first
navigation and 200 from a fresh context seconds later. The engines reload it up
to `--retries` times and then try a different exit if there is a pool. If it
persists from one address, that address is being throttled and more retries
will not help — add `--proxy-file`.

## `--mode reviews` returns 0 rows

Amazon serves two variants of the same `/dp/` URL, and one of them renders the
review widget empty. The variant is assigned **per session** — a review-less
context stayed review-less across six consecutive reloads — so the engines
start a fresh browser rather than reloading, up to five times. At the measured
~55% hit rate that leaves under a 2% chance of an empty result.

If it still comes back empty:

1. **Over `--cdp-endpoint` this cannot be re-rolled** — the remote profile's
   cookies are not the client's to discard. Use a different `pid`.
2. Check the product actually has reviews. `--mode product` reports
   `review_count`; if that is null too, the page itself is the problem.

## A best-seller run is short by twenty products

The grid lazy-loads: 30 cards on first render, 50 after scrolling to the
bottom. If the count comes back short, the engine says so explicitly, because
a chart publishes contiguous ranks and the arithmetic is unambiguous:

```
Best-seller ranks 31-50 are missing from the merged result (80 rows over ranks
1-100). Those cards never loaded — the output is short by 20 product(s).
```

A slow exit is the usual cause. The scroll waits for the count AND the page
height to hold still for three rounds before giving up, so raising `--retries`
does not help here; a faster exit does.

## A quarter of the rows have no price

Expected on Amazon search pages, and not a parser failure. On one measured page
16 of 22 tiles carried a price and six did not — real products for which Amazon
renders no price to that visitor. Each run logs its coverage:

```
Price coverage on page 1: 24/30 (80%).
```

Below 50% it warns instead, because that is low enough to suspect the snapshot.
Use `--dump-html` and look for `.a-price` in the saved bytes: if it is absent
there too, the site withheld the price.

## amazon.co.uk quotes prices in EUR

Not a bug. Amazon converts prices for a visitor whose delivery country differs
from the marketplace, and quotes the converted currency — from a European exit,
both amazon.co.uk and amazon.co.jp return `EUR`. The `currency` column reports
what the page said, never what the domain implies.

If you need a marketplace's own currency, use an exit IP in that country
(`country-` in a Scraping Browser endpoint, or a proxy in that country).

## Two runs minutes apart disagree on the price by a cent

The exchange rate moved. Two runs of the same command ten minutes apart
disagreed on 10 of 43 shared ASINs, all by the same relative amount
(0.039–0.058%). Pass `diff_runs.py --price-tolerance-pct 0.1` to classify those
as FX noise rather than price changes. It defaults to 0 deliberately — a run
against a marketplace in its own currency has no conversion in it, and a
monitor that silently swallows small moves is worse than one that cries wolf.

## The discount percentage does not match the one on the page

`discount_pct` is computed from `original_price` and `price`, never read from
the "-45%" a tile prints. The printed figure is rendered from a different field,
rounds differently, and on a coupon item describes a discount that is not in
`price` at all — a coupon is conditional on clipping it, so it is reported in
its own `coupon` column and not folded into the price.

## Dependency conflicts

Real, and safe to ignore in practice:

```
playwright  requires  pyee>=13,<14
pyppeteer   requires  pyee>=11,<12   urllib3>=1.25.8,<2.0.0
selenium    requires  urllib3[socks]>=2.6.3,<3.0
```

`pip check` reports both collisions. All four engines still import and run
together, because none of them exercises the incompatible parts. For a clean
environment, give each engine its own venv.

`requirements.txt` holds only `beautifulsoup4` and `requests`, so install it
normally. The engines live in `requirements-playwright.txt`,
`requirements-selenium.txt` and `requirements-puppeteer.txt` — install **one**.
Installing all three in one command is what triggers the conflicts above, and
pip may resolve them by downgrading something you wanted.

## Everything looks right but the URLs are click trackers

They should not be, and if they are, something is not reading
`_canonical_product_url`. A sponsored tile's href is
`/sspa/click?...&url=%2F...%2Fdp%2FB0C3HCD34R...` — the product path
percent-encoded inside a tracking URL — so reading hrefs directly both stores
trackers and misses sponsored products entirely (a plain `a[href*="/dp/"]`
match finds none of them). The parser rebuilds `https://www.{host}/dp/{ASIN}`
from the ASIN instead, which is also what keeps `url` stable between runs.

`smoke_test.py` pins the fixture that guards it, including the double-encoded
form.

## `review_count` is an implausible number

Fixed in v0.1.0, and worth knowing what it looked like: an `aria-label` reading
"4.4 out of 5 stars, 279,961 ratings" was being read by stripping every digit
from the whole string, giving 445279961. It affected every row of every mode
while the column looked fully populated — which is why the offline suite now
asserts the VALUE on four real fixtures, not just its presence. If you see one
again, you are on an older build.
