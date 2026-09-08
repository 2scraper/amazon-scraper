#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the identifier the README already tells people to diff on for price
monitoring and assortment tracking, but that nothing in this repo actually
computed.

    python3 diff_runs.py --old girls_clothing.2026-09-01.json \\
                          --new girls_clothing.2026-09-07.json

Typical use is a scheduled re-run of one of the four scraper engines, kept
under a dated filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "girls_$(date +%F)"
    python3 diff_runs.py --old "girls_$(ls -t girls_*.json | sed -n 2p)" \\
                          --new "girls_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or just
                   off this particular page/category run)
  changed        — sku present in both, with a different price,
                   original_price, discount_pct, currency or in_stock
  source_changed — sku present in both with a different price, but also a
                   different price_source: one run got the DOM-corrected
                   figure and the other the raw JSON-LD one, so the two are
                   not comparable on price. Reported separately because this
                   says something about our own two snapshots, not about the
                   site — and --fail-on-change deliberately ignores it.

A product this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

TRACKED_FIELDS = ("price", "original_price", "discount_pct", "currency", "in_stock")

# The subset of TRACKED_FIELDS whose comparability depends on price_source
# matching between the two runs — see diff_products.
PRICE_FIELDS = ("price", "original_price", "discount_pct")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing price field moved by less than `tolerance_pct`.

    Exists because of a measured Amazon behaviour, not a general wish for
    fuzzy comparison. Served to a visitor whose delivery country uses a
    different currency from the marketplace, Amazon converts the price — and
    the rate ticks. Two runs of the SAME command ten minutes apart, from the
    same exit, disagreed on 10 of 43 shared ASINs, every one of them by the
    same relative amount (0.039%-0.058%: 25.80 -> 25.81, 51.61 -> 51.63,
    1117.53 -> 1118.00). Nothing about the offer changed; the exchange rate
    did.

    Left unhandled, `--fail-on-change` fires on every FX tick and the tool
    becomes noise. So the tolerance exists — but it DEFAULTS TO ZERO, because
    a price monitor that silently swallows small moves is worse than one that
    cries wolf, and a run against a marketplace in its own currency has no
    conversion in it at all and should report every cent.

    A move is judged on the LARGEST relative change among the price fields,
    so a genuine 0.5% cut is not hidden by a 0.04% tolerance applied
    field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in PRICE_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance = [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose price_source differs between runs is not comparable on
        # price: on Amazon that means one run read `.a-price .a-offscreen` —
        # the screen-reader copy, fully formed — while the other had to
        # reassemble the number from the visually split `.a-price-whole` and
        # `.a-price-fraction`, or read a detail page's own price block. The
        # figures should agree, and when they do not, the difference is in
        # how OUR two snapshots rendered, not in what the site charges.
        # Reporting it as a price change would be a false alarm about the
        # site. Non-price fields still compare fine.
        sources = (before.get("price_source"), after.get("price_source"))
        if sources[0] != sources[1] and any(f in field_changes for f in PRICE_FIELDS):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "price_source": {"old": sources[0], "new": sources[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # An FX tick rather than a price change — see _within_tolerance. Only
        # when the ONLY differences are price fields: a currency or stock
        # change alongside is a real change whatever the size of the move.
        if (all(f in PRICE_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable on price, "
          f"{len(result.get('within_tolerance', []))} within the price "
          f"tolerance.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: an exchange-rate tick, not a price change]")
    for c in result["source_changed"]:
        src = c["price_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[price_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"rendered differently, so this is not a site-side price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode == "reviews":
            # Reviews are many rows per ASIN, so this tool's whole premise —
            # one row per `sku`, diffed on price — does not hold. Refused
            # outright rather than producing a diff whose every line is an
            # artefact of two reviews sharing an ASIN.
            problems.append(
                f"{label} ({path}) is a reviews run. This tool diffs one row "
                f"per ASIN on price; a reviews run has many rows per ASIN and "
                f"no price at all, so there is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A listing row and a "
            f"detail row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the catalogue.")
    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two amazon-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a price move smaller than PCT%% as an exchange-"
                        "rate tick rather than a price change: reported "
                        "separately and ignored by --fail-on-change. Default 0 "
                        "(report every cent). Only useful for a run whose exit "
                        "country differs from the marketplace's, where Amazon "
                        "converts the price and the rate drifts — measured at "
                        "0.04%% over ten minutes, on 10 of 43 ASINs. A run "
                        "against a marketplace in its own currency needs this "
                        "at 0.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our own two snapshots rendered differently; the second
    # means an exchange rate moved. Neither says anything about the site, and
    # alerting on either would train whoever reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
