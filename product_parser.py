"""
product_parser.py
-------------------
Extracts rows from Amazon search pages, best-seller grids, product detail
pages and the reviews rendered on a detail page.

Why there is no JSON-LD path
----------------------------
Every other scraper in this family reads schema.org JSON-LD first and treats
CSS as a fallback, because structured data carries every product regardless
of what has painted. Amazon publishes none. Measured on live captures taken
2026-09-08 from a European exit IP:

    /s?k=wireless+headphones          0 <script type="application/ld+json">
    /zgbs/electronics/                0
    /dp/B07K5214NZ                    0

So the primary path here is the site's OWN data attribute, which is a better
anchor than any CSS class and nearly as good as structured data:

    search      div[data-component-type="s-search-result"][data-asin]
    bestsellers [id^="p13n-asin-index-"]  (rank order, #1..#50)

and the URL-pattern fallback (`/dp/{ASIN}`) runs only if those yield nothing.

Anchoring on `data-asin` rather than a class is the same decision the rest of
the family makes for the same reason: Amazon's own classes are partly
build-generated hashes (`_cDEzb_p13n-sc-price_3mJ9Z`), so where a class is
unavoidable this file matches on a substring (`[class*="p13n-sc-price"]`)
rather than the whole hashed name.

Why the product URL is reconstructed, not read
----------------------------------------------
A sponsored tile's link is not a product URL — it is a click tracker with the
product URL percent-encoded inside it:

    /sspa/click?ie=UTF8&spc=...&url=%2FCancelling-Headphones%2Fdp%2FB0C3HCD34R...

A `a[href*="/dp/"]` match therefore MISSES every sponsored tile (30 such
links on the captured page), and a naive read of the first href gives a
tracker as the row's `url`. Since the ASIN is on the tile itself and
`https://{host}/dp/{ASIN}` always resolves, the canonical URL is rebuilt from
the ASIN. That also makes `url` stable between runs, which the tracking
parameters (`qid`, `xpid`, `ref=sr_1_3`) are not — otherwise diff_runs.py
would report every row as changed on every run.

Field coverage, measured, so a null is not read as a bug
--------------------------------------------------------
One live page, /s?k=wireless+headphones, 22 organic tiles, European exit:

    sku / title / rating / review_count / image     22/22
    price                                           16/22   <- see below
    original_price (list price)                     13/22
    sponsored                                        6/22
    badge                                            1/22
    coupon                                           2/22
    brand                                            0/22

`price` is null on 6 of 22 tiles and that is not a parser failure: those
tiles (Sony WH-CH520, Bose QuietComfort and three siblings) render no price
node at all to this exit — Amazon withholds the price for some offers rather
than showing one. A price-monitoring consumer must expect roughly a quarter
of a page to have no price; the canary's threshold is set from this number,
not from an aspiration.

`brand` is null on EVERY listing tile. Amazon's search markup has no brand
line — the brand is inside the title text and nowhere else as a field.
Guessing it by splitting the title on the first word would be wrong often
enough to be worse than null. It IS populated by --mode product, from
`#bylineInfo`.

There is no `prime` column. Amazon rendered no Prime marker at all on any
captured tile from a cross-border exit (0 of 22 on .com, 0 of 16 on .de,
0 of 60 on .co.jp), so the column would be null on every row of every run —
a field that looks available and never is. If a domestic exit turns out to
render one, add it back with the measurement that justified it.
"""

import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote

from bs4 import BeautifulSoup

from output_writer import Product, Review, SOURCE_DEFAULT

logger = logging.getLogger(__name__)


SELECTORS = {
    # The fallback anchor: an Amazon product detail URL always contains
    # "/dp/{10-char ASIN}". Used by the engines as the "has the grid
    # painted?" probe as well, which is why it stays a plain CSS selector.
    "item_link": 'a[href*="/dp/"]',
    # The primary anchors — the site's own attributes, one per page kind.
    "search_tile": 'div[data-component-type="s-search-result"][data-asin]',
    "bestseller_card": '[id^="p13n-asin-index-"]',
    "review": 'div[data-hook="reviewContainer"]',
}


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------
# Amazon is one site behind 21 hostnames, and which one produced a row is not
# derivable from the ASIN — the same ASIN is sold on several marketplaces at
# different prices. The marketplace is taken from the URL being scraped
# rather than from a flag, so there is no way for a flag and a URL to
# disagree about which site a run is reading.
#
# `currency` here is the marketplace's OWN currency and is used for exactly
# one thing: disambiguating a bare local symbol (see _prices_in). It is NOT
# a default for the row — Amazon serves prices in the currency of the exit
# IP's delivery country, so a European exit reading amazon.co.jp is shown
# "EUR 25.34", not yen. Defaulting a currency from the domain would state a
# fact the page contradicts.
MARKETPLACES: Dict[str, Dict[str, str]] = {
    "amazon.com":    {"country": "US", "currency": "USD", "name": "United States"},
    "amazon.co.uk":  {"country": "GB", "currency": "GBP", "name": "United Kingdom"},
    "amazon.de":     {"country": "DE", "currency": "EUR", "name": "Germany"},
    "amazon.fr":     {"country": "FR", "currency": "EUR", "name": "France"},
    "amazon.it":     {"country": "IT", "currency": "EUR", "name": "Italy"},
    "amazon.es":     {"country": "ES", "currency": "EUR", "name": "Spain"},
    "amazon.nl":     {"country": "NL", "currency": "EUR", "name": "Netherlands"},
    "amazon.com.be": {"country": "BE", "currency": "EUR", "name": "Belgium"},
    "amazon.pl":     {"country": "PL", "currency": "PLN", "name": "Poland"},
    "amazon.se":     {"country": "SE", "currency": "SEK", "name": "Sweden"},
    "amazon.co.jp":  {"country": "JP", "currency": "JPY", "name": "Japan"},
    "amazon.ca":     {"country": "CA", "currency": "CAD", "name": "Canada"},
    "amazon.com.au": {"country": "AU", "currency": "AUD", "name": "Australia"},
    "amazon.in":     {"country": "IN", "currency": "INR", "name": "India"},
    "amazon.com.br": {"country": "BR", "currency": "BRL", "name": "Brazil"},
    "amazon.com.mx": {"country": "MX", "currency": "MXN", "name": "Mexico"},
    "amazon.sg":     {"country": "SG", "currency": "SGD", "name": "Singapore"},
    "amazon.ae":     {"country": "AE", "currency": "AED", "name": "UAE"},
    "amazon.sa":     {"country": "SA", "currency": "SAR", "name": "Saudi Arabia"},
    "amazon.com.tr": {"country": "TR", "currency": "TRY", "name": "Turkey"},
    "amazon.eg":     {"country": "EG", "currency": "EGP", "name": "Egypt"},
}


