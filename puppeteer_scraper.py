#!/usr/bin/env python3
"""
amazon-scraper — pyppeteer edition (secondary engine)
=====================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  search results and best-seller grids
    --mode product              one /dp/{ASIN} page
    --mode reviews              the reviews rendered on /dp/{ASIN}

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * **No --concurrency.** The Playwright engine is the one that fetches pages
    in parallel; the flag is accepted here and reported as ignored rather
    than silently doing nothing.

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.amazon.com/s?k=wireless+headphones" --pages 3

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, detect_amazon_captcha,
                            solve_amazon_captcha, amazon_captcha_submit_url)
from product_parser import (parse_products, parse_product_detail, parse_reviews,
                            SELECTORS, detect_bot_challenge, detect_page_state,
                            page_url, listing_kind, marketplace_host)
from output_writer import dedupe_by_key, finish_run
import page_flow
from page_flow import NEXT_PAGE_SELECTOR, MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait out a WAF
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        message = str(context.get("exception") or context.get("message") or "")
        if "Target closed" in message or "Connection closed" in message:
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    needs_signin: bool = False

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None and not self.needs_signin


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


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded number: it drifts the moment a newer Chromium ships, and
    claiming an older Chrome than the JS engine and TLS handshake report is
    itself a mismatch a fingerprinter can key on. pyppeteer's
    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone. On Amazon a fresh browser also re-rolls
    the served page variant, which is the only fix for a detail page that
    came back without its reviews (see page_flow).
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            self.page = self.bridge.run(self.browser.newPage())
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
_SCROLL_INTO_VIEW_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (el) { el.scrollIntoView({block: 'center'}); return true; }
  return false;
}"""


def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def page_height():
        return bridge.run(page.evaluate("() => document.body.scrollHeight"))

    def scroll_to_bottom():
        bridge.run(page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def scroll_into_view(selector):
        try:
            bridge.run(page.evaluate(_SCROLL_INTO_VIEW_JS, selector))
        except Exception as e:  # noqa: BLE001 — best effort, never fatal
            logger.debug("Could not scroll %s into view: %s", selector, e)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # The AWS WAF challenge reloads the page from JavaScript, so a
            # snapshot can land exactly on the document swap. None tells the
            # caller to skip a check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    return {"count": count, "page_height": page_height,
            "scroll_to_bottom": scroll_to_bottom, "sleep": sleep,
            "scroll_into_view": scroll_into_view, "content": content,
            "current_url": current_url}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


def _parse_for_mode(html: str, url: str, args) -> List:
    if args.mode == "product":
        return parse_product_detail(html, url, category=args.category)
    if args.mode == "reviews":
        return parse_reviews(html, url)
    return parse_products(html, url, category=args.category)


def _same_url(a: str, b: str) -> bool:
    """URL equality ignoring Amazon's tracking parameters and their order."""
    pa, pb = urlparse(a), urlparse(b)

    def significant(query):
        return sorted((k, v) for k, v in parse_qsl(query)
                      if k not in page_flow.TRACKING_PARAMS)

    return (pa.scheme, pa.netloc, pa.path.rstrip("/")) == \
           (pb.scheme, pb.netloc, pb.path.rstrip("/")) and \
           significant(pa.query) == significant(pb.query)


