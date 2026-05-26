"""
http_client.py
~~~~~~~~~~~~~~
Stealth HTTP client for Amazon with:
  - Rotating User-Agents (real browser fingerprints)
  - Accept-Language / sec-ch-ua headers matching UA
  - Exponential backoff + jitter on errors
  - Automatic proxy rotation on ban detection
  - CAPTCHA detection and reporting
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
    before_sleep_log,
)
from urllib3.util.retry import Retry

from proxy_manager import Proxy, ProxyManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Real browser fingerprints (UA + matching sec-ch-ua)
# Based on real-world browser telemetry, updated periodically
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_PROFILES = [
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"macOS"',
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        "sec_ch_ua": None,  # Firefox does not send sec-ch-ua
        "sec_ch_ua_platform": None,
        "accept_language": "en-US,en;q=0.5",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "sec_ch_ua": None,
        "sec_ch_ua_platform": None,
        "accept_language": "en-US,en;q=0.9",
    },
]

# Patterns that indicate Amazon blocked the request
BLOCK_PATTERNS = [
    r"Type the characters you see in this image",   # image CAPTCHA
    r"Enter the characters you see below",           # text CAPTCHA
    r"Sorry, we just need to make sure you",         # robot check
    r"To discuss automated access to Amazon",        # API notice
    r'id="captchacharacters"',                       # CAPTCHA field
    r"<title>Robot Check</title>",
    r"<title>Sorry! Something went wrong",
    r"automated access",
]

# Patterns that indicate a *product* ASIN doesn't exist (not a block, not a category miss)
NOT_FOUND_PATTERNS = re.compile(
    r"<title>Page Not Found|page-not-found|glowbird-not-found",
    re.IGNORECASE,
)

BLOCK_RE = re.compile("|".join(BLOCK_PATTERNS), re.IGNORECASE)


class AmazonBlockedError(Exception):
    """Raised when Amazon returns a block/CAPTCHA page."""


class AmazonNotFoundError(Exception):
    """Raised when product page returns 404 or not-found page."""


class AmazonHTTPClient:
    """
    Thread-safe HTTP client with proxy rotation and anti-bot measures.

    Parameters
    ----------
    proxy_manager : ProxyManager
        Pool of 2captcha proxies.
    request_timeout : tuple
        (connect_timeout, read_timeout) in seconds.
    min_delay, max_delay : float
        Random delay range between requests (seconds).
    max_retries_on_block : int
        How many times to retry with a new proxy on block detection.
    """

    def __init__(
        self,
        proxy_manager: Optional[ProxyManager] = None,
        request_timeout: tuple = (10, 30),
        min_delay: float = 2.0,
        max_delay: float = 6.0,
        max_retries_on_block: int = 3,
        accept_language: str = "",        # marketplace-specific e.g. "de-DE,de;q=0.9"
    ):
        self.proxy_manager = proxy_manager
        self.timeout = request_timeout
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries_on_block = max_retries_on_block
        self.accept_language = accept_language
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _build_session(self, profile: dict, accept_language: str = "") -> requests.Session:
        session = requests.Session()

        # urllib3 transport-level retry — only real server errors, NOT 503/429
        # Amazon uses 503 as a soft ban signal; auto-retrying it instantly
        # makes the ban worse. We handle 503/429 at the application level
        # with proper backoff and proxy rotation.
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=1.0,
                status_forcelist=[500, 502, 504],   # NOT 503 or 429
                allowed_methods=["GET"],
                raise_on_status=False,
            )
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        headers = {
            "User-Agent": profile["user_agent"],
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            # Use marketplace-specific language if provided, else profile default
            "Accept-Language": accept_language or profile["accept_language"],
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
            "Cache-Control": "max-age=0",
        }
        if profile.get("sec_ch_ua"):
            headers["sec-ch-ua"] = profile["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = "?0"
        if profile.get("sec_ch_ua_platform"):
            headers["sec-ch-ua-platform"] = profile["sec_ch_ua_platform"]

        session.headers.update(headers)
        return session

    def _throttle(self) -> None:
        """Enforce minimum inter-request delay with jitter."""
        elapsed = time.time() - self._last_request_time
        delay = random.uniform(self.min_delay, self.max_delay)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def _is_blocked(self, html: str) -> bool:
        return bool(BLOCK_RE.search(html))

    def _is_not_found(self, html: str, status_code: int, url: str = "") -> bool:
        # Hard 404 — always a not-found
        if status_code == 404:
            return True
        # "Page Not Found" title — only raise for product/review pages.
        # Category, Best Sellers, and search pages can return a quasi-404
        # when the department slug mismatches; we don't want to abort those.
        if NOT_FOUND_PATTERNS.search(html):
            if "/dp/" in url or "/product-reviews/" in url:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get(self, url: str, **kwargs) -> requests.Response:
        """
        Fetch a URL with stealth headers, proxy rotation, and block detection.

        Raises
        ------
        AmazonBlockedError  – after exhausting all proxy retries
        AmazonNotFoundError – 404 / product removed
        requests.RequestException – network-level failure
        """
        profile = random.choice(BROWSER_PROFILES)
        session = self._build_session(profile, self.accept_language)

        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries_on_block + 1):
            proxy: Optional[Proxy] = None
            proxies_dict: Optional[dict] = None

            if self.proxy_manager:
                proxy = self.proxy_manager.get()
                if proxy:
                    proxies_dict = proxy.as_dict()
                else:
                    logger.warning("No healthy proxy available – trying without proxy")

            self._throttle()

            try:
                resp = session.get(
                    url,
                    proxies=proxies_dict,
                    timeout=self.timeout,
                    allow_redirects=True,
                    **kwargs,
                )

                if self._is_not_found(resp.text, resp.status_code, url=url):
                    if proxy:
                        self.proxy_manager.report_success(proxy)
                    raise AmazonNotFoundError(f"Product not found: {url}")

                # 503 / 429 = rate-limited or soft-banned
                # Do NOT hammer; back off exponentially and rotate proxy
                if resp.status_code in (503, 429):
                    backoff = min(30, 8 * (2 ** attempt))   # 8s, 16s, 30s
                    logger.warning(
                        "HTTP %d on attempt %d/%d (rate-limited) – "
                        "backing off %.0fs then rotating proxy",
                        resp.status_code, attempt + 1,
                        self.max_retries_on_block + 1, backoff,
                    )
                    if proxy:
                        self.proxy_manager.report_ban(proxy)
                    last_exc = AmazonBlockedError(
                        f"HTTP {resp.status_code} (rate-limited, attempt {attempt+1})"
                    )
                    profile = random.choice(BROWSER_PROFILES)
                    session = self._build_session(profile, self.accept_language)
                    time.sleep(backoff + random.uniform(0, 5))
                    continue

                if self._is_blocked(resp.text):
                    logger.warning(
                        "CAPTCHA/block page on attempt %d/%d – proxy: %s",
                        attempt + 1, self.max_retries_on_block + 1, proxy,
                    )
                    if proxy:
                        self.proxy_manager.report_ban(proxy)
                    last_exc = AmazonBlockedError(f"Amazon blocked request (attempt {attempt+1})")
                    profile = random.choice(BROWSER_PROFILES)
                    session = self._build_session(profile, self.accept_language)
                    time.sleep(random.uniform(5, 15))
                    continue

                # Success
                if proxy:
                    self.proxy_manager.report_success(proxy)
                return resp

            except (AmazonNotFoundError, AmazonBlockedError):
                raise
            except requests.exceptions.ProxyError as exc:
                logger.warning("Proxy error with %s: %s", proxy, exc)
                if proxy:
                    self.proxy_manager.report_fail(proxy)
                last_exc = exc
            except requests.exceptions.ConnectionError as exc:
                logger.warning("Connection error: %s", exc)
                if proxy:
                    self.proxy_manager.report_fail(proxy)
                last_exc = exc
            except requests.exceptions.Timeout as exc:
                logger.warning("Timeout fetching %s via %s", url, proxy)
                if proxy:
                    self.proxy_manager.report_fail(proxy)
                last_exc = exc
            except requests.RequestException as exc:
                logger.error("Unexpected HTTP error: %s", exc)
                if proxy:
                    self.proxy_manager.report_fail(proxy)
                last_exc = exc

            time.sleep(random.uniform(3, 8))

        raise last_exc or AmazonBlockedError(f"Failed to fetch {url} after all retries")