def marketplace_host(url: str) -> str:
    """The bare `amazon.<tld>` host of `url`, or the default.

    Strips a `www.` / `smile.` prefix so `source` is one value per
    marketplace rather than two spellings of the same one.
    """
    if not url:
        return SOURCE_DEFAULT
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return SOURCE_DEFAULT
    for prefix in ("www.", "smile.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host if host in MARKETPLACES else (host or SOURCE_DEFAULT)


def marketplace_info(url: str) -> Dict[str, str]:
    """Registry entry for `url`'s marketplace; empty dict if unknown.

    An unknown host is not an error: Amazon opens marketplaces faster than
    this table is updated, and a run against a hostname missing from it
    should still parse — it only loses the bare-symbol disambiguation.
    """
    return MARKETPLACES.get(marketplace_host(url), {})


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Currency, in descending trustworthiness (the family rule):
#   1. a written ISO code    "EUR 34.40"     — names itself, a fact
#   2. a prefixed symbol     "HK$1,234"      — nearly as good
#   3. a bare local symbol   "34,40 €"       — a guess, narrowed by the
#                                              marketplace when it maps
#   4. nothing               -> None, never a defaulted "USD"
#
# Amazon leans on form 1 far more than the rest of this family: served to a
# cross-border visitor it prints the ISO code rather than a symbol, on every
# marketplace ("EUR 25.34" on amazon.co.jp). Served domestically it prints
# the local symbol.
_CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
    "₹": "INR", "₺": "TRY", "₪": "ILS", "﷼": "SAR",
    # Deliberately ambiguous on their own — "kr" is SEK, NOK or DKK and "zł"
    # is unambiguous but non-ASCII. Both are resolved against the
    # marketplace when one is known; see _prices_in.
    "zł": "PLN",
}

# Symbols whose meaning depends on which marketplace rendered them. Listed
# separately so a match can be *declined* rather than guessed when the
# marketplace is unknown — reporting 199 kr as SEK on amazon.dk would be the
# wrong currency, not a rounding error.
#
# They must still be MATCHABLE, which is a separate thing from being
# resolvable: "kr" appears in no other table here, so before it was added to
# the alternation below, "199 kr" on amazon.se matched nothing at all and the
# tile was read as having no price — the same silent whole-locale data loss
# that a $-only pattern caused elsewhere in this family.
_MARKETPLACE_RESOLVED_SYMBOLS = {"kr", "kr.", "R$", "$"}

# A bare "$" is not enough on its own: several markets prefix it, and
# reporting HK$1,234 as 1234 USD is the wrong currency. Longest-first, so
# "HK$" is tried before "$" (see _PREFIXED_RE).
_PREFIXED_SYMBOLS = {
    "HK$": "HKD", "NZ$": "NZD", "AU$": "AUD", "CA$": "CAD", "US$": "USD",
    "A$": "AUD", "C$": "CAD", "S$": "SGD", "R$": "BRL", "NT$": "TWD",
    "Mex$": "MXN", "AED": "AED",
}

# An explicit allowlist, not a bare [A-Z]{3}: the latter matches any three
# capitals next to a number, so a size chart ("XXL 100") or a spec line
# ("USB 3") would start producing phantom prices. Every entry is a real ISO
# 4217 code, so a match names the currency as a fact rather than a guess.
_CURRENCY_CODES = frozenset("""
    USD EUR GBP JPY CHF AUD CAD NZD SGD HKD TWD KRW CNY MOP
    AED SAR QAR KWD BHD OMR JOD ILS TRY EGP MAD ZAR NGN KES
    SEK NOK DKK ISK PLN CZK HUF RON BGN HRK RSD UAH RUB
    INR IDR MYR THB PHP VND PKR LKR BDT KZT
    BRL MXN ARS CLP COP PEN UYU
""".split())

# Space characters used as a THOUSANDS separator. French, Polish, Swedish and
# several other locales group with a space, and a rendered page uses a
# no-break variant so the number does not wrap: a plain space, NBSP
# (U+00A0), narrow NBSP (U+202F) and thin space (U+2009) all appear. Amazon
# uses NBSP between the currency and the amount on every marketplace
# ("EUR 34.40", "179,99 €"), so missing these does not merely
# mis-group a number here — it fails to match the price at all.
_GROUP_SPACES = "    "

# Amount, in any of the three grouping conventions:
#   1,234.56 / 1.234,56 / 1 234,56 / 125 / 125.00
#
# The space-grouped form deliberately requires FULL groups of exactly three
# digits, so a stray "5 200" out of two unrelated numbers cannot merge.
_AMOUNT = (r"\d{1,3}(?:[" + _GROUP_SPACES + r"]\d{3})+(?:[.,]\d{1,2})?"
           r"|[\d.,]+(?:[.,]\d{1,2})?")
_PREFIXED_RE = "|".join(re.escape(s) for s in
                        sorted(_PREFIXED_SYMBOLS, key=len, reverse=True))
_BARE_RE = "|".join(re.escape(s) for s in sorted(
    set(_CURRENCY_SYMBOLS) | _MARKETPLACE_RESOLVED_SYMBOLS,
    key=len, reverse=True))
_SPACE = "[" + _GROUP_SPACES + "]?"
_PRICE_RE = re.compile(
    r"(?:(" + _PREFIXED_RE + r"|" + _BARE_RE + r")" + _SPACE + r"(" + _AMOUNT + r")"
    r"|(" + _AMOUNT + r")" + _SPACE + r"(" + _BARE_RE + r")"
    r"|\b([A-Z]{3})" + _SPACE + r"(" + _AMOUNT + r")"
    r"|\b(" + _AMOUNT + r")" + _SPACE + r"([A-Z]{3})\b)"
)

# The separator between an ISO code and the amount is OPTIONAL above, because
# a detail page renders "EUR19.61" with nothing between them while a listing
# tile renders "EUR 34.40". Making it optional widens what [A-Z]{3} can
# reach ("TRY2" in marketing copy would parse as 2 TRY), which is survivable
# only because every caller in this file hands _prices_in the text of a PRICE
# NODE (`.a-price .a-offscreen`, `#corePrice_feature_div`) and not the text
# of a whole tile. Whole-tile text is used on one path only, the URL-pattern
# fallback, and that path is documented as the weaker one.

_ASIN_RE = r"[A-Z0-9]{10}"
_ASIN_IN_URL_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/(" + _ASIN_RE + r")")


def _normalize_amount(raw: str) -> Optional[float]:
    """Parse a price amount written in either decimal convention.

    When BOTH separators appear, 'whichever comes last is the decimal point'
    disambiguates on its own. When only one appears, that is ambiguous
    between a thousands grouping and a decimal point — and no currency this
    parser recognises has a 3-digit subunit. So a single separator followed
    by exactly 3 digits is a thousands grouping; anything else is a decimal.
    """
    for space in _GROUP_SPACES:
        raw = raw.replace(space, "")

    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot != -1 and last_comma != -1:
        norm = (raw.replace(",", "") if last_dot > last_comma
                else raw.replace(".", "").replace(",", "."))
    else:
        sep_pos = max(last_dot, last_comma)
        trailing = raw[sep_pos + 1:] if sep_pos != -1 else ""
        if len(trailing) == 3 and trailing.isdigit():
            norm = raw.replace(".", "").replace(",", "")
        else:
            norm = raw.replace(",", ".")
    try:
        return float(norm)
    except ValueError:
        return None


