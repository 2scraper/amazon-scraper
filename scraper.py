"""
scraper.py
~~~~~~~~~~
High-level Amazon scraper API for the 2parser project.

Supports
--------
  • Single product page by ASIN or URL
  • Search results (with pagination)
  • Product reviews (with pagination)
  • Best Sellers page
  • Concurrent scraping with ThreadPoolExecutor

Usage
-----
    from scraper import AmazonScraper

    scraper = AmazonScraper(
        proxy_strings=["login:pass@1.2.3.4:8080"],
        captcha_api_key="YOUR_2CAPTCHA_KEY",   # optional – for CAPTCHA solving
        marketplace="com",                      # com | co.uk | de | fr | co.jp …
    )

    # Single product
    product = scraper.get_product("B08N5WRWNW")
    print(product.title, product.price)

    # Search
    results = scraper.search("wireless headphones", pages=3)
    for r in results:
        print(r.asin, r.title, r.price)

    # Reviews
    reviews = scraper.get_reviews("B08N5WRWNW", pages=5)

    # Bulk
    products = scraper.get_products_bulk(["B08N5WRWNW", "B07VGRJDFY"], workers=5)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlencode, urljoin, urlparse, quote

import requests

from http_client import AmazonHTTPClient, AmazonBlockedError, AmazonNotFoundError
from parser import (
    AmazonProductParser,
    AmazonSearchParser,
    AmazonReviewParser,
    AmazonBestSellersParser,
    ProductData,
    SearchResultItem,
    ReviewData,
    BestSellerItem,
)
from marketplace import Marketplace, resolve as resolve_marketplace, list_all as list_marketplaces
from proxy_manager import ProxyManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CAPTCHA solver via 2captcha.com
# ─────────────────────────────────────────────────────────────────────────────

class TwoCaptchaSolver:
    """
    Solve Amazon image CAPTCHAs using 2captcha.com API.
    Docs: https://2captcha.com/api-docs
    """

    SUBMIT_URL = "https://2captcha.com/in.php"
    RESULT_URL = "https://2captcha.com/res.php"

    def __init__(self, api_key: str):
        self._key = api_key

    def solve_image_captcha(self, image_url: str) -> Optional[str]:
        """
        Download captcha image and solve via 2captcha.
        Returns the solved text or None on failure.
        """
        try:
            img_resp = requests.get(image_url, timeout=15)
            img_resp.raise_for_status()
            import base64
            b64 = base64.b64encode(img_resp.content).decode()
        except Exception as exc:
            logger.error("Failed to download captcha image: %s", exc)
            return None

        try:
            submit = requests.post(
                self.SUBMIT_URL,
                data={"key": self._key, "method": "base64", "body": b64, "json": 1},
                timeout=15,
            )
            submit.raise_for_status()
            data = submit.json()
            if data.get("status") != 1:
                logger.error("2captcha submit error: %s", data)
                return None
            task_id = data["request"]
        except Exception as exc:
            logger.error("2captcha submit failed: %s", exc)
            return None

        # Poll for result (Amazon CAPTCHAs usually solved in 10-30s)
        for _ in range(20):
            time.sleep(5)
            try:
                result = requests.get(
                    self.RESULT_URL,
                    params={"key": self._key, "action": "get", "id": task_id, "json": 1},
                    timeout=10,
                )
                result.raise_for_status()
                data = result.json()
                if data.get("status") == 1:
                    logger.info("CAPTCHA solved: %s", data["request"])
                    return data["request"]
                if data.get("request") != "CAPCHA_NOT_READY":
                    logger.error("2captcha error: %s", data)
                    return None
            except Exception as exc:
                logger.warning("2captcha poll error: %s", exc)

        logger.error("CAPTCHA solving timed out")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main Scraper
# ─────────────────────────────────────────────────────────────────────────────

class AmazonScraper:
    """
    Amazon scraper с поддержкой прокси 2captcha.com и ротацией маркетплейсов.

    Прокси берутся из личного кабинета 2captcha.com в формате:
        http://host:port:login:password

    Пример использования
    --------------------
    # Один прокси
    scraper = AmazonScraper(proxy="http://1.2.3.4:8080:user:pass", marketplace="de")

    # Несколько прокси
    scraper = AmazonScraper(
        proxy_list=["http://1.2.3.4:8080:u:p", "http://5.6.7.8:3128:u:p"],
        marketplace="co.uk",
    )

    # Из файла (выгрузка из ЛК 2captcha)
    from proxy_manager import ProxyManager
    scraper = AmazonScraper(marketplace="de")
    scraper.proxy_manager = ProxyManager.from_file("proxies.txt")
    scraper.client.proxy_manager = scraper.proxy_manager
    """

    # For backwards compatibility — use marketplace.list_all() for the full list
    BASE_URLS = {m.tld: m.base_url for m in list_marketplaces()}

    def __init__(
        self,
        proxy: str = "",
        proxy_list: Optional[list[str]] = None,
        captcha_api_key: str = "",
        marketplace: str = "com",
        min_delay: float = 2.0,
        max_delay: float = 6.0,
    ):
        """
        Parameters
        ----------
        proxy : str
            Один прокси в формате 2captcha ЛК: http://host:port:login:password
            При каждом запросе используется этот прокси.
        proxy_list : list[str]
            Несколько прокси — ротируются round-robin.
            Можно загрузить из файла: ProxyManager.from_file("proxies.txt")
        captcha_api_key : str
            Ключ 2captcha.com для решения CAPTCHA (не для прокси).
        marketplace : str
            Маркетплейс: "de", "uk", "co.jp", "amazon.fr", и т.д.
            Полный список: python main.py --list-marketplaces
        """
        self._marketplace: Marketplace = resolve_marketplace(marketplace)
        self.base_url = self._marketplace.base_url
        self.captcha_api_key = captcha_api_key

        logger.info(
            "Marketplace: %s  (%s)  currency=%s",
            self._marketplace.name, self._marketplace.base_url, self._marketplace.currency,
        )

        # Proxy manager
        if proxy_list:
            self.proxy_manager = ProxyManager.from_list(proxy_list)
        elif proxy:
            self.proxy_manager = ProxyManager(proxy)
        else:
            self.proxy_manager = ProxyManager()

        if self.proxy_manager:
            logger.info("Proxies loaded: %s", self.proxy_manager.stats_summary())

        # HTTP client с языком маркетплейса
        self.client = AmazonHTTPClient(
            proxy_manager=self.proxy_manager if self.proxy_manager else None,
            min_delay=min_delay,
            max_delay=max_delay,
            max_retries_on_block=3,
            accept_language=self._marketplace.language,
        )

        # Parsers
        self._product_parser = AmazonProductParser()
        self._search_parser = AmazonSearchParser()
        self._review_parser = AmazonReviewParser()
        self._best_sellers_parser = AmazonBestSellersParser()

        # Optional CAPTCHA solver
        self._captcha_solver = TwoCaptchaSolver(captcha_api_key) if captcha_api_key else None

    # ------------------------------------------------------------------ #
    # URL builders
    # ------------------------------------------------------------------ #
    def _product_url(self, asin: str) -> str:
        return f"{self.base_url}/dp/{asin}"

    def _search_url(self, query: str, page: int = 1) -> str:
        params = {"k": query, "page": page, "ref": "sr_pg_" + str(page)}
        return f"{self.base_url}/s?{urlencode(params)}"

    def _reviews_url(self, asin: str, page: int = 1, sort_by: str = "recent") -> str:
        # sort_by: recent | helpful
        return (
            f"{self.base_url}/product-reviews/{asin}"
            f"?pageNumber={page}&sortBy={sort_by}&reviewerType=all_reviews"
        )

    # Amazon Best Sellers slug map (display name → URL path segment)
    BEST_SELLERS_SLUGS: dict = {
        "electronics":          "electronics",
        "computers":            "pc",
        "books":                "books",
        "clothing":             "fashion",
        "home":                 "garden",
        "kitchen":              "kitchen",
        "toys":                 "toys-and-games",
        "sports":               "sporting-goods",
        "beauty":               "beauty",
        "health":               "hpc",
        "tools":                "hi",
        "grocery":              "grocery",
        "movies":               "movies-tv",
        "music":                "music",
        "video games":          "videogames",
        "baby":                 "baby-products",
        "pet supplies":         "pet-supplies",
        "office products":      "office-products",
        "industrial":           "industrial",
        "automotive":           "automotive",
        "arts":                 "arts-crafts",
        "handmade":             "handmade",
    }

    def _best_sellers_url(self, department: str = "") -> str:
        """
        Build Best Sellers URL in Amazon's real format:
          https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/
        Falls back to the general Best Sellers page if department not found.
        """
        if not department:
            return f"{self.base_url}/gp/bestsellers/"

        slug = self.BEST_SELLERS_SLUGS.get(department.lower())
        if slug:
            # e.g. /Best-Sellers-Electronics/zgbs/electronics/
            display = department.replace(" ", "-").title()
            return f"{self.base_url}/Best-Sellers-{display}/zgbs/{slug}/"
        else:
            # User passed a raw slug like "software" or a full path
            clean = department.strip("/").lower().replace(" ", "-")
            return f"{self.base_url}/gp/bestsellers/{clean}/"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_product(self, asin_or_url: str) -> ProductData:
        """
        Scrape a single Amazon product.

        Parameters
        ----------
        asin_or_url : str
            10-character ASIN (e.g. "B08N5WRWNW") or full Amazon product URL.
            If a URL from a different marketplace is passed (e.g. amazon.co.uk),
            the request goes to that marketplace automatically.
        """
        if asin_or_url.startswith("http"):
            url = asin_or_url
            # Auto-switch base_url if the URL is from a different marketplace
            detected = resolve_marketplace(asin_or_url)
            if detected.base_url != self.base_url:
                logger.info(
                    "URL marketplace (%s) differs from configured (%s) — using URL's marketplace",
                    detected.name, self._marketplace.name,
                )
        else:
            url = self._product_url(asin_or_url)

        logger.info("Scraping product: %s", url)
        try:
            resp = self.client.get(url)
            product = self._product_parser.parse(resp.text, url=url)
            if product.parse_errors:
                logger.warning("Parse errors for %s: %s", url, product.parse_errors)
            return product
        except AmazonNotFoundError:
            logger.warning("Product not found: %s", url)
            return ProductData(url=url, parse_errors=["Product not found"])
        except AmazonBlockedError as exc:
            logger.error("Blocked scraping %s: %s", url, exc)
            return ProductData(url=url, parse_errors=[str(exc)])

    def get_products_bulk(
        self,
        asins: list[str],
        workers: int = 3,
    ) -> list[ProductData]:
        """
        Scrape multiple products concurrently.

        Parameters
        ----------
        asins : list[str]
            List of ASINs or URLs.
        workers : int
            Number of concurrent threads. Keep low (2-5) to avoid bans.
        """
        results = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.get_product, asin): asin for asin in asins}
            for future in as_completed(futures):
                asin = futures[future]
                try:
                    product = future.result()
                    results.append(product)
                    logger.info("✓ %s – %s (%.2f)", product.asin, product.title[:40], product.price or 0)
                except Exception as exc:
                    logger.error("✗ Failed %s: %s", asin, exc)
                    results.append(ProductData(url=asin, parse_errors=[str(exc)]))
        return results

    def search(
        self,
        query: str,
        pages: int = 1,
        sort_by: str = "",       # featured | price_asc | price_desc | review_rank | date_rank
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        debug_html_path: str = "",
        fill_missing_prices: bool = False,
        fill_workers: int = 3,
    ) -> list[SearchResultItem]:
        """
        Scrape Amazon search results.

        Parameters
        ----------
        query : str
            Search query string.
        pages : int
            Number of result pages to scrape (each page ~20 results).
        fill_missing_prices : bool
            Для товаров без цены (вариативные позиции, "Weitere Optionen")
            делает дополнительный запрос на страницу /dp/ASIN и берёт цену оттуда.
        fill_workers : int
            Число параллельных потоков для дозаполнения цен (default: 3).
        debug_html_path : str
            Save raw HTML of page 1 to this path for DOM inspection.
        """
        all_results: list[SearchResultItem] = []

        if not self.proxy_manager:
            logger.warning(
                "No proxies configured — Amazon search pages block direct requests quickly.\n"
                "  Add proxies: --proxies proxies.txt  or  --2captcha-key KEY --fetch-proxies"
            )

        for page in range(1, pages + 1):
            url = self._build_search_url(query, page, sort_by, min_price, max_price)
            logger.info("Scraping search page %d: %s", page, url)
            try:
                resp = self.client.get(url)
                if debug_html_path and page == 1:
                    import pathlib
                    pathlib.Path(debug_html_path).write_text(resp.text, encoding="utf-8")
                    logger.info("Raw HTML saved → %s  (%d chars)", debug_html_path, len(resp.text))
                items = self._search_parser.parse(resp.text, base_url=self.base_url)
                if not items:
                    logger.info("No results on page %d, stopping", page)
                    break
                all_results.extend(items)
                logger.info("Page %d: %d results", page, len(items))
            except AmazonBlockedError as exc:
                logger.error("Blocked on search page %d: %s", page, exc)
                break
            except Exception as exc:
                logger.error("Error on search page %d: %s", page, exc)
                break

        if fill_missing_prices:
            all_results = self._fill_prices(all_results, workers=fill_workers)

        return all_results

    def _fill_prices(
        self,
        items: list[SearchResultItem],
        workers: int = 3,
    ) -> list[SearchResultItem]:
        """
        Для позиций без цены запрашивает страницу товара и берёт цену оттуда.
        Типичный кейс: вариативные товары (iPhone разных цветов/объёмов),
        где Amazon показывает "Weitere Optionen" вместо единой цены.
        """
        # Дедупликация — несколько строк с одним ASIN берём один раз
        missing = list({
            item.asin: item
            for item in items
            if not item.price and item.asin
        }.values())

        if not missing:
            return items

        logger.info(
            "fill_missing_prices: %d позиций без цены → запрашиваем страницы товаров",
            len(missing),
        )

        # Параллельно получаем цены
        asin_to_price: dict[str, tuple[Optional[float], str]] = {}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.get_product, item.asin): item.asin
                for item in missing
            }
            for future in as_completed(futures):
                asin = futures[future]
                try:
                    product = future.result()
                    if product.price:
                        asin_to_price[asin] = (product.price, product.currency)
                        logger.info(
                            "  ✓  %s → %.2f %s",
                            asin, product.price, product.currency,
                        )
                    else:
                        logger.debug("  –  %s: цена не найдена на странице товара", asin)
                except Exception as exc:
                    logger.warning("  ✗  %s: %s", asin, exc)

        # Подставляем найденные цены
        filled = 0
        for item in items:
            if not item.price and item.asin in asin_to_price:
                item.price, item.currency = asin_to_price[item.asin]
                filled += 1

        logger.info(
            "fill_missing_prices: заполнено %d/%d позиций",
            filled, len(missing),
        )
        return items

    def _build_search_url(
        self,
        query: str,
        page: int,
        sort_by: str,
        min_price: Optional[int],
        max_price: Optional[int],
    ) -> str:
        params: dict = {"k": query, "page": page}
        sort_map = {
            "featured": "relevanceblender",
            "price_asc": "price-asc-rank",
            "price_desc": "price-desc-rank",
            "review_rank": "review-rank",
            "date_rank": "date-desc-rank",
        }
        if sort_by and sort_by in sort_map:
            params["s"] = sort_map[sort_by]
        if min_price:
            params["low-price"] = min_price
        if max_price:
            params["high-price"] = max_price
        return f"{self.base_url}/s?{urlencode(params)}"

    def get_reviews(
        self,
        asin: str,
        pages: int = 1,
        sort_by: str = "recent",
    ) -> list[ReviewData]:
        """
        Scrape product reviews.

        Parameters
        ----------
        asin : str
            Product ASIN.
        pages : int
            Number of review pages (each page has ~10 reviews).
        sort_by : str
            "recent" or "helpful".
        """
        all_reviews: list[ReviewData] = []

        for page in range(1, pages + 1):
            url = self._reviews_url(asin, page, sort_by)
            logger.info("Scraping reviews page %d for %s", page, asin)
            try:
                resp = self.client.get(url)
                reviews = self._review_parser.parse(resp.text)
                if not reviews:
                    logger.info("No reviews on page %d, stopping", page)
                    break
                all_reviews.extend(reviews)
            except AmazonBlockedError as exc:
                logger.error("Blocked on review page %d: %s", page, exc)
                break
            except Exception as exc:
                logger.error("Error on review page %d: %s", page, exc)
                break

        return all_reviews

    def get_best_sellers(
        self,
        department: str = "",
        debug_html_path: str = "",
    ) -> list[BestSellerItem]:
        """
        Scrape Amazon Best Sellers page.

        Parameters
        ----------
        department : str
            Known names: "Electronics", "Books", "Computers", "Clothing",
            "Home", "Kitchen", "Toys", "Sports", "Beauty", "Health",
            "Video Games", "Grocery", "Movies", "Music", "Automotive", etc.
            Or pass a raw Amazon department slug like "software".
        debug_html_path : str
            If set, the raw response HTML is saved to this path so you can
            inspect the DOM and report selector changes (e.g. "debug.html").
        """
        url = self._best_sellers_url(department)
        logger.info("Scraping Best Sellers: %s", url)
        try:
            resp = self.client.get(url)

            if debug_html_path:
                import pathlib
                pathlib.Path(debug_html_path).write_text(resp.text, encoding="utf-8")
                logger.info(
                    "Raw HTML saved → %s  (%d chars, status=%s)",
                    debug_html_path, len(resp.text), resp.status_code,
                )

            items = self._best_sellers_parser.parse(resp.text, base_url=self.base_url)

            if not items:
                logger.warning(
                    "Best Sellers returned 0 items for department=%r  url=%s\n"
                    "  → Re-run with --debug-html debug.html to inspect the raw page.\n"
                    "  → Known departments: %s",
                    department, url,
                    ", ".join(self.BEST_SELLERS_SLUGS.keys()),
                )
            return items

        except AmazonNotFoundError:
            logger.error(
                "Best Sellers page not found: %s\n  Known: %s",
                url, ", ".join(self.BEST_SELLERS_SLUGS.keys()),
            )
            return []
        except AmazonBlockedError as exc:
            logger.error("Blocked on Best Sellers: %s", exc)
            return []
        except Exception as exc:
            logger.error("Failed to scrape Best Sellers: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # Diagnostics / info
    # ------------------------------------------------------------------ #
    def proxy_stats(self) -> dict:
        return self.proxy_manager.stats_summary()

    def marketplace_info(self) -> dict:
        """Return metadata about the current marketplace."""
        m = self._marketplace
        return {
            "tld": m.tld,
            "name": m.name,
            "base_url": m.base_url,
            "currency": m.currency,
            "language": m.language,
            "country_code": m.country_code,
        }

    @staticmethod
    def available_marketplaces() -> list[dict]:
        """Return all supported marketplaces as a list of dicts."""
        return [
            {"tld": m.tld, "name": m.name, "base_url": m.base_url, "currency": m.currency}
            for m in list_marketplaces()
        ]
