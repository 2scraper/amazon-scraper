#!/usr/bin/env python3
"""
amazon-scraper — Selenium edition (secondary engine)
====================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  search results and best-seller grids
    --mode product              one /dp/{ASIN} page
    --mode reviews              the reviews rendered on /dp/{ASIN}

Two limits of this engine, stated here rather than left to be discovered.
Neither is a bug in this code and neither can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password. So --cdp-endpoint here works only for an endpoint that
    needs no credentials; a credentialed one is refused with exit 2 rather
    than connected to and silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=` accepts
    no credentials, and there is no equivalent of pyppeteer's
    `page.authenticate`. Credentials are stripped and a warning says so, so
    nobody believes a `user:pass` URL is doing something.

There is no --concurrency here either: parallel page fetching lives in the
Playwright engine.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.amazon.com/s?k=wireless+headphones" --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urlsplit, parse_qsl

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

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
logger = logging.getLogger("selenium_scraper")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


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

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and on Amazon a
    fresh browser is also the only thing that re-rolls the served page
    variant when a detail page came back without its reviews (page_flow).
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome" and Amazon serves a different page.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
_SCROLL_INTO_VIEW_JS = """
const el = document.querySelector(arguments[0]);
if (el) { el.scrollIntoView({block: 'center'}); return true; }
return false;
"""


def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def page_height():
        try:
            return int(driver.execute_script("return document.body.scrollHeight;") or 0)
        except (WebDriverException, JavascriptException, TypeError):
            return 0

    def scroll_to_bottom():
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except WebDriverException as e:
            logger.debug("scroll_to_bottom failed: %s", e)

    def scroll_into_view(selector):
        try:
            driver.execute_script(_SCROLL_INTO_VIEW_JS, selector)
        except WebDriverException as e:
            logger.debug("Could not scroll %s into view: %s", selector, e)

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # The AWS WAF challenge reloads the page from JavaScript, so a
            # snapshot can land on the document swap. None tells the caller to
            # skip a check rather than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    return {"count": count, "page_height": page_height,
            "scroll_to_bottom": scroll_to_bottom, "sleep": sleep,
            "scroll_into_view": scroll_into_view, "content": content,
            "current_url": current_url}


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
    """The site's own next-page link, resolved by the browser, or None.

    Reads the DOM's `.href` property, which is already absolute — the
    opposite of Playwright's get_attribute("href"), which returns the raw
    attribute. Kept explicit because the engines differ here.
    """
    try:
        return session.driver.execute_script(
            "const a = document.querySelector(arguments[0]);"
            "return a ? (a.href || a.getAttribute('href')) : null;",
            NEXT_PAGE_SELECTOR)
    except WebDriverException:
        return None


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    amazon_challenge = detect_amazon_captcha(html, d["current_url"]())
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
                # Fetched from inside the page, so it travels over the same
                # session and exit that was served the captcha. An answer to
                # an image fetched from elsewhere is rejected.
                data = driver.execute_async_script(
                    "const [url, done] = [arguments[0], arguments[1]];"
                    "fetch(url, {credentials: 'include'})"
                    "  .then(r => r.arrayBuffer())"
                    "  .then(b => done(Array.from(new Uint8Array(b))))"
                    "  .catch(() => done(null));", url)
                return bytes(data) if data else b""

            solution = solve_amazon_captcha(amazon_challenge, args.twocaptcha_key,
                                            fetch_image, api_version=args.captcha_api)
        except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
            logger.error("Solving the captcha failed (%s) — continuing with "
                         "whatever the page holds.", e)
            return False
        submit = amazon_captcha_submit_url(amazon_challenge, solution)
        logger.info("Submitting the solution and returning to the page.")
        try:
            driver.get(submit)
        except WebDriverException as e:
            logger.error("Submitting the captcha solution failed (%s).", e)
            return False
        return True

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
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
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break

        page_flow.wait_out_waf_challenge(d["content"], d["current_url"], d["sleep"])

        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = d["content"]() or ""
        state = detect_page_state(html, url=d["current_url"]())

        if state == "throttled":
            for attempt in range(1, args.retries + 1):
                pause = args.retry_delay * (2 ** (attempt - 1))
                logger.warning("Amazon returned its 'Sorry, something went "
                               "wrong' throttle page — reloading in %.1fs "
                               "(%d/%d).", pause, attempt, args.retries)
                time.sleep(pause)
                try:
                    session.driver.get(url)
                except (TimeoutException, WebDriverException):
                    continue
                page_flow.wait_out_waf_challenge(d["content"], d["current_url"],
                                                 d["sleep"])
                html = d["content"]() or ""
                state = detect_page_state(html, url=d["current_url"]())
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
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    if state == "signin":
        logger.error("Amazon redirected to sign-in (%s). This page needs an "
                     "account; no proxy or captcha solve changes that.",
                     d["current_url"]())
        outcome.needs_signin = True
        outcome.final_url = d["current_url"]()
        return outcome

    if state == "ok":
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode)
        scrolls = page_flow.needs_scrolling(args.mode, d["current_url"]())
        timeout_s = page_flow.content_timeout_ms(args.mode) / 1000.0
        session_budget = page_flow.session_attempts(args.mode, args.retries)
        for attempt in range(1, session_budget + 1):
            if scrolls:
                page_flow.hydrate(d["count"], d["page_height"],
                                  d["scroll_to_bottom"], d["sleep"],
                                  d["scroll_into_view"], args.mode, selector,
                                  threshold,
                                  page_flow.hydrate_attempts(args.mode))
            try:
                WebDriverWait(session.driver, timeout_s).until(
                    lambda _: d["count"](selector) > threshold)
                time.sleep(0.5)
            except TimeoutException:
                logger.warning("No content markers (%s) appeared within %.0fs.",
                               selector, timeout_s)
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
                                   "cookies are not ours to discard.")
                    break
                try:
                    session.relaunch()
                    d = _driver(session)
                    session.driver.get(url)
                    page_flow.wait_out_waf_challenge(d["content"],
                                                     d["current_url"], d["sleep"])
                except (TimeoutException, WebDriverException) as e:
                    logger.warning("The fresh session could not load the page "
                                   "(%s) — parsing what is there.", e)
                    break
            else:
                logger.warning("Still nothing after %d session(s) — the run "
                               "will report what it has.", session_budget)
        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    vendor = detect_bot_challenge(html, url=d["current_url"]())
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    products = _parse_for_mode(html, final_url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if products and args.mode == "listing":
        ranks = [p.position for p in products if p.position is not None]
        if ranks and listing_kind(final_url) == "bestsellers":
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
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


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

    session = None
    try:
        session = _Session(args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else "needs_signin" if first.needs_signin
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None or first.needs_signin
        elif args.mode == "listing":
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

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
                   (_next_page_href(session)
                    or page_url(session.driver.current_url, 2)))
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
                            or page_url(session.driver.current_url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()

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
        description="Amazon scraper (Selenium edition). Cannot authenticate a "
                    "proxy or a remote CDP endpoint — see the module "
                    "docstring; playwright_scraper.py is the primary engine.")
    p.add_argument("--url", default=None,
                   help="Amazon URL. Required, unless AMAZON_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product", "reviews"],
                   default="listing",
                   help="listing (default), product, or reviews. --mode "
                        "reviews reads the reviews rendered on the /dp/ page, "
                        "because /product-reviews/ needs an account even for "
                        "page 1.")
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
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows,Chrome,Desktop")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
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
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