def _prices_in(text: str, marketplace_currency: Optional[str] = None
               ) -> Tuple[List[float], Optional[str]]:
    """Return ([amounts], currency_code_or_None) for all prices in `text`.

    `marketplace_currency` resolves the symbols that cannot name themselves —
    "kr", "$", "R$" — using the marketplace whose page rendered them. That is
    still tier 3 (a guess), but a much better one: if the page printed a
    local symbol at all, the visitor is being served that market's own
    currency. When the marketplace is unknown, such a symbol yields a price
    with currency None rather than a plausible wrong code.
    """
    amounts, currency = [], None
    for m in _PRICE_RE.finditer(text):
        sym = m.group(1) or m.group(4)
        code = m.group(5) or m.group(8)
        if code and code not in _CURRENCY_CODES:
            # Three capitals next to a number that are not a real currency —
            # a size ("XXL 100"), a spec, a model name. Not a price.
            continue
        raw = m.group(2) or m.group(3) or m.group(6) or m.group(7)
        if currency is None:
            if code:
                currency = code
            elif sym in _PREFIXED_SYMBOLS and sym not in _MARKETPLACE_RESOLVED_SYMBOLS:
                currency = _PREFIXED_SYMBOLS[sym]
            elif sym in _MARKETPLACE_RESOLVED_SYMBOLS:
                currency = marketplace_currency or _PREFIXED_SYMBOLS.get(sym) \
                           or _CURRENCY_SYMBOLS.get(sym)
            else:
                currency = _CURRENCY_SYMBOLS.get(sym)
        amount = _normalize_amount(raw)
        if amount is not None:
            amounts.append(amount)
    return amounts, currency


def _first_price(node, marketplace_currency: Optional[str] = None
                 ) -> Tuple[Optional[float], Optional[str]]:
    """(amount, currency) from a price node's text, or (None, None)."""
    if node is None:
        return None, None
    amounts, currency = _prices_in(node.get_text(" ", strip=True), marketplace_currency)
    return (amounts[0] if amounts else None), currency


# ---------------------------------------------------------------------------
# Page state: four outcomes, four different right answers
# ---------------------------------------------------------------------------
# Amazon's front door is AWS WAF, not Akamai, and it produces three distinct
# non-content responses that want three opposite reactions. Collapsing them
# into one "blocked" is how a run either gives up on a page it would have
# got, or spends a captcha solve on a page no solve can help.
#
#   waf_challenge  HTTP 202 + challenge.js  ->  WAIT. A real browser runs
#                  AwsWafIntegration.getToken() and reloads itself; measured
#                  clearing in 3.9s on amazon.co.uk on 2026-09-08. Neither
#                  blocked nor solvable — solving it would bill for nothing.
#   throttled      HTTP 503 "Sorry! ... technical difficulties"  ->  RETRY,
#                  then rotate the exit. amazon.de returned this on a first
#                  request and 200 on a fresh context seconds later.
#   captcha        /errors/validateCaptcha, "Enter the characters you see
#                  below"  ->  Amazon's own IMAGE captcha. This is the path
#                  2Captcha is for, and the only one worth paying for.
#   signin         /ap/signin  ->  the page needs an account. Not a block and
#                  not solvable; the run asked for something anonymous
#                  visitors cannot have (see Review's docstring).
WAF_CHALLENGE_MARKERS = ("awswaf.com", "AwsWafIntegration", "challenge-container",
                         "awsWafCookieDomainList")
THROTTLE_MARKERS = ("Sorry! Something went wrong",
                    "To discuss automated access to Amazon data",
                    "we're having technical difficulties",
                    "Tut uns Leid")
CAPTCHA_MARKERS = ("/errors/validateCaptcha", "Enter the characters you see below",
                   "Type the characters you see in this image",
                   "captcha/captcha-form")
# Deliberately NOT a set of HTML markers. A live run reported "needs sign-in"
# for every best-seller grid, because "/ap/signin" is in the header of every
# Amazon page — and the real signin page contains that string ZERO times, the
# redirect being in the address bar rather than the markup. Testing the body
# got it wrong in both directions at once, so the state is read from the URL.
SIGNIN_URL_MARKER = "/ap/signin"

# Kept under the family's name and shape so the engines' shared logic reads
# the same in every repo. Only the states that genuinely stood between the
# run and the content are listed: the WAF interstitial is NOT here, because
# reporting it as a block would report a block that clears itself.
BOT_CHALLENGE_MARKERS = {
    "amazon-captcha": CAPTCHA_MARKERS,
    "amazon-throttle": THROTTLE_MARKERS,
}


def detect_page_state(html: str, status: Optional[int] = None,
                      url: Optional[str] = None) -> str:
    """One of "ok", "waf_challenge", "captcha", "throttled", "signin".

    Order matters. A captcha page also mentions "Sorry", and a signin page
    can be reached from either, so the most specific and most consequential
    state is tested first.
    """
    if not html:
        return "ok"
    if any(m in html for m in CAPTCHA_MARKERS):
        return "captcha"
    if any(m in html for m in WAF_CHALLENGE_MARKERS):
        return "waf_challenge"
    if any(m in html for m in THROTTLE_MARKERS) or status == 503:
        return "throttled"
    # From the URL the browser ENDED on — see SIGNIN_URL_MARKER.
    if url and SIGNIN_URL_MARKER in url:
        return "signin"
    return "ok"


def detect_bot_challenge(html: str, url: Optional[str] = None) -> Optional[str]:
    """Vendor name if `html` is a bot-challenge interstitial, else None.

    Deliberately returns None for the AWS WAF challenge: that one clears
    itself in a browser, so treating it as a block turns a 4-second wait into
    exit 3. The engines call detect_page_state() to decide whether to wait,
    retry, solve or give up, and use this only for the family's
    blocked/not-blocked exit-code mapping.
    """
    state = detect_page_state(html, url=url)
    if state == "captcha":
        return "amazon-captcha"
    if state == "throttled":
        return "amazon-throttle"
    return None


# ---------------------------------------------------------------------------
# URLs: page kinds, pagination, category
# ---------------------------------------------------------------------------
def listing_kind(url: str) -> str:
    """Which kind of page `url` is: "search", "bestsellers", "detail" or "other".

    Drives both the parser dispatch and the pagination convention, which are
    NOT the same between the two listing kinds.
    """
    try:
        path = urlparse(url or "").path
    except ValueError:
        return "other"
    if _ASIN_IN_URL_RE.search(path):
        return "detail"
    if path.startswith("/s") and (path == "/s" or path.startswith("/s/") or path.startswith("/s?")):
        return "search"
    if "/zgbs" in path or "/gp/bestsellers" in path or "/Best-Sellers" in path or "/bestsellers" in path:
        return "bestsellers"
    if path.rstrip("/").endswith("/s"):
        return "search"
    return "other"