def _next_page_href(session) -> Optional[str]:
    """The site's own next-page link, resolved, or None.

    Reads the DOM's `.href` property rather than the raw attribute, which the
    browser has already resolved — the opposite of Playwright's
    get_attribute("href"). Kept explicit because the two engines differ here
    and a hand-rolled join got it wrong once.
    """
    bridge, page = session.bridge, session.page
    return bridge.run(page.evaluate(
        "(selector) => { const a = document.querySelector(selector); "
        "return a ? (a.href || a.getAttribute('href')) : null; }",
        NEXT_PAGE_SELECTOR))


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    amazon_challenge = detect_amazon_captcha(html, page.url)
    if amazon_challenge:
        if when_blocked and already_rendered > MIN_CARD_MATCHES:
            logger.info("Amazon image captcha detected, but %d anchors are "
                        "already on the page — not solving it.", already_rendered)
            return False
        logger.warning("Amazon image captcha (%s) — attempting to solve.",
                       amazon_challenge.image_url)
        if not args.twocaptcha_key:
            logger.warning("No 2captcha API key, so this captcha cannot be "
                           "solved — continuing with whatever the page holds.")
            return False
        try:
            def fetch_image(url):
                # Fetched from inside the page, so it goes over the same
                # session and the same exit that was served the captcha. An
                # answer to an image fetched from elsewhere is rejected.
                data_url = bridge.run(page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include'});
                        const b = await r.arrayBuffer();
                        return Array.from(new Uint8Array(b));
                    }""", url))
                return bytes(data_url)

            solution = solve_amazon_captcha(amazon_challenge, args.twocaptcha_key,
                                            fetch_image, api_version=args.captcha_api)
        except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
            logger.error("Solving the captcha failed (%s) — continuing with "
                         "whatever the page holds.", e)
            return False
        submit = amazon_captcha_submit_url(amazon_challenge, solution)
        logger.info("Submitting the solution and returning to the page.")
        try:
            bridge.run(page.goto(submit, {"waitUntil": "domcontentloaded",
                                          "timeout": 60000}))
        except Exception as e:  # noqa: BLE001
            logger.error("Submitting the captcha solution failed (%s).", e)
            return False
        return True

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed = False
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break

        page_flow.wait_out_waf_challenge(d["content"], d["current_url"], d["sleep"])

        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = _content(session) or ""
        state = detect_page_state(html, url=page.url)

        if state == "throttled":
            for attempt in range(1, args.retries + 1):
                pause = args.retry_delay * (2 ** (attempt - 1))
                logger.warning("Amazon returned its 'Sorry, something went "
                               "wrong' throttle page — reloading in %.1fs "
                               "(%d/%d).", pause, attempt, args.retries)
                time.sleep(pause)
                try:
                    bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                               "timeout": 60000}))
                except Exception:  # noqa: BLE001
                    continue
                page_flow.wait_out_waf_challenge(d["content"], d["current_url"], d["sleep"])
                html = _content(session) or ""
                state = detect_page_state(html, url=page.url)
                if state != "throttled":
                    break

        if state in ("ok", "signin"):
            break
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    if state == "signin":
        logger.error("Amazon redirected to sign-in (%s). This page needs an "
                     "account; no proxy or captcha solve changes that.", page.url)
        outcome.needs_signin = True
        outcome.final_url = page.url
        return outcome

    if state == "ok":
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode)
        scrolls = page_flow.needs_scrolling(args.mode, page.url)
        session_budget = page_flow.session_attempts(args.mode, args.retries)
        for attempt in range(1, session_budget + 1):
            if scrolls:
                page_flow.hydrate(d["count"], d["page_height"],
                                  d["scroll_to_bottom"], d["sleep"],
                                  d["scroll_into_view"], args.mode, selector,
                                  threshold,
                                  page_flow.hydrate_attempts(args.mode))
            try:
                bridge.run(page.waitForFunction(
                    f"() => document.querySelectorAll({selector!r}).length > {threshold}",
                    {"timeout": page_flow.content_timeout_ms(args.mode)}))
                time.sleep(0.5)
            except Exception:  # noqa: BLE001 — a timeout here is expected sometimes
                logger.warning("No content markers (%s) appeared in time.", selector)
            if scrolls:
                loaded = page_flow.scroll_until_stable(
                    d["count"], d["page_height"], d["scroll_to_bottom"],
                    d["sleep"], selector)
                logger.info("Scrolled until the count stopped growing: %d "
                            "match(es).", loaded)
            if d["count"](selector) > threshold:
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
                    bridge, page = session.bridge, session.page
                    d = _driver(session)
                    bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                               "timeout": 60000}))
                    page_flow.wait_out_waf_challenge(d["content"], d["current_url"],
                                                     d["sleep"])
                except Exception as e:  # noqa: BLE001
                    logger.warning("The fresh session could not load the page "
                                   "(%s) — parsing what is there.", e)
                    break
            else:
                logger.warning("Still nothing after %d session(s) — the run "
                               "will report what it has.", session_budget)
        html = _content(session) or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    vendor = detect_bot_challenge(html, url=page.url)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    products = _parse_for_mode(html, page.url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if products and args.mode == "listing":
        ranks = [p.position for p in products if p.position is not None]
        if ranks and listing_kind(page.url) == "bestsellers":
            span = max(ranks) - min(ranks) + 1
            if span > len(ranks):
                logger.warning("Best-seller page %d returned %d rows spanning "
                               "ranks %d-%d — %d card(s) never loaded.",
                               page_num, len(ranks), min(ranks), max(ranks),
                               span - len(ranks))
        priced = sum(1 for p in products if p.price is not None)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(products), 100.0 * priced / len(products))
        if priced * 2 < len(products):
            logger.warning("Fewer than half the rows on page %d carry a price "
                           "— re-run with --dump-html to check the snapshot.",
                           page_num)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    dedupe_key = "review_id" if args.mode == "reviews" else "sku"
    stop_reason = "single_page_mode" if args.mode != "listing" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else "needs_signin" if first.needs_signin
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None or first.needs_signin
        elif args.mode == "listing":
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            # Same check as the Playwright engine: the ?page=N convention is
            # only used when the site's own link agrees with it, so a cursor
            # or token in pagination cannot be silently papered over.
            planned = None
            if args.pages > 1:
                constructed = page_url(first.final_url or args.url, 2)
                href = _next_page_href(session)
                if href and not _same_url(href, constructed):
                    logger.info("The site's own next-page link (%s) is not "
                                "what the page convention would build (%s) — "
                                "following its links one page at a time.",
                                href, constructed)
                else:
                    if not href:
                        logger.warning(
                            "No pagination link matched %s on page 1 — falling "
                            "back to the URL convention. Amazon serves no "
                            "link[rel=next], so this is the expected path once "
                            "the anchor classes change.", NEXT_PAGE_SELECTOR)
                    planned = [page_url(first.final_url or args.url, n)
                               for n in range(2, args.pages + 1)]

            url = (planned[0] if planned else
                   (_next_page_href(session) or page_url(session.page.url, 2)))
            for page_num in range(2, args.pages + 1):
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()

                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else "needs_signin" if outcome.needs_signin
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None or outcome.needs_signin
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break

                if page_num < args.pages:
                    url = (planned[page_num - 1] if planned else
                           (_next_page_href(session)
                            or page_url(session.page.url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    if args.mode == "listing" and listing_kind(args.url) == "bestsellers":
        ranks = sorted(r.position for r in all_rows if r.position is not None)
        if ranks:
            missing = sorted(set(range(ranks[0], ranks[-1] + 1)) - set(ranks))
            if missing:
                logger.warning("Best-seller ranks %d-%d are missing from the "
                               "merged result — the output is short by %d "
                               "product(s).", missing[0], missing[-1], len(missing))

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
    p = argparse.ArgumentParser(
        description="Amazon scraper (pyppeteer edition). pyppeteer is "
                    "effectively unmaintained — playwright_scraper.py is the "
                    "primary engine.")
    p.add_argument("--url", default=None,
                   help="Amazon URL. Required, unless AMAZON_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product", "reviews"],
                   default="listing",
                   help="listing (default), product, or reviews. See the "
                        "README; --mode reviews reads the reviews rendered on "
                        "the /dp/ page, because /product-reviews/ needs an "
                        "account even for page 1.")
    p.add_argument("--category", default=None, help="Label to tag output rows with.")
    p.add_argument("--pages", type=int, default=1, help="Listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load, per throttle reload, and per "
                        "fresh session before giving up (default 3)")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="amazon_products", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and AMAZON_URL is not set in the environment "
                "or in .env.")
    if args.mode != "listing" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    if args.mode == "listing" and listing_kind(args.url) == "detail":
        p.error("--url is a product page (/dp/ASIN) but --mode is listing.")
    if args.mode in ("product", "reviews") and listing_kind(args.url) != "detail":
        p.error(f"--mode {args.mode} needs a product URL containing /dp/ASIN.")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
