# Amazon Scraper — 2parser

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)
![Marketplaces](https://img.shields.io/badge/Marketplaces-21-orange)
![Proxies](https://img.shields.io/badge/Proxies-2captcha.com-green)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

Production-grade Amazon scraper with support for all 21 marketplaces and [2captcha.com](https://2captcha.com/proxy) proxy rotation.

---

## Features

- **21 marketplaces** — .com, .de, .co.uk, .fr, .co.jp, .in, .com.br and more
- **2captcha proxies** — native format, round-robin rotation, health tracking, ban cooldown
- **Search** — pagination, price/sort filters, automatic missing price fill
- **Products** — price, rating, images, bullet points, variations, seller info
- **Best Sellers** — 4 DOM strategies including current 2024–2025 layout
- **Reviews** — author, rating, body, verified purchase, helpful votes
- **Anti-bot** — 4 browser fingerprint profiles, per-market Accept-Language, 503/429 backoff
- **Export** — JSON, JSON Lines, CSV (UTF-8-BOM, opens in Excel without conversion)

---

## Installation

```bash
git clone https://github.com/2parser/amazon-scraper
cd amazon-scraper
pip install -r requirements.txt
```

---

## Proxies

Proxies are taken from your **2captcha.com personal account → Proxy** section.  
Download the list and pass it via `--proxies`:

```
# proxies.txt — one proxy per line (2captcha account format)
http://host:port:login:password
http://host:port:login:password
```

> No API key required for proxies — they come with login/password credentials.  
> The API key (`--2captcha-key`) is only needed for CAPTCHA solving (optional).

---

## Quick Start

```bash
# Search — 10 pages on amazon.de, fill missing prices
python main.py search "iphone" -m de \
    --proxies proxies.txt \
    --pages 10 \
    --fill-prices \
    --output results.json

# Single product by ASIN
python main.py product B08N5WRWNW -m de --proxies proxies.txt

# Best Sellers
python main.py best-sellers "Electronics" -m de --proxies proxies.txt

# Reviews
python main.py reviews B08N5WRWNW -m de --proxies proxies.txt --pages 5

# Bulk scraping from ASIN list file (one ASIN per line)
python main.py bulk asins.txt -m de --proxies proxies.txt --workers 4

# List all supported marketplaces
python main.py --list-marketplaces
```

---

## Marketplaces

`-m` / `--marketplace` accepts any format:

```bash
-m de                           # TLD
-m uk                           # alias → co.uk
-m co.jp                        # full TLD
-m amazon.fr                    # domain
-m https://www.amazon.it/dp/... # URL — marketplace auto-detected
```

| TLD | Country | Currency |
|-----|---------|----------|
| com | United States | USD |
| co.uk | United Kingdom | GBP |
| de | Germany | EUR |
| fr | France | EUR |
| it | Italy | EUR |
| es | Spain | EUR |
| nl | Netherlands | EUR |
| pl | Poland | PLN |
| se | Sweden | SEK |
| co.jp | Japan | JPY |
| ca | Canada | CAD |
| com.au | Australia | AUD |
| in | India | INR |
| com.br | Brazil | BRL |
| com.mx | Mexico | MXN |
| sg | Singapore | SGD |
| ae | UAE | AED |
| sa | Saudi Arabia | SAR |
| com.tr | Turkey | TRY |
| com.be | Belgium | EUR |
| eg | Egypt | EGP |

`Accept-Language` and proxy geo-targeting are automatically configured per marketplace.

---

## Python API

```python
from scraper import AmazonScraper
from proxy_manager import ProxyManager
from output import save_json, save_csv

# Initialize
scraper = AmazonScraper(
    proxy="http://host:port:login:password",  # single proxy
    # proxy_list=["http://...", "http://..."], # or a list
    marketplace="de",
)

# Load proxies from file
scraper.proxy_manager = ProxyManager.from_file("proxies.txt")
scraper.client.proxy_manager = scraper.proxy_manager

# Search
results = scraper.search(
    "iphone",
    pages=10,
    sort_by="review_rank",      # featured|price_asc|price_desc|review_rank|date_rank
    fill_missing_prices=True,   # extra /dp/ request for variative products
)
save_json([r.to_dict() for r in results], "results.json")
save_csv([r.to_dict() for r in results],  "results.csv")

# Single product
product = scraper.get_product("B08N5WRWNW")
print(product.title, product.price, product.currency)

# Best Sellers
items = scraper.get_best_sellers("Electronics")

# Reviews
reviews = scraper.get_reviews("B08N5WRWNW", pages=5)

# Bulk
products = scraper.get_products_bulk(["B08N5WRWNW", "B07VGRJDFY"], workers=3)
```

---

## Proxy formats

All common formats are supported:

| Format | Example |
|--------|---------|
| 2captcha account (main) | `http://host:port:login:password` |
| HTTPS | `https://host:port:login:password` |
| SOCKS5 | `socks5://host:port:login:password` |
| No scheme | `host:port:login:password` |
| URL format | `login:password@host:port` |
| No auth | `host:port` |

---

## Fill missing prices (`--fill-prices`)

Variative products (iPhones in different colors, laptops with different specs)
don't show a single price in search results — Amazon displays "More Options" instead.

`--fill-prices` automatically makes an extra `/dp/ASIN` request for each such product:

```
INFO  fill_missing_prices: 3 items without price → fetching product pages
INFO    ✓  B0CHX96JDY → 788.00 EUR
INFO    ✓  B09G995PVT → 303.00 EUR
INFO    ✓  B0CHWWM3JH → 774.00 EUR
INFO  fill_missing_prices: filled 3/3 items
```

---

## Output fields

### Search results
| Field | Type | Description |
|-------|------|-------------|
| `asin` | str | Amazon 10-char identifier |
| `title` | str | Product title |
| `price` | float | Price |
| `currency` | str | Currency (EUR, USD, GBP, JPY…) |
| `rating` | float | Rating 1.0–5.0 |
| `reviews_count` | int | Number of reviews |
| `url` | str | Full product URL |
| `sponsored` | bool | Is sponsored listing |
| `prime_eligible` | bool | Prime delivery available |
| `image_url` | str | Product image URL |

### Product page
`asin` · `title` · `brand` · `price` · `currency` · `list_price` · `rating` · `reviews_count` · `availability` · `prime_eligible` · `fulfilled_by_amazon` · `seller_name` · `images[]` · `bullets[]` · `description` · `category_breadcrumb[]` · `variations{}` · `deals` · `url`

### Best Sellers
`rank` · `asin` · `title` · `price` · `currency` · `rating` · `reviews_count` · `image_url` · `url` · `prime_eligible`

### Reviews
`review_id` · `author` · `rating` · `title` · `date` · `body` · `verified_purchase` · `helpful_votes`

---

## Anti-bot

| Mechanism | Details |
|-----------|---------|
| Browser fingerprints | Chrome Win/Mac, Firefox, Safari — real UAs + matching sec-ch-ua |
| Accept-Language | Per marketplace: `de-DE` for .de, `ja-JP` for .co.jp, etc. |
| Request delays | Random 2–6s, configurable via `--delay-min` / `--delay-max` |
| 503 / 429 handling | Exponential backoff 8s → 16s → 30s + proxy swap |
| CAPTCHA / block detection | 8 regex patterns, UA profile rotation on block |
| Proxy health tracking | Round-robin, 60s cooldown after 3 bans, drop below 20% success rate |

---

## Output formats

| Format | Extension | Notes |
|--------|-----------|-------|
| JSON | `.json` | Pretty-printed, full Unicode |
| JSON Lines | `.jsonl` | One object per line, streaming-friendly |
| CSV | `.csv` | UTF-8-BOM, opens in Excel without conversion |

---

## Project structure

```
amazon_scraper/
├── marketplace.py   ← registry of 21 marketplaces, format resolver
├── proxy_manager.py ← 2captcha proxy pool, rotation, health tracking
├── http_client.py   ← stealth HTTP, retry, 503/429 backoff
├── parser.py        ← lxml parsers: product / search / reviews / best-sellers
├── scraper.py       ← high-level API
├── output.py        ← JSON / JSONL / CSV export
├── main.py          ← CLI entry point
├── index.html       ← product landing page
└── requirements.txt
```

---

## CLI reference

```
python main.py [--list-marketplaces]
python main.py <command> <target> [options]

Commands:
  product         Single product (ASIN or URL)
  search          Search results
  reviews         Product reviews
  bulk            Bulk scraping from ASIN list file
  best-sellers    Best Sellers page

Core options:
  -o, --output PATH      Output file (.json | .jsonl | .csv)
  -m, --marketplace      Marketplace: de, uk, jp, co.uk, amazon.fr… (default: com)
      --proxies FILE      Proxy list file from 2captcha account (one per line)
      --proxy STRING      Single proxy: http://host:port:login:password
      --2captcha-key KEY  API key — only for CAPTCHA solving, not for proxies

Search options:
      --pages N           Pages to scrape (default: 1)
      --sort ORDER        featured|price_asc|price_desc|review_rank|date_rank
      --min-price N       Minimum price filter
      --max-price N       Maximum price filter
      --fill-prices       Fill missing prices via /dp/ for variative products

Performance:
      --workers N         Threads for bulk/fill-prices (default: 3)
      --delay-min SEC     Min request delay (default: 2.0)
      --delay-max SEC     Max request delay (default: 6.0)

Debug:
      --debug-html PATH   Save raw HTML response for DOM inspection
      --list-marketplaces Show all 21 marketplaces and exit
```

---

*Part of the [2parser](https://github.com/2parser) project — parsers for popular websites.*