def page_url(url: str, page_num: int) -> str:
    """Return `url` with Amazon's own page parameter set to `page_num`.

    The fallback for when the next-page selector finds nothing — and on
    Amazon it carries more weight than elsewhere in this family, because
    Amazon publishes no `link[rel="next"]` at all (checked on both listing
    kinds). The only selector available is a build artefact
    (`a.s-pagination-next`), so a scraper that trusted it alone would have
    the silent-single-page failure mode this convention exists to prevent:
    rename one class and every run stops after page 1 and reports a
    complete, successful result with a fraction of the data.

    THE TWO LISTING KINDS USE DIFFERENT PARAMETERS. Search paginates with
    `?page=N`, best-seller grids with `?pg=N` — confirmed from the captured
    markup of each ("/s?k=wireless+headphones&page=2" from the search page's
    own next link, ".../zgbs/electronics/ref=zg_bs_pg_2_electronics?pg=2"
    from the grid's). Using one for the other silently re-fetches page 1.

    Verified reconstructable: page 1's own next-link on /s carries `page=2`
    plus tracking parameters only (`xpid`, `qid`, `ref`), no cursor or token,
    so page N's address does not depend on having loaded page N-1. That is
    what makes --concurrency safe here.

    Existing query parameters (filters, sort order) are preserved, and an
    existing page parameter is replaced rather than appended twice.
    """
    param = "pg" if listing_kind(url) == "bestsellers" else "page"
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ("page", "pg")]
    query.append((param, str(page_num)))
    return urlunparse(parts._replace(query=urlencode(query)))


# Path segments that are routing, not a category.
_NOT_A_CATEGORY = {"s", "zgbs", "gp", "bestsellers", "best-sellers", "dp",
                   "ref", "product", "aw", "d"}


def category_from_url(url: str) -> Optional[str]:
    """Best-effort category label from a listing URL.

    `category` is a free-text label for grouping rows. On a search URL the
    query itself is the only honest label — there is no category — so the
    `k` parameter is used; on a best-seller grid the department slug is.

        /s?k=wireless+headphones                    -> wireless headphones
        /s?k=headphones&rh=n%3A172282               -> headphones
        /Best-Sellers-Electronics/zgbs/electronics/ -> electronics
        /zgbs/electronics/172282/                   -> electronics

    A caller who wants a tidier label passes --category, which always wins.
    Returns None when nothing usable is there, leaving the field null rather
    than inventing a value.
    """
    if not url:
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None

    kind = listing_kind(url)
    if kind == "detail":
        # A /dp/ URL carries no category, and the ASIN is emphatically not
        # one — returning it made every detail row's category its own id.
        # parse_product_detail reads the page's breadcrumb instead.
        return None
    if kind == "search":
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        term = (query.get("k") or "").strip()
        return term or None

    segs = [seg for seg in parts.path.split("/") if seg]
    # A best-seller path is /zgbs/<department>[/<node id>], and the node id
    # is digits — informative, but not a label.
    segs = [s for s in segs if s.lower() not in _NOT_A_CATEGORY and not s.isdigit()]
    segs = [s for s in segs if not s.startswith("ref=")]
    if not segs:
        return None
    # Prefer the segment AFTER the routing marker (/zgbs/electronics) over a
    # decorative slug before it (/Best-Sellers-Electronics/zgbs/electronics).
    return segs[-1] or None


def _canonical_product_url(host: str, asin: str) -> str:
    return "https://www.{}/dp/{}".format(host, asin)


def _asin_from_href(href: Optional[str]) -> Optional[str]:
    """ASIN out of a product href, including a sponsored click-tracker.

    A sponsored tile's href holds the real path percent-encoded in its `url`
    parameter, so the plain search fails and the string has to be unquoted
    first. Two rounds of unquoting: the value is encoded once as a query
    parameter and Amazon sometimes encodes it again inside that.
    """
    if not href:
        return None
    for candidate in (href, unquote(href), unquote(unquote(href))):
        m = _ASIN_IN_URL_RE.search(candidate)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Tile scoping, for the URL-pattern fallback only
# ---------------------------------------------------------------------------
# How far to widen from a product link when looking for its tile. The primary
# paths never need this — `data-asin` is ON the tile — but the fallback
# starts from a link and has to find the box around it.
_MAX_TILE_WIDEN = 8


def _tile_scope(anchor):
    """The outermost ancestor of `anchor` that still holds exactly ONE product link.

    Stopping one level too late is the "junk-link data theft" failure: every
    tile then reports its neighbours' prices, and a link that matches the URL
    shape by coincidence steals a real product's data. Amazon makes this
    concrete — a captured search page carries 143 `/dp/` links for 22 organic
    tiles, the rest belonging to carousels and "customers also viewed"
    strips.
    """
    best, node = anchor, anchor
    for _ in range(_MAX_TILE_WIDEN):
        node = node.parent
        if node is None or not hasattr(node, "select"):
            break
        if len(node.select(SELECTORS["item_link"])) != 1:
            break
        best = node
    return best


# ---------------------------------------------------------------------------
# Small field helpers
# ---------------------------------------------------------------------------
# Two orders, because not every locale writes the rating first. English,
# German, French, Italian and Spanish print "4.6 out of 5" / "4,6 von 5";
# Japanese prints the scale first — "5つ星のうち 4.2" — which the first pattern
# cannot match at all. Before the second one existed every rating on
# amazon.co.jp came back null (0 of 60 on a live page) while the page plainly
# showed them, and a column that is null by locale reads as a broken field.
_RATING_RE = re.compile(r"([\d.,]+)\s*(?:out of|von|sur|su|de|／|/)\s*5", re.IGNORECASE)
_RATING_RE_SCALE_FIRST = re.compile(r"5\s*つ星のうち\s*([\d.,]+)")

# Zero-width characters Amazon injects into titles (U+200B and friends).
# Left in, two runs of the same product produce strings that are unequal
# while looking identical, so every diff reports a title change.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\ufeff\u2060]")


def _clean_text(value):
    """Collapse whitespace and drop zero-width characters."""
    if not value:
        return None
    return " ".join(_INVISIBLE_RE.sub("", value).split()) or None


def _to_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group().replace(",", "."))
    except ValueError:
        return None


def _rating_from(node) -> Optional[float]:
    """Stars out of five, from the icon's own accessible text.

    Amazon renders the number only inside `.a-icon-alt` ("4.6 out of 5
    stars") or an `aria-label`; the visible stars are a sprite with no text.
    Localised pages use the local wording, hence the alternatives in
    _RATING_RE — a run on amazon.de would otherwise report every rating as
    null while the page plainly shows them.
    """
    if node is None:
        return None
    texts = [el.get_text(" ", strip=True) for el in node.select(".a-icon-alt")]
    texts += [el.get("aria-label", "") for el in node.select("[aria-label]")]
    for text in texts:
        if not text:
            continue
        for pattern in (_RATING_RE, _RATING_RE_SCALE_FIRST):
            m = pattern.search(text)
            if m:
                value = _to_float(m.group(1))
                # A rating is out of five by definition; anything else means
                # the pattern matched something that was not a rating.
                if value is not None and 0 <= value <= 5:
                    return value
    return None


