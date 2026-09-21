"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three row shapes, one writer
----------------------------
The rest of this family ships a single `Product` row per repo. Amazon ships
three modes, and they are genuinely different objects:

    --mode listing   search results and best-seller grids -> Product
    --mode product   one /dp/{ASIN} detail page           -> Product, with the
                     trailing detail-only fields populated
    --mode reviews   the reviews rendered on /dp/{ASIN}    -> Review

`Product` keeps the family's field order exactly, with the Amazon-specific
columns appended after `price_source` (the family rule: site-specific fields
go at the end, so a consumer written against another repo in this family
still reads the first thirteen columns unchanged). `Review` is a separate
schema rather than nulls bolted onto `Product`, and `diff_runs.py` refuses a
reviews run rather than pretending it can diff one.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The marketplace hostname a row came from. Amazon is one site across 21
# domains, and which one produced a row is not derivable from the ASIN: the
# same ASIN exists on several marketplaces at different prices. So `source`
# carries the actual hostname of the URL that was scraped ("amazon.co.jp"),
# and this is only the default for a row built without one.
SOURCE_DEFAULT = "amazon.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The ASIN. Amazon puts it in the tile (`data-asin`) and in the product
    # URL (`/dp/{ASIN}`), so unlike most sites in this family there is no
    # case where a row has no id to key on.
    sku: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    price: Optional[float] = None
    # No guessed default: a caller that doesn't know the currency should say
    # so (None) rather than silently claiming USD. On Amazon this matters
    # more than elsewhere in the family — the currency follows the exit IP's
    # delivery country, NOT the marketplace domain. A European exit is served
    # EUR prices on amazon.co.jp.
    currency: Optional[str] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from, because the same column can hold figures of
    # different confidence and nothing used to say which. Amazon publishes no
    # JSON-LD at all (checked on listing, best-seller and detail pages: zero
    # `application/ld+json` blocks), so the values here are DOM-only and
    # describe which node was read:
    #   "offscreen" — `.a-price .a-offscreen`, the screen-reader copy of the
    #                 price. One node, fully formed, with the currency in it.
    #                 The trustworthy read.
    #   "split"     — reassembled from `.a-price-whole` + `.a-price-fraction`
    #                 because no offscreen node was present. Correct, but it
    #                 depends on how the tile was laid out, so a change there
    #                 shows up here rather than as a silent price change.
    #   "detail"    — a detail page's `#corePrice*` block (--mode product).
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about Amazon.
    price_source: Optional[str] = None

    # ---- Amazon-specific, appended so the family prefix above is stable ----
    # Position within the page as the site ordered it. On a best-seller grid
    # this is the published rank (#1..#50) and is the point of the page; on a
    # search page it is `data-index`, which is only meaningful together with
    # the page number and the query.
    # Which listing page this row came from (1-based). Without it `position`
    # is ambiguous: `data-index` restarts near 2 on every search page, so a
    # row from page 3 would claim the same position as one from page 1. Null
    # outside --mode listing, where there is no page.
    page: Optional[int] = None
    position: Optional[int] = None
    # Sponsored placements are real rows, not junk, but they are advertising
    # and a price-monitoring consumer usually wants them out. Flagged rather
    # than dropped, so the choice belongs to the consumer.
    sponsored: Optional[bool] = None
    # There is no `prime` column, deliberately: Amazon rendered no Prime
    # marker on any captured tile from a cross-border exit (0 of 22 on .com,
    # 0 of 16 on .de, 0 of 60 on .co.jp), so the column would be null on
    # every row of every run. A field that looks available and never is costs
    # more than a missing one. If a domestic exit does render it, add it back
    # with the measurement that justified it.
    # "Amazon's Choice", "Best Seller", "Overall Pick" — one badge slot.
    badge: Optional[str] = None
    # The printed coupon text ("Save 10%"), NOT applied to `price`: a coupon
    # is conditional on clipping it, so folding it in would report a price no
    # unclipped customer pays.
    coupon: Optional[str] = None
    # ---- populated by --mode product only; null on a listing run ----
    seller: Optional[str] = None
    availability: Optional[str] = None
    bullets: Optional[List[str]] = None
    images: Optional[List[str]] = None
    variations: Optional[List[str]] = None


