"""
main.py
~~~~~~~
CLI entry point and usage examples for the Amazon scraper.

Quick start
-----------
    # Single product
    python main.py product B08N5WRWNW

    # Search
    python main.py search "wireless headphones" --pages 3

    # Reviews
    python main.py reviews B08N5WRWNW --pages 5

    # Bulk from file (one ASIN per line)
    python main.py bulk asins.txt --workers 3 --output out/products.json

    # With 2captcha proxies
    python main.py product B08N5WRWNW \
        --2captcha-key YOUR_KEY \
        --fetch-proxies \
        --proxy-country US

    # With your own proxy list
    python main.py search "laptop" \
        --proxies proxies.txt \
        --output results.csv \
        --pages 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from scraper import AmazonScraper
from marketplace import list_all as list_marketplaces
from output import save_json, save_jsonl, save_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amazon-scraper",
        description="2parser Amazon scraper powered by 2captcha proxies",
    )
    p.add_argument(
        "command",
        choices=["product", "search", "reviews", "bulk", "best-sellers"],
        nargs="?",
        help="What to scrape",
    )
    p.add_argument("target", nargs="?", help="ASIN, URL, search query, or path to ASIN list")

    # Output
    p.add_argument("--output", "-o", default="", help="Output file (.json | .jsonl | .csv)")
    p.add_argument(
        "--marketplace", "-m",
        default="com",
        metavar="MARKET",
        help=(
            "Amazon marketplace. Accepts: TLD (de, co.uk, jp), alias (uk, us, au, jp), "
            "domain (amazon.de), or URL. Default: com. "
            "Use --list-marketplaces to see all 21 options."
        ),
    )
    p.add_argument(
        "--list-marketplaces",
        action="store_true",
        help="Print all supported Amazon marketplaces and exit",
    )

    # Прокси 2captcha (берутся из ЛК 2captcha.com)
    p.add_argument(
        "--proxy", default="",
        metavar="http://host:port:login:password",
        help="Один прокси в формате 2captcha ЛК: http://host:port:login:password",
    )
    p.add_argument(
        "--proxies", default="",
        metavar="FILE",
        help="Файл со списком прокси (один на строку, формат 2captcha ЛК)",
    )
    p.add_argument(
        "--2captcha-key", dest="captcha_key", default="",
        help="API ключ 2captcha.com — только для решения CAPTCHA (не для прокси)",
    )

    # Scraping params
    p.add_argument("--pages", type=int, default=1, help="Pages to scrape (search/reviews)")
    p.add_argument("--workers", type=int, default=3, help="Parallel workers for bulk scraping")
    p.add_argument("--sort", default="", help="Sort order for search: featured|price_asc|price_desc|review_rank|date_rank")
    p.add_argument("--min-price", type=int, default=None)
    p.add_argument("--max-price", type=int, default=None)
    p.add_argument(
        "--fill-prices",
        action="store_true",
        help=(
            "Для товаров без цены (вариативные позиции) делает доп. запрос "
            "на страницу /dp/ASIN. Увеличивает время, но даёт полные данные."
        ),
    )
    p.add_argument("--delay-min", type=float, default=2.0)
    p.add_argument("--delay-max", type=float, default=6.0)
    p.add_argument(
        "--debug-html",
        default="",
        metavar="PATH",
        help="Save raw response HTML to PATH for DOM inspection (e.g. debug.html)",
    )

    return p


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_proxy_file(path: str) -> list[str]:
    proxies = []
    p = Path(path)
    if not p.exists():
        logger.warning("Proxy file not found: %s", path)
        return []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            proxies.append(line)
    logger.info("Loaded %d proxies from %s", len(proxies), path)
    return proxies


def save_output(data: list[dict], output_path: str) -> None:
    if not output_path:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    ext = Path(output_path).suffix.lower()
    if ext == ".csv":
        save_csv(data, output_path)
    elif ext == ".jsonl":
        save_jsonl(data, output_path)
    else:
        save_json(data, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args = build_argparser().parse_args()

    # List marketplaces and exit
    if args.list_marketplaces:
        print(f"\n{'TLD':<12} {'Name':<20} {'Currency':<8} {'URL'}")
        print("─" * 68)
        for m in list_marketplaces():
            print(f"{m.tld:<12} {m.name:<20} {m.currency:<8} {m.base_url}")
        print()
        return

    # Прокси
    proxy_list: list[str] = []
    if args.proxies:
        proxy_list = load_proxy_file(args.proxies)

    # Init scraper
    scraper = AmazonScraper(
        proxy=args.proxy,
        proxy_list=proxy_list if proxy_list else None,
        captcha_api_key=args.captcha_key,
        marketplace=args.marketplace,
        min_delay=args.delay_min,
        max_delay=args.delay_max,
    )

    if not args.command:
        build_argparser().print_help()
        return

    logger.info("Marketplace: %s", scraper.marketplace_info())
    logger.info("Proxy pool: %s", scraper.proxy_stats())

    # ---------------------------------------------------------------- #
    if args.command == "product":
        if not args.target:
            sys.exit("Error: provide ASIN or URL as target")
        product = scraper.get_product(args.target)
        save_output([product.to_dict()], args.output)

    elif args.command == "search":
        if not args.target:
            sys.exit("Error: provide search query as target")
        results = scraper.search(
            args.target,
            pages=args.pages,
            sort_by=args.sort,
            min_price=args.min_price,
            max_price=args.max_price,
            debug_html_path=args.debug_html,
            fill_missing_prices=args.fill_prices,
            fill_workers=args.workers,
        )
        logger.info("Total search results: %d", len(results))
        save_output([r.to_dict() for r in results], args.output)

    elif args.command == "reviews":
        if not args.target:
            sys.exit("Error: provide ASIN as target")
        reviews = scraper.get_reviews(args.target, pages=args.pages)
        logger.info("Total reviews: %d", len(reviews))
        save_output([r.to_dict() for r in reviews], args.output)

    elif args.command == "bulk":
        if not args.target:
            sys.exit("Error: provide path to ASIN list file")
        p = Path(args.target)
        if not p.exists():
            sys.exit(f"File not found: {args.target}")
        asins = [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
        logger.info("Bulk scraping %d ASINs with %d workers", len(asins), args.workers)
        products = scraper.get_products_bulk(asins, workers=args.workers)
        save_output([pr.to_dict() for pr in products], args.output)

    elif args.command == "best-sellers":
        dept = args.target or ""
        items = scraper.get_best_sellers(department=dept, debug_html_path=args.debug_html)
        logger.info("Best Sellers items: %d", len(items))
        save_output([i.to_dict() for i in items], args.output)


if __name__ == "__main__":
    main()