def _int_from(text: Optional[str]) -> Optional[int]:
    """An integer out of a grouped number, in any locale's grouping.

    "87,349" / "87.349" / "87 349" all mean the same count. Every separator
    is dropped rather than interpreted: a review count has no decimal part,
    so there is nothing to disambiguate.
    """
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


# The words that name a ratings COUNT, in the marketplaces' own languages.
# The count is captured as the number immediately BEFORE the word, which is
# the whole point of this pattern rather than a digit sweep: an aria-label
# reads "4.4 out of 5 stars, 279,961 ratings" and pulling every digit out of
# it produced 445279961 — the rating, the scale and the count concatenated.
# That shipped in a live best-seller run where the column looked fully
# populated (50 of 50 rows) and every value in it was garbage.
_COUNT_WORDS = (r"ratings?|reviews?|Bewertungen|avis|valutazioni|rese\u00f1as|"
                r"opiniones|beoordelingen|avalia\u00e7\u00f5es|\u30ec\u30fc\u30c6\u30a3\u30f3\u30b0|\u4ef6\u306e\u8a55\u4fa1|\u8a55\u4fa1")
_COUNT_BEFORE_WORD_RE = re.compile(
    r"([\d][\d.,\u00a0\u202f\u2009 ]*)\s*(?:" + _COUNT_WORDS + r")", re.IGNORECASE)
# A node that also states a rating cannot be read as a bare count.
_HAS_RATING_RE = re.compile(r"out of\s*5|von\s*5|sur\s*5|su\s*5|\u3064\u661f\u306e\u3046\u3061",
                            re.IGNORECASE)


def _review_count_from(node) -> Optional[int]:
    """The ratings count beside the stars.

    Read from an `aria-label` in preference to visible text, because the
    visible form is abbreviated to "1K+" on some layouts while the label
    stays exact — but read as "the number before the word", never as "every
    digit in the string". See _COUNT_BEFORE_WORD_RE.
    """
    if node is None:
        return None

    for element in node.select("[aria-label]"):
        label = element.get("aria-label") or ""
        m = _COUNT_BEFORE_WORD_RE.search(label)
        if m:
            count = _int_from(m.group(1))
            if count is not None:
                return count

    # Fall back to the visible node. Anything that also states a rating is
    # skipped rather than parsed: on a best-seller card the rating and the
    # count share one row, and reading the row gives neither.
    for selector in ('[data-cy="reviews-block"] a span', ".a-size-small",
                     ".a-size-base.s-underline-text", "#acrCustomerReviewText"):
        for element in node.select(selector):
            text = element.get_text(" ", strip=True)
            if not text or _HAS_RATING_RE.search(text):
                continue
            m = _COUNT_BEFORE_WORD_RE.search(text)
            if m:
                count = _int_from(m.group(1))
                if count is not None:
                    return count
            # A node holding nothing but a grouped number IS the count —
            # "(28)" and "279,961" are both this shape.
            if re.fullmatch(r"[(\[]?[\d][\d.,\u00a0\u202f\u2009 ]*[)\]]?", text):
                count = _int_from(text)
                if count is not None:
                    return count
    return None


def _discount_from(price: Optional[float], original_price: Optional[float]
                   ) -> Optional[float]:
    """Percentage off, computed from the two prices rather than read.

    Amazon prints a "-45%" flash beside a deal price, and it is usually
    consistent with the arithmetic — but it is rendered from a different
    field, rounds differently, and on a coupon item describes a discount that
    is not in `price` at all. The arithmetic on two numbers this parser read
    itself is the trustworthy source, so nothing is read from the flash.
    """
    if original_price and price is not None and original_price > price:
        return round((1 - price / original_price) * 100, 1)
    return None


# ---------------------------------------------------------------------------
# Listing: search results
# ---------------------------------------------------------------------------
def _valid_asin(value: Optional[str]) -> Optional[str]:
    """An ASIN, or None. `data-asin=""` is common on layout rows.

    Amazon puts empty `data-asin` attributes on spacer and messaging rows
    inside the results list (11 of 63 on the captured page), so presence of
    the attribute is not presence of a product.
    """
    if not value:
        return None
    value = value.strip().upper()
    return value if re.fullmatch(_ASIN_RE, value) else None


