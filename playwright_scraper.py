#!/usr/bin/env python3
"""
amazon-scraper — Playwright edition (primary engine)
====================================================

Scrapes Amazon listing pages, product detail pages and the reviews rendered
on a detail page, from any of the 21 marketplaces. Which marketplace is
decided by the URL you pass — there is no --marketplace flag, so a flag and a
URL cannot disagree about which site a run is reading.

    --mode listing   (default)  search results (/s?k=...) and best-seller
                                grids (/zgbs/...), paginated
    --mode product              one /dp/{ASIN} page, with brand, seller,
                                bullets, images and availability
    --mode reviews              the ~13 reviews Amazon renders to an
                                anonymous visitor on /dp/{ASIN}

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money; the shared decisions live
in output_writer.finish_run() so they cannot drift apart.

What is different about Amazon
------------------------------
* **No JSON-LD anywhere.** Rows come from the site's own `data-asin`
  attributes. See product_parser.py's docstring for the measurements.
* **AWS WAF, not Akamai.** Amazon's front door answers a suspicious request
  with HTTP 202 and a self-solving JavaScript challenge. A real browser
  clears it in a few seconds without help and without a captcha solve, so
  this engine WAITS for it rather than reporting a block. Treating it as a
  block is how a run gives up on a page it would have got; treating it as a
  captcha is how it pays for one it did not need.
* **503 is a throttle, not a failure.** "Sorry! Something went wrong" comes
  back on a first request and 200 on a retry seconds later. Retried, then
  answered with a different exit.
* **The image captcha is the paid path.** "Enter the characters you see
  below" is Amazon's own captcha and the only state here worth a 2Captcha
  solve. See captcha_solver.detect_amazon_captcha.
* **Best-seller grids lazy-load.** A captured grid held 30 cards before
  scrolling and 50 after, so the readiness wait scrolls until the count stops
  growing instead of trusting the first render.

Usage
-----
    python playwright_scraper.py \\
        --url "https://www.amazon.com/s?k=wireless+headphones" \\
        --pages 3 \\
        --format both

    python playwright_scraper.py --mode product --url "https://www.amazon.com/dp/B07K5214NZ"
    python playwright_scraper.py --mode reviews --url "https://www.amazon.com/dp/B07K5214NZ"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS,
                            detect_amazon_captcha, solve_amazon_captcha,
                            amazon_captcha_submit_url)
from product_parser import (parse_products, parse_product_detail, parse_reviews,
                            SELECTORS, detect_bot_challenge, detect_page_state,
                            page_url, listing_kind, marketplace_host)
from output_writer import dedupe_by_key, finish_run
import page_flow
from page_flow import NEXT_PAGE_SELECTOR, MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate ASIN (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # Set when Amazon answered with a signin redirect. Neither a block nor a
    # failure: the run asked for something an anonymous visitor cannot have.
    needs_signin: bool = False

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None and not self.needs_signin


ITEM_LINK_SELECTOR = SELECTORS["item_link"]


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    def scroll_into_view(selector):
        try:
            element = page.query_selector(selector)
            if element is not None:
                element.scroll_into_view_if_needed(timeout=5000)
        except PWError as e:
            logger.debug("Could not scroll %s into view: %s", selector, e)

    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "page_height": lambda: page.evaluate("() => document.body.scrollHeight"),
        "scroll_to_bottom": lambda: page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"),
        "sleep": page.wait_for_timeout,
        "scroll_into_view": scroll_into_view,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args) -> int:
    return page_flow.min_matches(args.mode)


def _needs_scrolling(args, url: str) -> bool:
    return page_flow.needs_scrolling(args.mode, url)


def _wait_out_waf_challenge(page, args) -> bool:
    d = _driver(page)
    return page_flow.wait_out_waf_challenge(d["content"], d["current_url"], d["sleep"])


def _scroll_until_stable(page, selector: str, **kwargs) -> int:
    d = _driver(page)
    return page_flow.scroll_until_stable(d["count"], d["page_height"],
                                         d["scroll_to_bottom"], d["sleep"],
                                         selector, **kwargs)


def _hydrate(page, args, selector: str, threshold: int,
             attempts: Optional[int] = None) -> int:
    d = _driver(page)
    if attempts is None:
        attempts = page_flow.hydrate_attempts(args.mode)
    return page_flow.hydrate(d["count"], d["page_height"],
                             d["scroll_to_bottom"], d["sleep"],
                             d["scroll_into_view"], args.mode, selector,
                             threshold, attempts)

# What "the page has painted" means, per mode. The listing form deliberately
# matches the PRIMARY anchors and not `a[href*="/dp/"]`: a captured search
# page carries 143 such links for 22 organic tiles, most of them in carousels
# that render before the grid does, so counting them would call the page
# ready while the results were still empty.


# Ordered most-durable first, per the family rule: a standards-based signal
# before a build artefact. `<link rel="next">` is FIRST and is known to be
# ABSENT — checked on both listing kinds on 2026-09-08, zero matches. It is
# kept because it costs one query and is the form that would survive a
# redesign, but it is explicitly not what makes pagination work here:
# `a.s-pagination-next` is a class Amazon can rename any day, and
# product_parser.page_url() is what actually backs pagination up.


# How many primary anchors must appear before the page counts as loaded
# rather than as a lucky single match. Must be > 1: waiting for one resolves
# on an unrelated element long before the grid paints. Five, against measured
# floors of 16 organic tiles on amazon.com, 16 on amazon.de, 60 on
# amazon.co.jp and 30-before-scroll on a best-seller grid — low enough that a
# short result page still passes, high enough that a single stray match does
# not.


# How long to let the AWS WAF interstitial clear itself. Measured at 3.9s on
# amazon.co.uk; 25s is generous enough for a slow exit and still bounded,
# because "every remote call is bounded" and an unbounded wait here would
# hang a run on a challenge that is never going to resolve.


def _plan_page_urls(page, args, page_one_url: str) -> Optional[List[str]]:
    """URLs for pages 2..N, decided once from page 1, or None to chain.

    Following the site's own next-link one page at a time is correct but
    strictly sequential: the address of page 5 is not knowable until page 4
    has been fetched. Constructing the page parameter up front removes that
    chain — which is what makes fetching pages independently (and later,
    concurrently) possible at all.

    It is only safe when the site's own link AGREES with the convention, so
    that is checked rather than assumed: if page 1's next-link is not what
    `page_url()` would build for page 2, pagination is carrying something the
    convention cannot reproduce (a cursor, a token, a filter id) and the
    caller must keep chaining link to link. Returns None in that case.

    On Amazon the check passes: page 1's next link is `?...&page=2` plus
    tracking parameters (`xpid`, `qid`, `ref`) that _same_url ignores.
    """
    if args.pages < 2:
        return None

    constructed = page_url(page_one_url, 2)
    next_link = page.query_selector(NEXT_PAGE_SELECTOR)
    href = next_link.get_attribute("href") if next_link else None

    if href:
        advertised = _resolve_pagination_url(page_one_url, href)
        if _same_url(advertised, constructed):
            logger.info("Pagination follows the ?%s=N convention (page 2 link "
                        "matches the constructed URL) — planning pages 2-%d up "
                        "front.", "pg" if listing_kind(page_one_url) == "bestsellers"
                        else "page", args.pages)
        else:
            logger.info("The site's own next-page link (%s) is not what the "
                        "page convention would build (%s) — following its links "
                        "one page at a time instead. Pages cannot be fetched "
                        "independently for this listing.", advertised, constructed)
            return None
    else:
        logger.warning(
            "No pagination link matched %s on page 1 — falling back to the URL "
            "convention. Amazon serves no link[rel=next], so this is the "
            "expected path once the anchor classes change; if the run comes "
            "back with one page of data, that is what to check first.",
            NEXT_PAGE_SELECTOR)

    return [page_url(page_one_url, n) for n in range(2, args.pages + 1)]


def _next_url_from_page(page, args, page_num: int) -> str:
    """Next page's URL from the site's own link, falling back to ?page=N.

    Only used when pagination could not be planned up front. A missing link
    must not end the run: pagination resting entirely on DOM selectors is a
    silent-success failure waiting to happen, so the convention backs it up
    and the DATA decides when to stop.
    """
    next_link = page.query_selector(NEXT_PAGE_SELECTOR)
    href = next_link.get_attribute("href") if next_link else None
    if href:
        return _resolve_pagination_url(page.url, href)
    return page_url(page.url, page_num + 1)


def _same_url(a: str, b: str) -> bool:
    """URL equality that ignores tracking parameters and parameter ORDER.

    Amazon's own next-page link carries `xpid`, `qid` and `ref` alongside
    `page=2`. None of them select content — they are search-session and
    click-attribution ids — so a strict comparison would decide the site's
    link disagrees with the convention on every single run, and every run
    would fall back to sequential chaining and refuse --concurrency.
    """
    tracking = page_flow.TRACKING_PARAMS
    pa, pb = urlparse(a), urlparse(b)

    def significant(query):
        return sorted((k, v) for k, v in parse_qsl(query) if k not in tracking)

    return (pa.scheme, pa.netloc, pa.path.rstrip("/")) == \
           (pb.scheme, pb.netloc, pb.path.rstrip("/")) and \
           significant(pa.query) == significant(pb.query)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like. On Amazon it also resets the AWS WAF token, which is bound to
    the address that earned it.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # Note it does NOT cover the AWS WAF interstitial, which needs no solving
    # anyway — see _settle_page.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


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


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. On Amazon that is not an edge case: the AWS WAF challenge reloads the
    page from JavaScript the moment it has its token, so a snapshot taken
    right after goto() can land exactly on the swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (the WAF challenge reloading?) — "
                        "retrying content() in %dms (%d/%d).", pause_ms,
                        attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. Two
    families are checked, because Amazon serves one on the paths this scraper
    reads and the other elsewhere:

      1. Amazon's own image captcha ("Enter the characters you see below"),
         which is what a blocked listing page actually looks like;
      2. reCAPTCHA, via the static-HTML and runtime detectors reconciled
         against each other. Amazon uses it on account and payment flows, and
         the family's rule is that detection stays broad — different geos and
         scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    amazon_challenge = detect_amazon_captcha(html, page.url)
    if amazon_challenge:
        if when_blocked and already_rendered > MIN_CARD_MATCHES:
            logger.info("Amazon image captcha detected, but %d anchors are "
                        "already on the page — not solving it. Pass "
                        "--solve-captcha always to solve it anyway.",
                        already_rendered)
            return False
        logger.warning("Amazon image captcha (%s) — attempting to solve.",
                       amazon_challenge.image_url)
        if not args.twocaptcha_key:
            logger.warning("No 2captcha API key, so this captcha cannot be "
                           "solved — continuing with whatever the page holds. "
                           "Pass --twocaptcha-key or set TWOCAPTCHA_KEY if the "
                           "run comes back blocked (exit 3).")
            return False
        try:
            # Fetched through the BROWSER's request context, not this
            # process: the image is bound to the session that was served the
            # captcha, and a correct answer to an image fetched from another
            # address is rejected.
            def fetch_image(url):
                return page.request.get(url, timeout=30000).body()

            solution = solve_amazon_captcha(amazon_challenge, args.twocaptcha_key,
                                            fetch_image, api_version=args.captcha_api)
        except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
            logger.error("Solving the captcha failed (%s) — continuing with "
                         "whatever the page holds. If it was in fact blocking, "
                         "the run will report that as exit 3.", e)
            return False
        submit = amazon_captcha_submit_url(amazon_challenge, solution)
        logger.info("Submitting the solution and returning to the page.")
        try:
            page.goto(submit, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            logger.error("Submitting the captcha solution failed (%s).", e)
            return False
        return True

    # BOTH reCAPTCHA detectors run, always — not static-then-fallback. They
    # can disagree on the same page about the widget's version, and the
    # runtime one is authoritative when they do, so short-circuiting on the
    # static one would send the wrong parameters to 2captcha.
    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


# The container each mode's content hangs off, used to pull the lazy-load
# trigger precisely instead of jumping past it. Amazon hydrates the review
# widget from an intersection observer on #reviewsMedley: scrolling straight
# to the end of the document flies past that element without ever
# intersecting it, which is why "scroll to the bottom" got 13 reviews on two
# runs out of three and zero on the other.


def _parse_for_mode(html: str, url: str, args) -> List:
    if args.mode == "product":
        return parse_product_detail(html, url, category=args.category)
    if args.mode == "reviews":
        return parse_reviews(html, url)
    return parse_products(html, url, category=args.category)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a throttle, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried from a DIFFERENT exit.
    # Zero without a pool: there is nowhere else to go, and a bare retry from
    # the same address just burns it further.
    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        # The WAF interstitial comes first: until it clears, every other
        # check would be looking at a challenge page rather than at Amazon.
        _wait_out_waf_challenge(session.page, args)

        if handle_captcha_if_present(session.page, args):
            # A solve navigated the page. Give the destination a moment
            # before judging what came back.
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = detect_page_state(html, url=session.page.url)

        # A 503 throttle is not a block and not a load failure: Amazon
        # answered, it just declined to serve content this second. A fresh
        # request seconds later gets 200 — measured on amazon.de, which
        # returned 503 on a first navigation and 200 on a fresh context. So
        # it is reloaded a few times before an exit is blamed.
        if state == "throttled":
            for attempt in range(1, args.retries + 1):
                pause = args.retry_delay * (2 ** (attempt - 1))
                logger.warning("Amazon returned its 'Sorry, something went "
                               "wrong' throttle page — reloading in %.1fs "
                               "(%d/%d).", pause, attempt, args.retries)
                time.sleep(pause)
                try:
                    session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except (PWTimeout, PWError):
                    continue
                _wait_out_waf_challenge(session.page, args)
                html = _content_when_settled(session.page) or ""
                state = detect_page_state(html, url=session.page.url)
                if state != "throttled":
                    break

        if state in ("ok", "signin"):
            break

        # Still throttled or still behind a captcha. A different exit is the
        # one thing that plausibly changes the outcome — the address is what
        # was scored, so retrying it unchanged would only confirm it.
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    if state == "signin":
        # Not a block and not solvable. The run asked for a page Amazon does
        # not serve anonymously — /product-reviews/ does this on page 1. Kept
        # distinct so the message can say what to do about it instead of
        # suggesting a proxy or a captcha key would help.
        logger.error("Amazon redirected to sign-in (%s). This page needs an "
                     "account; no proxy or captcha solve changes that. For "
                     "reviews use --mode reviews, which reads the ones "
                     "rendered on the product page itself.", session.page.url)
        outcome.needs_signin = True
        outcome.final_url = session.page.url
        return outcome

    if state == "ok":
        # Don't wait for network idle (retail sites never go fully quiet) and
        # don't accept a single selector match as "ready".
        selector, threshold = _ready_selector(args), _min_matches(args)
        scrolls = _needs_scrolling(args, session.page.url)

        # A FRESH SESSION, not a scroll and not a reload, is what fixes an
        # empty review widget. Three measurements, in the order they were
        # taken, because the first two conclusions were wrong:
        #
        #   1. Amazon serves two variants of the same /dp/ URL — one with the
        #      reviews in the initial markup (13 containers, ~466 [data-hook]
        #      elements, 17.5k page height), one where the widget is present
        #      but empty (5 [data-hook] elements, 10.8k height). Six scroll
        #      passes left the second at zero, so it is not a lazy load.
        #   2. Reloading does not help either: a review-less context stayed
        #      review-less across six consecutive navigations (0,0,0,0,0,0).
        #      The variant is sticky to the SESSION.
        #   3. A fresh context does help: 3 of 5 new contexts returned the
        #      full page, and 4 of 8 in an earlier sample. So the variant is
        #      assigned per cookie jar, and a new browser is the only thing
        #      that re-rolls it.
        #
        # Hence session.relaunch() — the same tool a proxy rotation uses, for
        # the same reason: a session cannot be talked out of what it has
        # already been assigned. Note this is a no-op over --cdp-endpoint,
        # where the profile's cookies are not ours to discard; there, a
        # review-less profile stays review-less and a different `pid` is the
        # answer.
        session_budget = page_flow.session_attempts(args.mode, args.retries)
        for attempt in range(1, session_budget + 1):
            if scrolls:
                _hydrate(session.page, args, selector, threshold)
            content_timeout = page_flow.content_timeout_ms(args.mode)
            try:
                session.page.wait_for_function(
                    f"document.querySelectorAll({selector!r}).length > {threshold}",
                    timeout=content_timeout)
                session.page.wait_for_timeout(500)
            except PWTimeout:
                logger.warning("No content markers (%s) appeared within %.0fs.",
                               selector, content_timeout / 1000)

            if scrolls:
                # A second pass, because the wait above may have been what let
                # the first batch appear, and the batch after it needs another
                # scroll. Cheap when there is nothing more to load.
                loaded = _scroll_until_stable(session.page, selector)
                logger.info("Scrolled until the count stopped growing: %d "
                            "match(es).", loaded)

            if len(session.page.query_selector_all(selector)) > threshold:
                break
            if attempt < session_budget:
                logger.info("Nothing matched %s. %s (attempt %d/%d).",
                            selector, page_flow.VARIANT_RETRY_NOTE, attempt,
                            session_budget)
                if session.remote:
                    logger.warning("Cannot start a fresh session over "
                                   "--cdp-endpoint: the remote profile's "
                                   "cookies are not ours to discard. Try a "
                                   "different `pid` in the endpoint URL.")
                    break
                try:
                    session.relaunch()
                    session.page.goto(url, wait_until="domcontentloaded",
                                      timeout=60000)
                    _wait_out_waf_challenge(session.page, args)
                except (PWTimeout, PWError) as e:
                    logger.warning("The fresh session could not load the page "
                                   "(%s) — parsing what is there.", e)
                    break
            else:
                logger.warning("Still nothing after %d session(s). Amazon kept "
                               "serving the variant without this content; the "
                               "run will report what it has.", session_budget)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    vendor = detect_bot_challenge(html, url=session.page.url)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     f" (tried {block_retries + 1} exit(s))" if block_retries else "")
        outcome.blocked_by = vendor
        return outcome

    products = _parse_for_mode(html, session.page.url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    # A best-seller grid publishes contiguous ranks, so a gap is arithmetic
    # rather than a guess: 30 rows spanning ranks 1-50 means 20 cards never
    # loaded. Without this the run reports a complete page and a consumer
    # sees 20 products "delisted" from the chart.
    if products and args.mode == "listing":
        ranks = [p.position for p in products if p.position is not None]
        if ranks and listing_kind(session.page.url) == "bestsellers":
            span = max(ranks) - min(ranks) + 1
            if span > len(ranks):
                logger.warning("Best-seller page %d returned %d rows spanning "
                               "ranks %d-%d — %d card(s) never loaded. The "
                               "lazy-load did not finish; the output is short "
                               "by that many products.", page_num, len(ranks),
                               min(ranks), max(ranks), span - len(ranks))

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        # Reported every time, not only when it looks wrong: on Amazon a
        # quarter of a page legitimately carries no price (16 of 22 priced on
        # a measured live page), so a consumer needs the number rather than a
        # threshold someone guessed. Below half, something is more likely
        # wrong with the snapshot than with the catalogue.
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(products), 100.0 * priced / len(products))
        if priced * 2 < len(products):
            logger.warning("Fewer than half the rows on page %d carry a price. "
                           "Amazon does withhold some prices, but this is low "
                           "— re-run with --dump-html to check the snapshot.",
                           page_num)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on its
        own.

    Its exit stays put for the worker's lifetime otherwise: a SESSION must
    not change address mid-flight, and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no rows at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page result would fetch
    # 45 empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # A reviews run keys on the review, not the product: a dozen rows
    # legitimately share one ASIN, and deduping those by sku would keep one
    # review per product and silently drop the rest.
    dedupe_key = "review_id" if args.mode == "reviews" else "sku"
    # Why the loop ended. "completed" means every requested page was fetched;
    # "no_new_products" means the listing itself ran out (also a complete
    # result). "single_page_mode" is complete by construction — a detail page
    # has no page 2. Anything else is an early stop, and the run is only a
    # partial view.
    stop_reason = "single_page_mode" if args.mode != "listing" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode != "listing":
            logger.info("--concurrency is ignored in --mode %s: there is one "
                        "page to fetch.", args.mode)
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. "
                           "Amazon is quicker than most to notice. Pass "
                           "--proxy-file to spread the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own: its content is what decides
            # whether pages 2..N can be addressed independently at all.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else "needs_signin" if first.needs_signin
                               else f"blocked_{first.blocked_by}")
                # A signin wall is not a bot challenge, but it has the same
                # consequence: something stands between the run and the
                # content. The family's exit codes have one code for that
                # (3), so it maps there rather than reporting exit 4 as if
                # the result were genuinely empty — and `stop_reason:
                # needs_signin` in the sidecar keeps the two apart for anyone
                # who needs to know which it was.
                blocked = first.blocked_by is not None or first.needs_signin
            elif args.mode != "listing":
                pass  # one page is the whole run
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                planned = _plan_page_urls(session.page, args, first.final_url)

                if args.pages > 1 and concurrency > 1 and planned is None:
                    logger.warning("--concurrency %d requested, but this "
                                   "listing's pagination cannot be addressed "
                                   "independently (see above) — falling back to "
                                   "one page at a time.", concurrency)
                    concurrency = 1

                if args.pages > 1 and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # browser than asked for.
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                args.pages, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else "needs_signin" if worst.needs_signin
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by or o.needs_signin for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        # Should not happen without a failure or exhaustion,
                        # but say so rather than reporting a complete run.
                        stop_reason = "pages_unattempted"
                    session = None  # already closed
                else:
                    url = (planned[0] if planned else
                           _next_url_from_page(session.page, args, 1))
                    for page_num in range(2, args.pages + 1):
                        # A new exit per page is what actually spreads a run's
                        # volume, and it costs a browser relaunch: carrying the
                        # session across exits would defeat the point.
                        if pool and pool.rotates_per_page():
                            pool.advance(f"per-page rotation, page {page_num}")
                            session.relaunch()

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout" if outcome.load_failed
                                           else "needs_signin" if outcome.needs_signin
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = (outcome.blocked_by is not None
                                       or outcome.needs_signin)
                            break

                        # Whether this page contributed anything not already
                        # seen. Kept as a running check because the condition is
                        # inherently sequential — "new" only means anything
                        # relative to the pages before it. The authoritative
                        # dedupe happens once, after the loop, in page order.
                        fresh_count = sum(1 for p in outcome.products
                                          if p.sku is None or p.sku not in seen_keys)
                        seen_keys.update(p.sku for p in outcome.products
                                         if p.sku is not None)

                        # A page past the first that contributes nothing new
                        # means the end of the results — or that pagination is
                        # looping back on itself. Either way there is nothing
                        # further to fetch, and this is the honest terminating
                        # condition: a property of the DATA, not of a CSS
                        # selector that may have been renamed. It matters more
                        # here than elsewhere in this family, because Amazon
                        # keeps serving a valid page for `&page=400` and
                        # repeats content rather than 404ing.
                        if not fresh_count:
                            logger.info("Page %d added no rows not already seen "
                                        "— treating that as the end of the "
                                        "listing.", page_num)
                            stop_reason = "no_new_products"
                            break

                        if page_num < args.pages:
                            url = (planned[page_num - 1] if planned else
                                   _next_url_from_page(session.page, args, page_num))
                            time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Not "on an earlier page": page 1 dropped rows on a live run,
            # because Amazon renders the same ASIN twice on one page (once
            # sponsored, once organic). The duplicate can be on this page.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # A per-page rank check cannot see a gap between pages, and that is
    # exactly where the lazy-load loss showed up: page 1 held ranks 1-30 and
    # page 2 started at 51, so ranks 31-50 were missing from a run that
    # reported itself complete. Checked here, over the merged result.
    if args.mode == "listing" and listing_kind(args.url) == "bestsellers":
        ranks = sorted(r.position for r in all_rows if r.position is not None)
        if ranks:
            missing = sorted(set(range(ranks[0], ranks[-1] + 1)) - set(ranks))
            if missing:
                logger.warning("Best-seller ranks %d-%d are missing from the "
                               "merged result (%d rows over ranks %d-%d). Those "
                               "cards never loaded — the output is short by %d "
                               "product(s), and this run is NOT a complete view "
                               "of the chart.",
                               missing[0], missing[-1], len(ranks), ranks[0],
                               ranks[-1], len(missing))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=marketplace_host(final_url),
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(description="Amazon scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Amazon URL. A search listing (/s?k=...), a best-seller "
                        "grid (/zgbs/...) or a product page (/dp/ASIN) depending "
                        "on --mode. Any of the 21 marketplaces — the hostname "
                        "decides which. Required, unless AMAZON_URL is set in "
                        "the environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product", "reviews"],
                   default="listing",
                   help="listing (default): paginated search results or a "
                        "best-seller grid. product: one /dp/ page with brand, "
                        "seller, bullets and images. reviews: the ~13 reviews "
                        "Amazon renders on a /dp/ page to a visitor with no "
                        "account — /product-reviews/ needs one even for page 1, "
                        "so there is no --pages for this mode.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the search "
                        "term or the department slug from the URL (and to the "
                        "breadcrumb in --mode product), so the column is never "
                        "empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1,
                   help="Number of listing pages to crawl. Ignored outside "
                        "--mode listing.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "Also how many times Amazon's 503 throttle page is "
                        "reloaded before another exit is blamed. The pause "
                        "between attempts doubles each time.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="amazon_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). Amazon keys page "
                        "language off this and the exit IP; it does NOT decide "
                        "the currency, which follows the delivery country.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back throttled or behind a captcha, "
                        "retry it from this many OTHER exits before giving up "
                        "(default 2). Needs a pool of more than one; ignored "
                        "otherwise.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and this default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop",
    # which the fingerprint API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation.
    #
    # fingerprint_client.py's own --tags help has said so all along; the
    # engines' default contradicted it. Measured against the live API on
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`,
    # `Chrome` and `Desktop` each 400.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a challenge "
                        "if the content is not already readable. always: solve "
                        "whenever one is detected. Neither setting touches the "
                        "AWS WAF interstitial, which the browser clears by "
                        "itself and which no solve would help.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets and for Amazon's image captcha.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and AMAZON_URL is not set in the environment "
                "or in .env.")
    if args.mode != "listing" and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something.
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read. The run status will say single_page_mode.",
                       args.pages, args.mode)
        args.pages = 1
    if args.mode == "listing" and listing_kind(args.url) == "detail":
        p.error("--url is a product page (/dp/ASIN) but --mode is listing. "
                "Use --mode product or --mode reviews for a product URL.")
    if args.mode in ("product", "reviews") and listing_kind(args.url) != "detail":
        p.error(f"--mode {args.mode} needs a product URL containing /dp/ASIN; "
                f"got {args.url}")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