@dataclass
class Review:
    """One customer review as rendered on /dp/{ASIN}.

    Deliberately NOT sourced from /product-reviews/{ASIN}: that path redirects
    to /ap/signin even for page 1, so the paginated review history is not
    available to an anonymous visitor at all. The detail page renders roughly
    a dozen reviews to anyone, and those are what this schema holds. There is
    no `--pages` for this mode, because there is no second page to fetch.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # Kept as `sku` and not `asin` so dedupe_by_key, diff_runs and every
    # consumer's id column are the same word across all three modes. This is
    # the ASIN of the page that was REQUESTED, which is not always the one on
    # the review: a live run against /dp/B0C3HCD34R returned reviews whose
    # own data-asin was B0CQXMXJC5, a colour variant. Keying the rows on the
    # variant would hand back rows the caller cannot join to what it asked
    # for, so the variant's id goes in `variant_asin` and both facts survive.
    sku: Optional[str] = None
    review_id: Optional[str] = None
    title: Optional[str] = None
    rating: Optional[float] = None
    author: Optional[str] = None
    review_date: Optional[str] = None
    verified_purchase: Optional[bool] = None
    helpful_votes: Optional[int] = None
    variant: Optional[str] = None
    # The ASIN the review itself is attached to, when it differs from `sku`
    # (a colour or size variant of the product that was requested). Null when
    # they are the same, so a non-null value always means something.
    variant_asin: Optional[str] = None
    body: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product, "reviews": Review}

# Modes whose rows are one-per-ASIN, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. A reviews run has many rows per ASIN.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. Amazon needs this for a
    second reason the rest of the family does not: sponsored placements
    repeat across pages (2 of 16 tiles on page 2 of a live search were
    already on page 1), so without it a paginated run inflates its own count.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    On `--mode reviews` this is called with key="review_id", because many
    rows legitimately share one `sku`.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # reviews run writes reviews columns rather than product columns.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On Amazon this code specifically does NOT cover the AWS WAF interstitial
# (HTTP 202 + challenge.js), which a real browser clears by itself in a few
# seconds. Reporting that as blocked would report a block that isn't one, and
# would spend a captcha solve on a page that needed patience.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
EXIT_PARTIAL = 6

# Exit code for a run that never GOT the page: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Until this existed every one of those returned EXIT_NO_PRODUCTS, so a dead
# proxy, a network flap and a search that genuinely matched nothing were one
# value to an automated caller. Those want three different responses — retry
# the same exit, change exit, accept the answer — and the caller had no way
# to choose. Measured: a live run through a misconfigured proxy failed to
# load for 60s and exited 4, the code documented as "ran fine, no products".
#
# 5 rather than a new number, and this is the part worth not re-litigating:
# the family's exit-code contract already reserves 5 for "the transport
# failed" (scraper_api_client has used it for a Scraper API error since it
# was written), and the browser engines simply had no way to say the same
# thing. Every repo in this family spells 4 as "ran, found nothing" and none
# of them defines a 7, so widening 5 from "remote API error" to "the fetch
# failed" keeps ONE meaning per code across the family instead of making
# this repo the only one whose callers need a per-repo table.
#
# Deliberately NOT applied when rows were gathered: a timeout on page 7 of
# 10 is a PARTIAL run (exit 6, output written), which is already right. This
# only decides what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def write_last_attempt(out_prefix: str, meta: dict) -> str:
    """Record a run that wrote no output, under its OWN filename.

    `finish_run` deliberately writes no `<out>.meta.json` for a failed run:
    `save` leaves the previous good output in place, and a "failed" sidecar
    beside good data would contradict it and make diff_runs.py refuse a
    comparison of data that is in fact fine.

    The cost of that, until this existed, was that a failed run left the
    caller NOTHING to read — the exit code was the whole story, so "the proxy
    died" and "the search matched nothing" were indistinguishable to anything
    driving the scraper. A separate name is what lets both facts coexist:
    `<out>.meta.json` keeps describing the last SUCCESSFUL output and
    `<out>.last_attempt.json` describes the most recent attempt, whether or
    not it produced anything.

    It is overwritten every run, including successful ones, so it is never
    stale: a `last_attempt` whose status is "complete" and whose
    `finished_at` matches the sidecar means the last attempt is the last
    success.
    """
    path = f"{out_prefix}.last_attempt.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because on Amazon neither is implied by
    the repo: the same output prefix can hold a listing run or a reviews run,
    from any of 21 marketplaces, and a consumer that guesses wrong reads the
    wrong columns. diff_runs.py refuses a pair whose modes differ.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # How many ROWS the run wrote, and what one row is. `products` was
        # the only name for years and is wrong for a mode whose rows are not
        # products: a live reviews run recorded "products": 13 for thirteen
        # reviews of ONE product, which reads as thirteen products to
        # anything summing the field across runs.
        #
        # `record_type` is derived from the row class rather than written
        # per mode, so a new mode cannot add a row type and forget to
        # declare it here.
        "records": products,
        "record_type": ROW_CLASS_BY_MODE.get(mode, Product).__name__.lower(),
        # Deprecated alias, kept because it has been in every sidecar this
        # project has ever written and something is reading it. Identical to
        # `records`, including for reviews, where both are equally the row
        # count — the fix is the NAME, so silently changing what the old
        # name means would be worse than leaving it wrong.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    # The noun comes from the row class, so a reviews run does not report
    # "Saved 13 products" for thirteen reviews of one product.
    noun = row_cls.__name__.lower() + ("" if len(rows) == 1 else "s")
    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} {noun} -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} {noun} -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. Amazon publishes no `link[rel=next]` at all, so the selector
# layer here is a build artefact (`a.s-pagination-next`) and the data layer
# carries correspondingly more weight. See playwright_scraper.py.
#
# "single_page_mode" is complete by construction: --mode product and
# --mode reviews read one page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


# Stop reasons that mean the run never obtained the page, as opposed to
# obtaining it and finding nothing on it. Kept as DATA next to the exit code
# they map to, so an engine cannot invent a reason that silently falls
# through to "no products" — the failure this list exists to prevent.
#
# The engines here currently only ever produce the first: a dead proxy is
# detected (`_proxy_failure`) and rotates the exit, but it reaches the
# sidecar as `page_load_timeout` like any other navigation failure. The other
# two are listed anyway, spelled as the rest of the family spells them, so
# splitting them later is a one-line change in the engine rather than a
# silent fall-through here.
#
# "pages_unattempted" is deliberately NOT in this set. It is set when page 1
# was fetched fine and the queue never drained, so a run holding no rows
# under that reason really did get its page and really did find nothing.
FETCH_FAILURE_STOP_REASONS = ("page_load_timeout", "proxy_unusable",
                              "http_error")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    status = "complete" if (rows and complete) else (
        "partial" if rows else "failed")
    meta = run_meta(
        status=status, stop_reason=stop_reason,
        pages_requested=pages_requested, pages_completed=pages_completed,
        pages_failed=pages_failed, mode=mode, source=source,
        start_url=start_url, final_url=final_url, products=len(rows))

    if wrote_output:
        write_run_meta(out_prefix, meta)
    # Always, and under its own name: see write_last_attempt for why this
    # cannot be the same file.
    write_last_attempt(out_prefix, meta)

    if not rows:
        # Nothing gathered at all, and the three reasons are not the same
        # answer. Ordered by how much each one proves: a named challenge
        # outranks a transport failure, which outranks "we got the page and
        # it was empty" — the only one of the three that is really
        # EXIT_NO_PRODUCTS.
        if blocked:
            return EXIT_BLOCKED
        if stop_reason in FETCH_FAILURE_STOP_REASONS:
            print(f"[!] The page was never fetched ({stop_reason}) — this is "
                  f"exit {EXIT_FETCH_FAILED}, NOT an empty result "
                  f"(exit {EXIT_NO_PRODUCTS}). Nothing can be concluded about "
                  f"the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
