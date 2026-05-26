"""
parser.py
~~~~~~~~~
Parse Amazon product, search, and review pages with lxml (fast) and
fallback to css-selectors for resilience against DOM changes.

Extracted fields
----------------
Product page
  title, brand, asin, price, currency, rating, reviews_count,
  availability, images (list), description, bullets (list),
  category_breadcrumb (list), seller_name, fulfilled_by_amazon,
  variations (color/size/etc.), deals, prime_eligible

Search results
  asin, title, price, rating, reviews_count, url, sponsored

Reviews
  author, rating, title, date, body, verified_purchase, helpful_votes
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from lxml import html as lxhtml
from lxml.html import HtmlElement

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProductData:
    asin: str = ""
    title: str = ""
    brand: str = ""
    price: Optional[float] = None
    currency: str = ""
    list_price: Optional[float] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    availability: str = ""
    prime_eligible: bool = False
    fulfilled_by_amazon: bool = False
    seller_name: str = ""
    images: list = field(default_factory=list)
    bullets: list = field(default_factory=list)
    description: str = ""
    category_breadcrumb: list = field(default_factory=list)
    variations: dict = field(default_factory=dict)
    deals: str = ""
    url: str = ""
    parse_errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResultItem:
    asin: str = ""
    title: str = ""
    price: Optional[float] = None
    currency: str = ""
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    url: str = ""
    sponsored: bool = False
    prime_eligible: bool = False
    image_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewData:
    review_id: str = ""
    author: str = ""
    rating: Optional[float] = None
    title: str = ""
    date: str = ""
    body: str = ""
    verified_purchase: bool = False
    helpful_votes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _text(element: HtmlElement, xpath: str, default: str = "") -> str:
    """Extract and strip text from first matching node."""
    nodes = element.xpath(xpath)
    for node in nodes:
        text = node.strip() if isinstance(node, str) else (node.text_content() or "").strip()
        if text:
            return text
    return default


def _parse_price(text: str) -> tuple[Optional[float], str]:
    """
    Parse Amazon prices in any locale format.

    Handles all Amazon marketplace formats:
      US/CA/UK  $1,299.99  £1,299.99  →  dot = decimal, comma = thousands
      DE/FR/IT  € 1.299,99            →  comma = decimal, dot = thousands
      BR/PL     R$ 1.299,99  1 299,99 →  comma = decimal
      JP        ¥12,999               →  integer (no decimal)
      IN        ₹1,12,999.00          →  lakh format, dot = decimal
      SE        1 299 kr              →  space = thousands, integer
    """
    if not text:
        return None, ""

    text = text.strip().replace("\xa0", " ").replace("\u202f", " ")

    # Extract currency symbol or ISO code
    _CURRENCY_RE = re.compile(
        r"(R\$|[€£¥₹₺$]|USD|EUR|GBP|CAD|AUD|JPY|INR|BRL|MXN|SEK|"
        r"PLN|DKK|NOK|CHF|SGD|AED|SAR|TRY|EGP|CA\$|A\$|kr|zł|zl)",
        re.IGNORECASE,
    )
    _SYM_MAP = {
        "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
        "₺": "TRY", "$": "USD", "R$": "BRL", "CA$": "CAD", "A$": "AUD",
        "kr": "SEK", "zł": "PLN", "zl": "PLN",
    }
    cur_m = _CURRENCY_RE.search(text)
    currency = cur_m.group(1) if cur_m else ""
    currency = _SYM_MAP.get(currency, currency)

    # Keep only digits, dots and commas
    num = re.sub(r"[^\d,.]", "", text)
    if not num:
        return None, currency

    last_comma = num.rfind(",")
    last_dot   = num.rfind(".")
    after_comma = num[last_comma + 1:] if last_comma != -1 else ""
    after_dot   = num[last_dot   + 1:] if last_dot   != -1 else ""

    if last_comma > last_dot and len(after_comma) <= 2:
        # European format: comma = decimal  (€ 1.299,99 → 1299.99)
        normalized = num.replace(".", "").replace(",", ".")
    elif last_dot > last_comma and len(after_dot) <= 2:
        # US/UK format:    dot = decimal    ($1,299.99 → 1299.99)
        normalized = num.replace(",", "")
    elif last_dot != -1 and last_comma == -1 and len(after_dot) > 2:
        # Dot as thousands only (¥12.999 → 12999)
        normalized = num.replace(".", "")
    elif last_comma != -1 and last_dot == -1 and len(after_comma) > 2:
        # Comma as thousands only ($12,999 integer → 12999)
        normalized = num.replace(",", "")
    else:
        # Integer or ambiguous — strip all separators
        normalized = re.sub(r"[,.]", "", num)

    try:
        return float(normalized), currency
    except ValueError:
        return None, currency


def _parse_rating(text: str) -> Optional[float]:
    # Парсит рейтинг на любом языке Amazon:
    #   EN "4.4 out of 5 stars"   DE "4,4 von 5 Sternen"
    #   FR "4,4 sur 5 etoiles"    IT "4,4 su 5 stelle"
    #   ES "4,4 de un maximo de 5" JP "5つ星のうち4.4"
    #   TR "5 yildiz uzerinden 4,4" SE "4,4 pa 5 stjarnor"
    if not text:
        return None

    # Универсальный: "X,X слово 5" или "X.X word 5"
    m = re.search(r"(\d+[.,]\d+)\s+\S+\s+5(?:[^.,\d]|$)", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # EN: "4.4 out of 5"
    m = re.search(r"([\d.]+)\s+out\s+of\s+5", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # JP/TR: число ПОСЛЕ "5..." — "5つ星のうち4.4"
    m = re.search(r"5[^\d]+(\d+[.,]\d+)", text)
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
            if 1.0 <= v <= 5.0:
                return v
        except ValueError:
            pass

    # Последний шанс: первое X.X / X,X в [1-5]
    m = re.search(r"\b([1-5][.,]\d)\b", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    return None


def _parse_int(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_asin_from_url(url: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# Product page parser
# ─────────────────────────────────────────────────────────────────────────────

class AmazonProductParser:

    def parse(self, html_content: str, url: str = "") -> ProductData:
        product = ProductData(url=url, asin=_extract_asin_from_url(url))
        try:
            tree = lxhtml.fromstring(html_content)
        except Exception as exc:
            product.parse_errors.append(f"HTML parse failed: {exc}")
            return product

        self._parse_title(tree, product)
        self._parse_brand(tree, product)
        self._parse_price(tree, product)
        self._parse_rating(tree, product)
        self._parse_availability(tree, product)
        self._parse_seller(tree, product)
        self._parse_images(tree, product)
        self._parse_bullets(tree, product)
        self._parse_description(tree, product)
        self._parse_breadcrumb(tree, product)
        self._parse_variations(tree, product)
        self._parse_deals(tree, product)
        self._parse_prime(tree, product)

        return product

    # ---- individual field parsers ---------------------------------------- #

    def _parse_title(self, tree: HtmlElement, p: ProductData) -> None:
        selectors = [
            '//span[@id="productTitle"]//text()',
            '//h1[@id="title"]//text()',
        ]
        for sel in selectors:
            t = " ".join(s.strip() for s in tree.xpath(sel) if s.strip())
            if t:
                p.title = t
                return
        p.parse_errors.append("title not found")

    def _parse_brand(self, tree: HtmlElement, p: ProductData) -> None:
        # Method 1: brand byline link
        brand = _text(tree, '//a[@id="bylineInfo"]')
        if brand:
            brand = re.sub(r"^(Brand:|Visit the|Store)", "", brand, flags=re.I).strip()
            p.brand = brand
            return
        # Method 2: table row
        brand = _text(tree, '//tr[th[contains(., "Brand")]]/td')
        if brand:
            p.brand = brand

    def _parse_price(self, tree: HtmlElement, p: ProductData) -> None:
        price_selectors = [
            '//span[@id="priceblock_ourprice"]',
            '//span[@id="priceblock_dealprice"]',
            '//span[contains(@class,"a-price-whole")]/..',  # assembled price
            '//span[@class="a-offscreen"]',
            '//span[@id="price_inside_buybox"]',
        ]
        for sel in price_selectors:
            raw = _text(tree, sel + "//text()")
            if raw:
                p.price, p.currency = _parse_price(raw)
                if p.price:
                    break

        # List/was-price
        list_raw = _text(tree, '//span[@class="a-price a-text-price"]//text()')
        if list_raw:
            p.list_price, _ = _parse_price(list_raw)

    def _parse_rating(self, tree: HtmlElement, p: ProductData) -> None:
        # Rating
        rating_raw = _text(tree, '//span[@id="acrPopover"]/@title')
        if not rating_raw:
            rating_raw = _text(tree, '//i[contains(@class,"a-icon-star")]//text()')
        p.rating = _parse_rating(rating_raw)

        # Review count
        count_raw = _text(tree, '//span[@id="acrCustomerReviewText"]//text()')
        p.reviews_count = _parse_int(count_raw)

    def _parse_availability(self, tree: HtmlElement, p: ProductData) -> None:
        avail = _text(tree, '//div[@id="availability"]//text()')
        p.availability = avail or _text(tree, '//div[@id="outOfStock"]//text()')

    def _parse_seller(self, tree: HtmlElement, p: ProductData) -> None:
        seller = _text(tree, '//a[@id="sellerProfileTriggerId"]//text()')
        p.seller_name = seller

        # Fulfilled by Amazon?
        fba_text = _text(tree, '//div[@id="fulfilledByThirdParty"]//text()')
        p.fulfilled_by_amazon = not bool(fba_text) and "Amazon" in _text(
            tree, '//div[@id="merchant-info"]//text()'
        )

    def _parse_images(self, tree: HtmlElement, p: ProductData) -> None:
        # Amazon stores images in a JS data block
        scripts = tree.xpath('//script[contains(., "ImageBlockATF")]//text()')
        for script in scripts:
            # Extract JSON embedded in JS
            matches = re.findall(r'"hiRes"\s*:\s*"(https://[^"]+)"', script)
            if matches:
                p.images = list(dict.fromkeys(matches))  # unique, order-preserving
                return

        # Fallback: img tags in image block
        img_nodes = tree.xpath('//div[@id="imgTagWrapperId"]//img/@src')
        p.images = [src for src in img_nodes if src.startswith("https")]

    def _parse_bullets(self, tree: HtmlElement, p: ProductData) -> None:
        bullets = tree.xpath('//div[@id="feature-bullets"]//li//text()')
        cleaned = [b.strip() for b in bullets if b.strip() and len(b.strip()) > 3]
        p.bullets = cleaned

    def _parse_description(self, tree: HtmlElement, p: ProductData) -> None:
        # Some products use productDescription div, others use aplus content
        desc = " ".join(
            tree.xpath('//div[@id="productDescription"]//p//text()')
        ).strip()
        if not desc:
            desc = " ".join(
                tree.xpath('//div[@id="aplus"]//p//text()')
            ).strip()
        p.description = desc

    def _parse_breadcrumb(self, tree: HtmlElement, p: ProductData) -> None:
        crumbs = tree.xpath('//div[@id="wayfinding-breadcrumbs_feature_div"]//a//text()')
        p.category_breadcrumb = [c.strip() for c in crumbs if c.strip()]

    def _parse_variations(self, tree: HtmlElement, p: ProductData) -> None:
        """
        Variations (color, size, etc.) are stored in a JS dataToReturn object.
        We extract the dimensionToAsinMap to get ASIN → name mapping.
        """
        scripts = tree.xpath('//script[contains(., "dimensionToAsinMap")]//text()')
        for script in scripts:
            m = re.search(r'"variationValues"\s*:\s*(\{.*?\})', script, re.DOTALL)
            if m:
                try:
                    p.variations = json.loads(m.group(1))
                    return
                except json.JSONDecodeError:
                    pass

    def _parse_deals(self, tree: HtmlElement, p: ProductData) -> None:
        deal = _text(tree, '//span[@id="dealprice_savings"]//text()')
        if not deal:
            deal = _text(tree, '//span[contains(@class,"savingPriceOverride")]//text()')
        p.deals = deal

    def _parse_prime(self, tree: HtmlElement, p: ProductData) -> None:
        prime_nodes = tree.xpath('//*[contains(@class,"a-icon-prime")]')
        p.prime_eligible = bool(prime_nodes)


# ─────────────────────────────────────────────────────────────────────────────
# Search results parser
# ─────────────────────────────────────────────────────────────────────────────

class AmazonSearchParser:

    BASE_URL = "https://www.amazon.com"

    def parse(self, html_content: str, base_url: str = "") -> list[SearchResultItem]:
        base = base_url or self.BASE_URL
        try:
            tree = lxhtml.fromstring(html_content)
        except Exception as exc:
            logger.error("Search HTML parse failed: %s", exc)
            return []

        results = []
        cards = tree.xpath('//div[@data-asin and @data-component-type="s-search-result"]')
        for card in cards:
            item = self._parse_card(card, base)
            if item.asin:
                results.append(item)
        return results

    def _parse_card(self, card: HtmlElement, base: str = "") -> SearchResultItem:
        base = base or self.BASE_URL
        item = SearchResultItem()
        item.asin = card.get("data-asin", "")

        # Title
        item.title = _text(card, './/h2//span//text()') or _text(card, './/h2//a//text()')

        # URL — href может быть относительным (/dp/...) на любом маркетплейсе
        href = card.xpath('.//h2//a/@href')
        if href:
            h = href[0]
            item.url = h if h.startswith("http") else (base + h)
        else:
            item.url = f"{base}/dp/{item.asin}" if item.asin else ""

        # Price
        price_raw = _text(card, './/span[@class="a-offscreen"]//text()')
        item.price, item.currency = _parse_price(price_raw)

        # Rating
        rating_raw = _text(card, './/span[@class="a-icon-alt"]//text()')
        item.rating = _parse_rating(rating_raw)

        # Reviews count
        reviews_raw = _text(card, './/span[@data-component-type="s-client-side-analytics"]//text()')
        item.reviews_count = _parse_int(reviews_raw)

        # Sponsored
        sponsored_label = card.xpath('.//*[contains(text(),"Sponsored")]')
        item.sponsored = bool(sponsored_label)

        # Prime
        item.prime_eligible = bool(card.xpath('.//*[contains(@class,"a-icon-prime")]'))

        # Image
        img = card.xpath('.//img[contains(@class,"s-image")]/@src')
        item.image_url = img[0] if img else ""

        return item


# ─────────────────────────────────────────────────────────────────────────────
# Reviews parser
# ─────────────────────────────────────────────────────────────────────────────

class AmazonReviewParser:

    def parse(self, html_content: str) -> list[ReviewData]:
        try:
            tree = lxhtml.fromstring(html_content)
        except Exception as exc:
            logger.error("Reviews HTML parse failed: %s", exc)
            return []

        reviews = []
        cards = tree.xpath('//div[@data-hook="review"]')
        for card in cards:
            review = self._parse_card(card)
            if review.body:
                reviews.append(review)
        return reviews

    def _parse_card(self, card: HtmlElement) -> ReviewData:
        r = ReviewData()
        r.review_id = card.get("id", "")
        r.author = _text(card, './/span[@class="a-profile-name"]//text()')
        r.title = _text(card, './/a[@data-hook="review-title"]//span//text()')

        rating_raw = _text(card, './/i[@data-hook="review-star-rating"]//span//text()')
        r.rating = _parse_rating(rating_raw)

        r.date = _text(card, './/span[@data-hook="review-date"]//text()')
        r.body = " ".join(
            t.strip() for t in card.xpath('.//span[@data-hook="review-body"]//text()')
            if t.strip()
        )

        verified = card.xpath('.//*[@data-hook="avp-badge"]')
        r.verified_purchase = bool(verified)

        helpful_raw = _text(card, './/span[@data-hook="helpful-vote-statement"]//text()')
        r.helpful_votes = _parse_int(helpful_raw) or 0

        return r


# ─────────────────────────────────────────────────────────────────────────────
# Best Sellers parser  (zgbs / gp/bestsellers pages)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BestSellerItem:
    rank: int = 0
    asin: str = ""
    title: str = ""
    price: Optional[float] = None
    currency: str = ""
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    image_url: str = ""
    url: str = ""
    prime_eligible: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class AmazonBestSellersParser:
    """
    Parses Amazon Best Sellers pages (/gp/bestsellers/, /zgbs/).

    Confirmed working against real DOM (May 2025):
        div[id="p13n-asin-index-N"]          — outer card for item N (0-indexed)
          div[data-asin="B0..."]             — ASIN on inner wrapper
            span.zg-bdg-text                 — rank badge "#1"
            div.zg-grid-general-faceout
              div.p13n-sc-uncoverable-faceout[id="ASIN"]
                img.p13n-sc-dynamic-image    — product image
                div._cDEzb_p13n-sc-css-line-clamp-3_* — title (hashed class)
                span.a-icon-alt              — "4.4 out of 5 stars"
                span.a-size-small            — "275,783"
                span._cDEzb_p13n-sc-price_* — price (hashed class)

    Hashed CSS classes (_cDEzb_*) are matched via partial `contains()` on
    the stable portion of the class name that Amazon preserves across deploys.
    """

    BASE_URL = "https://www.amazon.com"

    def parse(self, html_content: str, base_url: str = "") -> list[BestSellerItem]:
        base = base_url or self.BASE_URL
        try:
            tree = lxhtml.fromstring(html_content)
        except Exception as exc:
            logger.error("Best Sellers HTML parse failed: %s", exc)
            return []

        # Strategy: try most-specific → broadest, pick highest yield
        strategies = [
            ("p13n-asin-index",  self._parse_p13n_index(tree, base)),
            ("zg-grid-faceout",  self._parse_zg_faceout(tree, base)),
            ("data-p13n-json",   self._parse_p13n_json_attr(tree, base)),
            ("zg-item-imm",      self._parse_zg_item_immersion(tree, base)),
        ]

        best_label, results = max(strategies, key=lambda x: len(x[1]))

        if not results:
            title = _text(tree, "//title//text()")
            data_asins = set(
                a for a in tree.xpath('//*//@data-asin')
                if len(a) == 10
            )
            logger.warning(
                "Best Sellers: 0 items parsed.\n"
                "  Page title    : %s\n"
                "  data-asin(10) : %d found\n"
                "  Run with --debug-html debug.html and open a bug report.",
                title[:100], len(data_asins),
            )
        else:
            logger.info("Best Sellers parsed %d items via strategy=%s", len(results), best_label)

        return results

    # ── Strategy 1: p13n-asin-index-N  (confirmed 2024-2025) ─────────────── #

    def _parse_p13n_index(self, tree, base: str) -> list[BestSellerItem]:
        """
        Primary strategy for current Amazon layout.
        Cards are div[id="p13n-asin-index-0"] … div[id="p13n-asin-index-N"].
        """
        cards = tree.xpath('//div[starts-with(@id,"p13n-asin-index-")]')
        return [item for item in (self._extract_p13n_card(c, base) for c in cards) if item]

    def _extract_p13n_card(self, card, base: str) -> Optional[BestSellerItem]:
        # ASIN — inner wrapper carries data-asin; faceout div has ASIN as its id
        asin = ""
        asin_nodes = card.xpath('.//*[@data-asin and string-length(@data-asin)=10]/@data-asin')
        if asin_nodes:
            asin = asin_nodes[0]
        if not asin:
            faceout = card.xpath('.//div[contains(@class,"p13n-sc-uncoverable-faceout")]/@id')
            asin = faceout[0] if faceout and len(faceout[0]) == 10 else ""
        if not asin:
            return None

        item = BestSellerItem(asin=asin)

        # Rank from badge
        rank_texts = card.xpath('.//span[contains(@class,"zg-bdg-text")]//text()')
        item.rank = _parse_int(rank_texts[0]) if rank_texts else 0

        # Title — hashed class always contains "p13n-sc-css-line-clamp"
        title_parts = card.xpath('.//*[contains(@class,"p13n-sc-css-line-clamp")]//text()')
        item.title = " ".join(t.strip() for t in title_parts if t.strip())

        # Fallback title from link title attribute or longest anchor text
        if not item.title:
            item.title = (
                _text(card, './/a/@title') or
                max((t.strip() for t in card.xpath('.//a//text()') if len(t.strip()) > 15),
                    key=len, default="")
            )

        # URL
        hrefs = card.xpath('.//a[contains(@href,"/dp/")]/@href')
        item.url = (base + hrefs[0]) if hrefs else f"{base}/dp/{asin}"

        # Price — hashed "p13n-sc-price" class is always present in price span
        price_texts = card.xpath('.//*[contains(@class,"p13n-sc-price")]//text()')
        price_raw = price_texts[0].replace("\xa0", " ").strip() if price_texts else ""
        if not price_raw:
            # fallback: a-color-price
            price_texts = card.xpath('.//*[contains(@class,"a-color-price")]//text()')
            price_raw = price_texts[0].replace("\xa0", " ").strip() if price_texts else ""
        item.price, item.currency = _parse_price(price_raw)

        # Rating from aria text inside star icon
        rating_texts = card.xpath('.//span[@class="a-icon-alt"]//text()')
        item.rating = _parse_rating(rating_texts[0] if rating_texts else "")

        # Reviews count — a-size-small after the star row
        for t in card.xpath('.//div[contains(@class,"a-icon-row")]//span[contains(@class,"a-size-small")]//text()'):
            n = _parse_int(t)
            if n and n > 5:
                item.reviews_count = n
                break

        # Image
        imgs = card.xpath('.//img[contains(@class,"p13n-sc-dynamic-image") or contains(@class,"p13n-product")]/@src')
        item.image_url = next((s for s in imgs if s.startswith("https")), "")

        item.prime_eligible = bool(card.xpath('.//*[contains(@class,"a-icon-prime")]'))
        return item

    # ── Strategy 2: zg-grid-general-faceout  (legacy / some markets) ─────── #

    def _parse_zg_faceout(self, tree, base: str) -> list[BestSellerItem]:
        items = []
        seen: set[str] = set()
        containers = tree.xpath('//div[contains(@class,"zg-grid-general-faceout")]')
        for container in containers:
            asin_nodes = container.xpath('.//*[@data-asin and string-length(@data-asin)=10]/@data-asin')
            asin = asin_nodes[0] if asin_nodes else ""
            if not asin:
                faceout = container.xpath('.//*[contains(@class,"p13n-sc-uncoverable-faceout")]/@id')
                asin = faceout[0] if faceout and len(faceout[0]) == 10 else ""
            if not asin or asin in seen:
                continue
            seen.add(asin)

            item = BestSellerItem(asin=asin)
            item.rank = len(items) + 1

            rank_texts = container.xpath('.//span[contains(@class,"zg-bdg-text")]//text()')
            if rank_texts:
                item.rank = _parse_int(rank_texts[0]) or item.rank

            title_parts = container.xpath('.//*[contains(@class,"p13n-sc-css-line-clamp")]//text()')
            item.title = " ".join(t.strip() for t in title_parts if t.strip())

            hrefs = container.xpath('.//a[contains(@href,"/dp/")]/@href')
            item.url = (base + hrefs[0]) if hrefs else f"{base}/dp/{asin}"

            price_texts = container.xpath('.//*[contains(@class,"p13n-sc-price")]//text()')
            price_raw = price_texts[0].replace("\xa0"," ").strip() if price_texts else ""
            item.price, item.currency = _parse_price(price_raw)

            rating_texts = container.xpath('.//span[@class="a-icon-alt"]//text()')
            item.rating = _parse_rating(rating_texts[0] if rating_texts else "")

            imgs = container.xpath('.//img[contains(@class,"p13n-sc-dynamic-image") or contains(@class,"p13n-product")]/@src')
            item.image_url = next((s for s in imgs if s.startswith("https")), "")

            items.append(item)

        items.sort(key=lambda x: x.rank or 999)
        return items

    # ── Strategy 3: data-p13n-asin-metadata JSON attribute  (2023 format) ── #

    def _parse_p13n_json_attr(self, tree, base: str) -> list[BestSellerItem]:
        items = []
        for card in tree.xpath('//*[@data-p13n-asin-metadata]'):
            raw = card.get("data-p13n-asin-metadata", "{}")
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            asin = meta.get("asin", "")
            if not asin or len(asin) != 10:
                continue
            item = BestSellerItem(asin=asin)
            item.rank = int(meta.get("index", len(items) + 1))
            item.title = meta.get("title", "") or _text(card, './/span//text()')
            item.url = f"{base}/dp/{asin}"
            price_raw = _text(card, './/*[contains(@class,"a-color-price")]//text()') or meta.get("price","")
            item.price, item.currency = _parse_price(price_raw)
            rating_raw = _text(card, './/span[@class="a-icon-alt"]//text()')
            item.rating = _parse_rating(rating_raw) or (float(meta["rating"]) if "rating" in meta else None)
            imgs = card.xpath('.//img/@src')
            item.image_url = next((s for s in imgs if s.startswith("https")), "")
            items.append(item)
        items.sort(key=lambda x: x.rank or 999)
        return items

    # ── Strategy 4: li.zg-item-immersion  (classic 2020-2022 layout) ─────── #

    def _parse_zg_item_immersion(self, tree, base: str) -> list[BestSellerItem]:
        items = []
        cards = tree.xpath(
            '//li[contains(@class,"zg-item-immersion")]'
            '//div[@data-asin and string-length(@data-asin)=10]'
        )
        for card in cards:
            asin = card.get("data-asin", "")
            if not asin:
                continue
            item = BestSellerItem(asin=asin)
            rank_raw = _text(card, './/span[contains(@class,"zg-bdg-pct")]//text()')
            item.rank = _parse_int(rank_raw) or (len(items) + 1)
            title_raw = _text(card, './/*[contains(@class,"p13n-sc-truncate")]//text()')
            item.title = title_raw
            hrefs = card.xpath('.//a[contains(@href,"/dp/")]/@href')
            item.url = (base + hrefs[0]) if hrefs else f"{base}/dp/{asin}"
            price_raw = _text(card, './/*[contains(@class,"a-color-price")]//text()')
            item.price, item.currency = _parse_price(price_raw)
            rating_raw = _text(card, './/span[@class="a-icon-alt"]//text()')
            item.rating = _parse_rating(rating_raw)
            imgs = card.xpath('.//img/@src')
            item.image_url = next((s for s in imgs if s.startswith("https")), "")
            items.append(item)
        return items
