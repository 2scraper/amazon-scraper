#!/usr/bin/env python3
"""
amazon-scraper — 2captcha Scraper API edition (fourth engine)
=============================================================

A fourth way to run this scraper. Unlike playwright_scraper.py /
puppeteer_scraper.py / selenium_scraper.py, this one manages **no browser and
no CDP session of its own**: it POSTs a URL to 2captcha's separate **Scraper
API** (https://scraper.2captcha.com — a different product from the Scraping
Browser API the other three reach through --cdp-endpoint), gets HTML back over
plain HTTPS, and feeds it to this project's product_parser.

Why you would want it: no Chromium to install, no CDP plumbing, runs from a
tiny container or a lambda.

WHAT THIS SITE NEEDS — READ THIS FIRST
--------------------------------------
Amazon is a better fit for a browserless path than most sites in this family,
and worse in one specific way.

Better: Amazon renders its listings SERVER-SIDE. The products are in the HTML
that arrives — 22 organic tiles with prices, ratings and ASINs were parsed
from a saved response with no browser in the path at all, which is how this
project's offline fixtures work. There is no client-side hydration to wait
for on a search page.

Worse: Amazon's front door is AWS WAF, and its challenge is JavaScript. A
challenged request gets HTTP 202 and a page whose only content is
`challenge.js`; clearing it requires running that script, which is precisely
what this engine does not do. A browser clears it in about four seconds, so
the three browser engines never even report it.

So: this path works when the request is not challenged, and cannot when it
is. It fails loudly either way — a challenge page exits 3 rather than being
written as a result.

**Untested against amazon.com.** No Scraper API subscription was available
while this repo was built, so unlike the three browser engines (each of which
has live numbers in the README) this one has none. Treat it as unproven, and
prefer `--cdp-url` — which routes the fetch through a real browser and so
gets the WAF challenge cleared for you.

Listing pages only. `--mode product` / `--mode reviews` live in the browser
engines; the reviews widget in particular is served per-session and needs a
fresh browser to re-roll, which this engine has no way to do.

API surface used (per https://2captcha.com/scraper/scraper-api/api)
------------------------------------------------------------------
  POST https://scraper.2captcha.com/tasks/sync
    Authorization: Bearer <API_KEY>
    Content-Type: application/json
    {"task_type": "scrape", "url": ..., "data_format": "raw",
     "format": "json", "timeout": 1..120,
     "waitFor": "<JSON *string*, not an object>",
     "cdpurl": "ws://user:pass@host:port"   # optional
    }
  -> 200 {"status": 200, "headers": {...}, "body": "<!DOCTYPE html>..."}

Note `waitFor` must be a JSON *string* (double-encoded), and the param is
spelled `cdpurl` (all lowercase) while `waitFor` is camelCase — that is the
API's own inconsistency, not a typo here.

Usage
-----
    # plain HTTP, no browser anywhere
    python3 scraper_api_client.py \
        --key "$TWOCAPTCHA_KEY" \
        --url "https://www.amazon.com/s?k=wireless+headphones"

    # routed through a Scraping Browser API session, which clears the WAF
    # challenge on the way
    python3 scraper_api_client.py \
        --key "$TWOCAPTCHA_KEY" \
        --url "https://www.amazon.com/s?k=wireless+headphones" \
        --cdp-url "ws://user:pass@cb.2captcha.com:9222" \
        --wait-text 's-search-result' --timeout 90

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import (parse_products, detect_bot_challenge,
                            marketplace_host, BOT_CHALLENGE_MARKERS)
from output_writer import finish_run, EXIT_FETCH_FAILED
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API failing
# is not the operator passing wrong arguments, and a harness that lumps them
# together sends you looking in the wrong place. Run 7 reported `exit=2` for an
# HTTP 422 from the API — which reads as "you called it wrong".
# An alias, not a second declaration: "the remote API failed" and "the page
# was never fetched" are the same fact to a caller, and output_writer is
# where that code's meaning is written down. Spelling 5 here again is how the
# two drift.
EXIT_API_ERROR = EXIT_FETCH_FAILED

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver._KEY_IN_TEXT_RE and fingerprint_client's. A
# third copy is one too many and they should be unified in a family pass;
# reaching into another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed `cdpurl` put that URL's
    username and password into the log, and a key passed as a query parameter
    would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field. The trade is that a credential in a
    shape neither pattern knows would survive — so the patterns are global
    and the check beside them uses realistic fixtures rather than the two
    literals that happen to appear here today.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[str]:
    """`waitFor` must be a JSON STRING (double-encoded), per the API docs.
    Passing a nested object is silently wrong.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return json.dumps({"text": args.wait_text})
    if args.wait_element:
        return json.dumps({"element": args.wait_element, "checkVisible": True})
    if args.wait_state:
        return json.dumps({"state": args.wait_state})
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", wait_for)

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = body.get("status")
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    return html


