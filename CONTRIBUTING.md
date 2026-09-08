# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Amazon changing its markup is the normal way this stops working, and it has its
own issue template. The detail that saves the most time is which of the three
paths broke, because the parser tries them in order:

1. the page kind's own anchor — `div[data-component-type="s-search-result"]`,
   `[id^="p13n-asin-index-"]`, `div[data-hook="reviewContainer"]`;
2. the other listing kind's anchor, in case the URL's shape misled it;
3. the `/dp/{ASIN}` URL pattern, which logs a warning when it runs because it
   over-collects carousel items.

There is deliberately **no JSON-LD path**: Amazon publishes none. If you are
about to add one, read the measurement in `product_parser.py` first.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Five properties in this repo exist because they were once absent and cost real
time. Tests pin all five, so a PR that breaks one will fail rather than
silently regress:

- **The row's `url` is rebuilt from the ASIN, never read from an href.** A
  sponsored tile links to `/sspa/click?...&url=%2F...%2Fdp%2FASIN...`, so
  reading hrefs stores click trackers and misses every sponsored product. It
  also keeps `url` stable between runs, which Amazon's `qid`/`xpid`/`ref`
  parameters are not.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked (a captcha, an unresolved throttle, or a sign-in wall),
  `4` zero rows, `5` remote API error, `6` partial. A pipeline branches on
  these.
- **The AWS WAF challenge is waited out, never reported as a block and never
  sent to the solver.** A browser clears it by itself in about four seconds.
  Reporting it as a block reports a block that does not exist; solving it bills
  for a challenge no solver can answer. `page_flow.py` holds that policy for
  all three engines so they cannot disagree about it.
- **An ASIN already written by an earlier page of the same run is dropped, not
  duplicated** — and on Amazon this is not hypothetical: sponsored placements
  repeat across pages, so a 3-page run of 90 rows returned 77. See
  `dedupe_by_key` in `output_writer.py`. A reviews run keys on `review_id`
  instead, because a dozen rows legitimately share one ASIN.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
amazon.com, say in the PR what you ran, on which marketplace, from which
exit country, and what you
got. Product counts differ by country and by URL, so a bare "worked for me" is
not reproducible.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Amazon: search results, best-seller
grids, product pages, and the reviews shown to a visitor with no account.
Out of scope:
anything behind a login, anything that submits a form, and anything that
defeats a protection rather than passing it the way an ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