def _tile_price(tile, mcur: Optional[str]
                ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """(price, currency, price_source) for one listing tile.

    Reads `.a-price .a-offscreen` — the screen-reader copy — in preference to
    anything visible. The visible price is split across three nodes
    (`a-price-symbol` + `a-price-whole` + `a-price-fraction`) so that Amazon
    can render the cents smaller, which means `.a-price`'s own text is the
    price CONCATENATED WITH ITSELF: "EUR 85.15EUR85.15". Reading that node
    directly is how a tile ends up parsed as 8515.
    """
    node = tile.select_one(".a-price:not(.a-text-price) .a-offscreen")
    if node is not None:
        price, currency = _first_price(node, mcur)
        if price is not None:
            return price, currency, "offscreen"

    # No offscreen copy: reassemble from the visible parts. Recorded as a
    # different price_source, so a layout change shows up as a provenance
    # change in diff_runs.py rather than as a silent price movement.
    whole = tile.select_one(".a-price:not(.a-text-price) .a-price-whole")
    if whole is not None:
        symbol = tile.select_one(".a-price:not(.a-text-price) .a-price-symbol")
        fraction = tile.select_one(".a-price:not(.a-text-price) .a-price-fraction")
        text = "{}{}{}".format(
            (symbol.get_text(strip=True) + " ") if symbol else "",
            whole.get_text(strip=True).rstrip(".,"),
            ("." + fraction.get_text(strip=True)) if fraction else "")
        price, currency = _prices_in(text, mcur)
        if price:
            return price[0], currency, "split"
    return None, None, None


def _tile_original_price(tile, mcur: Optional[str], price: Optional[float]
                         ) -> Tuple[Optional[float], Optional[str]]:
    """(original_price, currency) from the struck-through list price.

    Guarded on being ABOVE the current price. Amazon reuses `a-text-price`
    for other struck-through figures (a per-unit price, a "Typical:" range),
    and an "original" below the price would produce a negative discount.
    """
    node = tile.select_one(".a-price.a-text-price .a-offscreen") \
        or tile.select_one(".a-text-price .a-offscreen")
    original, currency = _first_price(node, mcur)
    if original is None or (price is not None and original <= price):
        return None, currency
    return original, currency


def _coupon_text(tile) -> Optional[str]:
    """The printed coupon text, de-duplicated.

    The coupon widget renders its label twice — once for screen readers and
    once visibly — so its own text reads "Save EUR 42.15 EUR 42.15 off
    coupon". The highlighted span alone is the label.
    """
    node = tile.select_one('[data-component-type="s-coupon-component"]')
    if node is None:
        return None
    highlight = node.select_one('[class*="s-coupon-highlight"]')
    text = (highlight or node).get_text(" ", strip=True)
    text = " ".join(text.split())
    return text or None


def page_number_from_url(url: str) -> Optional[int]:
    """Which listing page `url` addresses; 1 when it says nothing.

    Read from the URL rather than passed in, so every caller gets it right
    without extra plumbing — and both pagination conventions are covered
    (`page` on /s, `pg` on /zgbs). It exists because `position` alone is
    ambiguous: Amazon's `data-index` restarts near 2 on every page, so
    without the page number a row from page 3 and a row from page 1 claim the
    same position. A rank-tracking consumer needs the pair.
    """
    if not url:
        return None
    try:
        query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    except ValueError:
        return None
    for key in ("page", "pg"):
        raw = query.get(key)
        if raw and raw.isdigit():
            return int(raw)
    return 1


def _parse_search_tiles(soup, host: str, label: Optional[str],
                        mcur: Optional[str], page: Optional[int] = None
                        ) -> List[Product]:
    products: List[Product] = []
    for tile in soup.select(SELECTORS["search_tile"]):
        asin = _valid_asin(tile.get("data-asin"))
        if asin is None:
            continue

        price, currency, source = _tile_price(tile, mcur)
        original, original_currency = _tile_original_price(tile, mcur, price)
        title_node = tile.select_one("h2")
        image = tile.select_one("img.s-image") or tile.select_one("img[src]")
        # Joined, not taken first: Amazon splits a badge label across spans,
        # so amazon.de renders "Amazon's Choice" as
        # <span>Amazons </span><span>Tipp</span> and reading one span gives
        # "Amazons".
        badge = _clean_text(" ".join(
            b.get_text(" ", strip=True) for b in tile.select('[class*="a-badge-text"]')))
        classes = tile.get("class") or []

        products.append(Product(
            source=host,
            url=_canonical_product_url(host, asin),
            sku=asin,
            title=_clean_text(title_node.get_text(" ", strip=True)) if title_node else None,
            # No brand column on a search tile — see the module docstring.
            brand=None,
            price=price,
            currency=currency or original_currency,
            original_price=original,
            discount_pct=_discount_from(price, original),
            rating=_rating_from(tile),
            review_count=_review_count_from(tile),
            image_url=(image.get("src") if image else None),
            category=label,
            price_source=source,
            page=page,
            position=_int_from(tile.get("data-index")),
            # Two independent markers, because either alone has been seen
            # without the other: the label span, and `AdHolder` on the tile.
            sponsored=bool(tile.select('[class*="sponsored-label"]')) or "AdHolder" in classes,
            badge=badge,
            coupon=_coupon_text(tile),
        ))
    return products


# ---------------------------------------------------------------------------
# Listing: best-seller grids
# ---------------------------------------------------------------------------
_RANK_RE = re.compile(r"#\s*([\d,.]+)")


def _parse_bestseller_cards(soup, host: str, label: Optional[str],
                            mcur: Optional[str], page: Optional[int] = None
                            ) -> List[Product]:
    """Rows from a /zgbs/ grid, in the site's published rank order.

    `position` here is Amazon's own rank, not our enumeration order — it is
    the whole point of the page, and it is printed on the card ("#1"). Read
    rather than counted, so a card that fails to parse leaves a gap in the
    ranks instead of silently renumbering everything below it.

    Note these grids lazy-load: a captured page held 30 cards before
    scrolling and 50 after. That is the engines' problem (the readiness wait
    scrolls until the count stops growing), but it is why a run that returns
    30 rows for a 50-item grid is a scroll problem and not a parser one.
    """
    products: List[Product] = []
    for card in soup.select(SELECTORS["bestseller_card"]):
        inner = card.select_one("[data-asin]")
        asin = _valid_asin(inner.get("data-asin") if inner else None)
        if asin is None:
            link = card.select_one("a[href]")
            asin = _asin_from_href(link.get("href") if link else None)
        if asin is None:
            continue

        # Hashed class names ("_cDEzb_p13n-sc-price_3mJ9Z"), so matched on a
        # substring — the hash changes with the build, the stem does not.
        price_node = card.select_one('[class*="p13n-sc-price"]')
        price, currency = _first_price(price_node, mcur)
        title_node = card.select_one('[class*="line-clamp"]') \
            or card.select_one('[class*="p13n-sc-truncate"]')
        image = card.select_one("img[src]")
        rank_node = card.select_one('[class*="zg-bdg-text"]')
        # Detected the same way as on a search tile so the column carries the
        # same meaning in both. Best-seller grids showed no sponsored cards
        # in any capture, but "checked and there were none" (False) and "the
        # page does not say" (None) are different claims.
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else None
        if not title and image is not None:
            # The card's image alt text is the title, and on this grid it is
            # the same string the visible node holds.
            title = (image.get("alt") or "").strip() or None

        products.append(Product(
            source=host,
            url=_canonical_product_url(host, asin),
            sku=asin,
            title=title,
            brand=None,
            price=price,
            currency=currency,
            rating=_rating_from(card),
            review_count=_review_count_from(card),
            image_url=(image.get("src") if image else None),
            category=label,
            price_source="offscreen" if price is not None else None,
            page=page,
            position=_int_from(_RANK_RE.search(
                rank_node.get_text(strip=True)).group(1)) if (
                    rank_node and _RANK_RE.search(rank_node.get_text(strip=True))) else None,
            sponsored=bool(card.select('[class*="sponsored-label"]')),
        ))
    return products


# ---------------------------------------------------------------------------
# Listing: URL-pattern fallback
# ---------------------------------------------------------------------------
def _parse_url_fallback(soup, host: str, label: Optional[str],
                        mcur: Optional[str], page: Optional[int] = None
                        ) -> List[Product]:
    """Last resort: every `/dp/{ASIN}` link, scoped to its own tile.

    Deliberately the weakest path and deliberately still present. It runs
    only when both primary anchors yield nothing, which on Amazon means
    either a layout this parser has not seen or a page kind it does not know
    — and returning rows with a `price_source` that says "read from tile
    text" beats returning zero rows and reporting an empty category.

    Its known cost, measured: the captured search page carries 143 `/dp/`
    links covering 52 ASINs for 22 organic products. So this path
    over-collects carousel items, which is why it does not run when the
    primary path worked.
    """
    products: List[Product] = []
    seen = set()
    for anchor in soup.select(SELECTORS["item_link"]):
        asin = _asin_from_href(anchor.get("href"))
        if asin is None or asin in seen:
            continue
        seen.add(asin)
        tile = _tile_scope(anchor)
        amounts, currency = _prices_in(tile.get_text(" ", strip=True), mcur)
        # Highest is the list price, lowest is what is charged — the same
        # convention the rest of this family uses when a tile shows several.
        price = min(amounts) if amounts else None
        original = max(amounts) if len(amounts) > 1 else None
        image = tile.select_one("img[src]")
        title = anchor.get_text(" ", strip=True) or (
            (image.get("alt") or "").strip() if image else "")

        products.append(Product(
            source=host,
            url=_canonical_product_url(host, asin),
            sku=asin,
            title=title or None,
            price=price,
            currency=currency,
            original_price=original,
            discount_pct=_discount_from(price, original),
            rating=_rating_from(tile),
            review_count=_review_count_from(tile),
            image_url=(image.get("src") if image else None),
            category=label,
            price_source="split" if price is not None else None,
            page=page,
        ))
    return products


def parse_products(html: str, base_url: str, category: Optional[str] = None
                   ) -> List[Product]:
    """Rows from a listing page: search results or a best-seller grid.

    Dispatches on the URL's shape, then falls back — in that order — to the
    other primary anchor and finally to the URL pattern. A page kind this
    parser does not recognise still gets all three tried, because a URL that
    does not look like a listing may still BE one (a filtered department
    page, a deals page).
    """
    soup = BeautifulSoup(html, "html.parser")
    host = marketplace_host(base_url)
    mcur = marketplace_info(base_url).get("currency")
    # An explicit label always wins; otherwise derive one so the column is
    # populated by default. `base_url` is the URL the browser ENDED on, so
    # after a redirect this reflects the page actually parsed.
    label = category or category_from_url(base_url)

    kind = listing_kind(base_url)
    page = page_number_from_url(base_url)
    order = ([_parse_bestseller_cards, _parse_search_tiles]
             if kind == "bestsellers" else
             [_parse_search_tiles, _parse_bestseller_cards])
    for parser in order:
        products = parser(soup, host, label, mcur, page)
        if products:
            return products

    products = _parse_url_fallback(soup, host, label, mcur, page)
    if products:
        logger.warning(
            "Neither primary anchor matched (%s / %s); fell back to the "
            "/dp/ URL pattern and got %d row(s). That path over-collects "
            "carousel items — check whether the layout changed.",
            SELECTORS["search_tile"], SELECTORS["bestseller_card"], len(products))
    return products


# ---------------------------------------------------------------------------
# Product detail
# ---------------------------------------------------------------------------
_BRAND_PATTERNS = (
    re.compile(r"^Visit the (.+?) Store$", re.IGNORECASE),
    re.compile(r"^Brand: (.+)$", re.IGNORECASE),
    re.compile(r"^(?:Marke|Marca|Marque|Merk|ブランド)[:：]\s*(.+)$", re.IGNORECASE),
    re.compile(r"^(.+?) Store$", re.IGNORECASE),
)


def _brand_from_byline(node) -> Optional[str]:
    """The brand out of `#bylineInfo`, which is a sentence, not a field.

    Amazon renders the brand as a link labelled "Visit the ZIHNIC Store" (or
    "Brand: ZIHNIC", or the localised equivalent). The whole sentence in a
    `brand` column would be wrong in a way that silently breaks any grouping
    a consumer does on it, so it is unwrapped — and when none of the known
    shapes match, the raw text is kept rather than dropped: a brand column
    holding "Visit the X Store" is at least recoverable, while None is not.
    """
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    if not text:
        return None
    for pattern in _BRAND_PATTERNS:
        m = pattern.match(text)
        if m:
            return m.group(1).strip()
    return text


def _detail_images(soup) -> Optional[List[str]]:
    """Every image URL the detail page offers, largest variant first.

    `#landingImage` carries a `data-a-dynamic-image` JSON map of
    URL -> [width, height]; the plain `src` is only whichever size the
    layout picked. Reading the map means the row holds the full-size image a
    consumer would actually want.
    """
    urls: List[str] = []
    main = soup.select_one("#landingImage, #imgBlkFront, #main-image")
    if main is not None:
        raw = main.get("data-a-dynamic-image")
        if raw:
            try:
                by_url = json.loads(raw)
            except (ValueError, TypeError):
                by_url = {}
            for url, size in sorted(
                    by_url.items(),
                    key=lambda kv: -(kv[1][0] if isinstance(kv[1], list) and kv[1] else 0)):
                if url not in urls:
                    urls.append(url)
        src = main.get("src")
        if src and src not in urls:
            urls.append(src)
    for thumb in soup.select("#altImages img[src]"):
        src = thumb.get("src")
        if src and src not in urls:
            urls.append(src)
    return urls or None


def _detail_in_stock(soup) -> Optional[bool]:
    """Whether the item can be bought, decided from MARKUP not from words.

    `#availability` reads "In Stock" / "Auf Lager" / "在庫あり" depending on
    the marketplace, so matching its text would work on amazon.com and
    silently report every German product as out of stock. The presence of an
    add-to-cart control, or of the out-of-stock block, is language-neutral.
    Neither present -> None, because "we could not tell" is not "no".
    """
    if soup.select_one("#add-to-cart-button, #buy-now-button, #addToCart"):
        return True
    if soup.select_one("#outOfStock, #outofstock_feature_div .a-color-price"):
        return False
    return None


# Ordered most-specific first. Amazon ships several price containers on one
# detail page and which of them holds the number depends on the layout
# variant served — on the captured page
# #corePriceDisplay_desktop_feature_div was PRESENT AND EMPTY while
# #corePrice_feature_div held "EUR19.61".
# `:not(.a-text-price)` on every entry, for the same reason the tiles need
# it: the struck-through LIST price is also an `.a-price .a-offscreen` and it
# sits inside the very same container. Without the exclusion the detail row
# reported 20.64 — the crossed-out figure — while the page charged 19.61.
PRICE_SELECTORS_DETAIL = (
    "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#centerCol .a-price:not(.a-text-price) .a-offscreen",
)
LIST_PRICE_SELECTORS_DETAIL = (
    "#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen",
    "#corePrice_feature_div .a-price.a-text-price .a-offscreen",
    "#basisPrice .a-price .a-offscreen",
    "#centerCol .a-price.a-text-price .a-offscreen",
)


def _first_parsing_price(soup, selectors, mcur):
    """First selector whose node yields an actual number.

    Falling through on an EMPTY match rather than on a MISSING one is the
    whole point: a present-but-empty container used to end the search and
    leave the price null.
    """
    for selector in selectors:
        for node in soup.select(selector):
            price, currency = _first_price(node, mcur)
            if price is not None:
                return price, currency
    return None, None


def parse_product_detail(html: str, base_url: str, category: Optional[str] = None
                         ) -> List[Product]:
    """One Product from a /dp/{ASIN} page, with the detail-only fields filled.

    Returns a LIST of zero or one row, so every mode's parser has the same
    shape and the engines' page loop does not special-case this one.
    """
    soup = BeautifulSoup(html, "html.parser")
    host = marketplace_host(base_url)
    mcur = marketplace_info(base_url).get("currency")

    asin = _asin_from_href(base_url)
    if asin is None:
        # A canonical link or the reviews widget carries it even when the URL
        # that was requested does not (a /gp/ URL, a slug-only URL).
        canonical = soup.select_one('link[rel="canonical"][href]')
        asin = _asin_from_href(canonical.get("href") if canonical else None)
    if asin is None:
        holder = soup.select_one("[data-asin]")
        asin = _valid_asin(holder.get("data-asin") if holder else None)
    title_node = soup.select_one("#productTitle")
    if asin is None or title_node is None:
        logger.warning("No product on %s (asin=%s, title=%s) — not a detail "
                       "page, or it did not render.", base_url, asin,
                       bool(title_node))
        return []

    price, currency = _first_parsing_price(soup, PRICE_SELECTORS_DETAIL, mcur)
    original, original_currency = _first_parsing_price(
        soup, LIST_PRICE_SELECTORS_DETAIL, mcur)
    if original is not None and price is not None and original <= price:
        original = None

    crumbs = [a.get_text(strip=True) for a
              in soup.select("#wayfinding-breadcrumbs_feature_div a")]
    seller = soup.select_one("#sellerProfileTriggerId") or soup.select_one("#merchant-info")
    availability = soup.select_one("#availability")
    bullets = [b.get_text(" ", strip=True) for b
               in soup.select("#feature-bullets li span.a-list-item")]
    variations = [v.get_text(" ", strip=True) for v
                  in soup.select("#twister .a-button-text, #twisterContainer .a-button-text")]
    # Scoped, not document-wide. Read from the whole page, _review_count_from
    # matched an aria-label belonging to a "customers also viewed" carousel
    # item and reported ITS review count (13,930) for this product, whose own
    # #acrCustomerReviewText said 87,349. A wrong number that looks right is
    # the worst kind of bug this parser can ship.
    rating_holder = soup.select_one("#averageCustomerReviews") or soup.select_one("#centerCol") or soup

    return [Product(
        source=host,
        url=_canonical_product_url(host, asin),
        sku=asin,
        title=_clean_text(title_node.get_text(" ", strip=True)),
        brand=_brand_from_byline(soup.select_one("#bylineInfo")),
        price=price,
        currency=currency or original_currency,
        original_price=original,
        discount_pct=_discount_from(price, original),
        rating=_rating_from(rating_holder),
        review_count=_review_count_from(rating_holder),
        in_stock=_detail_in_stock(soup),
        image_url=(_detail_images(soup) or [None])[0],
        # The breadcrumb is the site's own taxonomy and strictly better than
        # anything derivable from a /dp/ URL, which carries no category at
        # all. An explicit --category still wins.
        category=category or (" > ".join(crumbs) if crumbs else None),
        price_source="detail" if price is not None else None,
        # Through _clean_text, which returns None for an empty match: a live
        # run found #sellerProfileTriggerId present but empty and wrote "" —
        # a column that is neither a value nor a null, and that every
        # consumer has to special-case.
        seller=_clean_text(seller.get_text(" ", strip=True)) if seller else None,
        availability=_clean_text(availability.get_text(" ", strip=True)) if availability else None,
        bullets=bullets or None,
        images=_detail_images(soup),
        variations=variations or None,
    )]


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------
# Amazon wraps a review body in an accessibility affordance whose text is
# part of the node: "Brief content visible, double tap to read full content."
# Left in, every single body row starts with it.
# The wording pairs both ways round — "Brief content visible, double tap to
# read full content." and "Full content visible, double tap to read brief
# content." — so this is a pattern rather than a list of literals. Matching
# only one of them left the other on the front of every body.
_REVIEW_FILLER_RE = re.compile(
    r"(?:Brief|Full|Short) content visible, double tap to read "
    r"(?:full|brief|short) content\.?", re.IGNORECASE)
_REVIEW_FILLER = ("Read more", "Read less")
_LEADING_STARS_RE = re.compile(r"^\s*[\d.,]+\s*(?:out of|von|sur|su|de)\s*5\s*stars?\s*",
                               re.IGNORECASE)
# The count group must START with a digit. Without that anchor the character
# class matched the single SPACE in "One person found this helpful", the group
# captured " ", and _int_from(" ") returned None — so the worded form (which
# Amazon uses for exactly one vote) reported no votes at all instead of 1.
_HELPFUL_RE = re.compile(
    r"(\d[\d.,   ]*)\s*(?:people|person|Personen|persona|personne)",
    re.IGNORECASE)


def _clean_review_body(node) -> Optional[str]:
    if node is None:
        return None
    text = _REVIEW_FILLER_RE.sub(" ", node.get_text(" ", strip=True))
    for filler in _REVIEW_FILLER:
        text = text.replace(filler, " ")
    return _clean_text(text)


def parse_reviews(html: str, base_url: str) -> List[Review]:
    """Reviews rendered on a /dp/{ASIN} page.

    Roughly a dozen per product — that is what Amazon shows an anonymous
    visitor, and /product-reviews/{ASIN} (the paginated history) redirects to
    /ap/signin even for page 1, so there is no second page to fetch. See
    Review's docstring in output_writer.py.
    """
    soup = BeautifulSoup(html, "html.parser")
    host = marketplace_host(base_url)
    page_asin = _asin_from_href(base_url)

    reviews: List[Review] = []
    for node in soup.select(SELECTORS["review"]):
        review_id = (node.get("data-reviewid") or "").strip() or None
        # The requested page's ASIN is the key; the review's own is recorded
        # beside it only when it differs. See Review.sku for the measurement.
        node_asin = _valid_asin(node.get("data-asin"))
        asin = page_asin or node_asin
        variant_asin = node_asin if (node_asin and node_asin != asin) else None
        title_node = node.select_one('[data-hook="reviewTitle"]')
        title = None
        if title_node is not None:
            # The title node also holds the star text on some layouts; the
            # rating has its own column, so it is stripped from the title
            # rather than duplicated into it.
            title = _LEADING_STARS_RE.sub(
                "", " ".join(title_node.get_text(" ", strip=True).split())) or None
        author = node.select_one(".a-profile-name")
        date_node = node.select_one('[data-hook="review-date"]')
        helpful_node = node.select_one('[data-hook="helpful-vote-statement"]')
        helpful = None
        if helpful_node is not None:
            text = helpful_node.get_text(" ", strip=True)
            m = _HELPFUL_RE.search(text)
            # "One person found this helpful" has no digit at all — a real
            # shape, and _int_from would return None for it.
            helpful = _int_from(m.group(1)) if m else (1 if "one" in text.lower() else None)
        variant = node.select_one('[data-hook="format-strip"]')

        reviews.append(Review(
            source=host,
            # The per-review permalink, not the product page: two reviews
            # from one product would otherwise be indistinguishable by url.
            url=("https://www.{}/gp/customer-reviews/{}".format(host, review_id)
                 if review_id else _canonical_product_url(host, asin) if asin else ""),
            sku=asin,
            review_id=review_id,
            title=title,
            rating=_rating_from(node.select_one('[data-hook="review-star-rating"]') or node),
            author=author.get_text(" ", strip=True) if author else None,
            # Kept as Amazon printed it ("Reviewed in the United States on
            # August 10, 2026") rather than parsed to a date: the string
            # carries the reviewer's marketplace as well as the day, the
            # wording is localised, and a wrong date is worse than a
            # verbatim one.
            review_date=(" ".join(date_node.get_text(" ", strip=True).split())
                         if date_node else None),
            verified_purchase=bool(node.select('[data-hook="avp-badge"]')),
            helpful_votes=helpful,
            variant=_clean_text(variant.get_text(" ", strip=True)) if variant else None,
            variant_asin=variant_asin,
            body=_clean_review_body(node.select_one('[data-hook="reviewText"]')),
        ))
    return reviews
