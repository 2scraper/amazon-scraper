"""
output.py
~~~~~~~~~
Export scraped data to JSON, CSV, JSONL, or pipe to a callback.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Callable, Union

logger = logging.getLogger(__name__)


def save_json(data: list[dict], path: Union[str, Path], indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    logger.info("Saved %d records → %s", len(data), path)


def save_jsonl(data: list[dict], path: Union[str, Path]) -> None:
    """JSON Lines — one JSON object per line, ideal for large datasets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in data:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("Saved %d records → %s (JSONL)", len(data), path)


def save_csv(data: list[dict], path: Union[str, Path], flatten_lists: bool = True) -> None:
    """
    Save to CSV. List fields (images, bullets, etc.) are joined with '|'.
    """
    if not data:
        logger.warning("No data to save to CSV")
        return

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Flatten list/dict fields for CSV compatibility
    flat_data = []
    for record in data:
        flat = {}
        for k, v in record.items():
            if isinstance(v, list):
                flat[k] = " | ".join(str(i) for i in v) if flatten_lists else json.dumps(v)
            elif isinstance(v, dict):
                flat[k] = json.dumps(v, ensure_ascii=False)
            else:
                flat[k] = v
        flat_data.append(flat)

    fieldnames = list(flat_data[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig for Excel
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_data)
    logger.info("Saved %d records → %s (CSV)", len(data), path)


def stream_to_callback(items, callback: Callable[[dict], None]) -> None:
    """Process each scraped item through a custom callback (e.g. DB insert)."""
    for item in items:
        try:
            d = item.to_dict() if hasattr(item, "to_dict") else item
            callback(d)
        except Exception as exc:
            logger.error("Callback error for item %s: %s", getattr(item, "asin", "?"), exc)