def _finish(rows, args, *, blocked: bool, stop_reason: str) -> int:
    """Every outcome of this client, through the same decision the engines use.

    It used to call `save` and hand-spell its own 3 and 4, which meant a
    Scraper API run wrote no run-metadata sidecar at all: a consumer could
    read the rows but could not learn the status, the stop reason or the
    marketplace, and had no way to tell an empty result from a challenge
    page except by reading the log. The browser engines have had that since
    finish_run existed.

    This does NOT make it a fourth engine — it still fetches exactly one
    page and still has no pagination, which is why `stop_reason` is
    `single_page_mode` on success. It makes its OUTPUT honest, which is a
    smaller claim and the one worth making now.
    """
    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      # One page, always: there is no --pages here.
                      pages_requested=1,
                      pages_completed=1 if rows else 0,
                      mode="listing", source=marketplace_host(args.url),
                      start_url=args.url, final_url=args.url)


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final (see _run_once), so a
    # single attempt is not evidence. Each retry is a fresh billable task —
    # $0.0005 at the observed rate — so the default is deliberately low.
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    if attempts > 1:
        logger.info("Attempt %d/%d", attempt, attempts)

    try:
        html = fetch_html(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s", e)
        return _finish([], args, blocked=False, stop_reason="http_error")
    except RuntimeError as e:
        # HTTP 4xx/5xx from the API, including the 422 that a busy or
        # unreachable cdpurl produces.
        logger.error("%s", e)
        return _finish([], args, blocked=False, stop_reason="http_error")

    if args.dump_html:
        with open(args.dump_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Raw HTML written to %s", args.dump_html)

    vendor = detect_bot_challenge(html)
    if vendor:
        logger.error(
            "The Scraper API returned a %s bot-challenge page (%d bytes), not real content.",
            vendor, len(html),
        )
        logger.error("A challenge page is not a final answer — retry before concluding "
                     "anything (--retries). This site needs a rendered browser in the "
                     "path: pass --cdp-url, or use playwright_scraper.py / "
                     "puppeteer_scraper.py directly.")
        return _finish([], args, blocked=True,
                       stop_reason="blocked_%s" % vendor)

    products = parse_products(html, args.url, category=args.category)
    logger.info("Parsed %d products.", len(products))

    if not products:
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 products parsed — saved the raw response to %s so you can see "
                       "what actually came back.", dump)
        return _finish([], args, blocked=False, stop_reason="completed")

    return _finish(products, args, blocked=False, stop_reason="single_page_mode")


def parse_args():
    p = argparse.ArgumentParser(
        description="Amazon scraper — 2captcha Scraper API edition (no local browser). "
                    "NOTE: untested against amazon.com, and cannot clear the "
                    "category pages; see this file's docstring.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="Amazon listing URL (/s?k=... or /zgbs/...). Required, unless "
                        "AMAZON_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="amazon_products_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page, e.g. '$'. Use this "
                           "on protected sites — a DOM/load wait is satisfied instantly by "
                           "the challenge page itself.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'a[href*=\"-item-\"]'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were parsed. Off by "
                        "default so a failed fetch can't overwrite a good result.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. One retry is "
                        "usually worth it. Each attempt is a separate billable task, so "
                        "this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML to this path (always, even on success)")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "AMAZON_CDP_ENDPOINT": "cdp_url",
        "AMAZON_URL": "url",
    })
    if not args.url:
        p.error("no --url given, and AMAZON_URL is not set in the environment "
                "or in .env.")
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
