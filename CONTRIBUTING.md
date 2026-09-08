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
own issue template. The one detail that saves the most time: the parser tries
**JSON-LD first**, then a CSS + URL-pattern fallback. Knowing which of the two
broke narrows the fix immediately. `--out dump` writes the page next to the
output when a run finds nothing.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Four properties in this repo exist because they were once absent and cost real
time. Tests pin all four, so a PR that breaks one will fail rather than
silently regress:

- **The product URL comes from `offers.url`, not `node.url`.** No product on
  this site carries `node.url`. Read the wrong field and every row points at the
  category page while title, brand and price all look correct.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked by a challenge, `4` zero products, `5` remote API error,
  `124` self-imposed timeout. A pipeline branches on these.
- **A sku already written by an earlier page of the same run is dropped, not
  duplicated.** All three browser engines paginate by following
  `NEXT_PAGE_SELECTOR`; a stale or repeating link must not double a row in the
  output. See `dedupe_by_sku` in `output_writer.py` and `diff_runs.py`, which
  diffs two runs by the same key.

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
