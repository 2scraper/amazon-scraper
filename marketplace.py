"""
marketplace.py
~~~~~~~~~~~~~~
Amazon marketplace registry and auto-detection utilities.

Supports any of these input formats:
  "de"                   → amazon.de
  "amazon.de"            → amazon.de
  "www.amazon.de"        → amazon.de
  "https://amazon.de/..."→ amazon.de
  "amazon.co.uk"         → amazon.co.uk
  "uk"                   → amazon.co.uk  (alias)
  "jp"                   → amazon.co.jp  (alias)
"""

from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Marketplace:
    tld: str           # e.g. "de", "co.uk"
    base_url: str      # e.g. "https://www.amazon.de"
    name: str          # e.g. "Germany"
    currency: str      # e.g. "EUR"
    language: str      # Accept-Language header value
    country_code: str  # ISO 3166-1 alpha-2, for proxy selection


# ─── Registry ────────────────────────────────────────────────────────────────
_REGISTRY: list[Marketplace] = [
    Marketplace("com",    "https://www.amazon.com",    "United States",    "USD", "en-US,en;q=0.9", "US"),
    Marketplace("co.uk",  "https://www.amazon.co.uk",  "United Kingdom",   "GBP", "en-GB,en;q=0.9", "GB"),
    Marketplace("de",     "https://www.amazon.de",     "Germany",          "EUR", "de-DE,de;q=0.9,en;q=0.8", "DE"),
    Marketplace("fr",     "https://www.amazon.fr",     "France",           "EUR", "fr-FR,fr;q=0.9,en;q=0.8", "FR"),
    Marketplace("it",     "https://www.amazon.it",     "Italy",            "EUR", "it-IT,it;q=0.9,en;q=0.8", "IT"),
    Marketplace("es",     "https://www.amazon.es",     "Spain",            "EUR", "es-ES,es;q=0.9,en;q=0.8", "ES"),
    Marketplace("nl",     "https://www.amazon.nl",     "Netherlands",      "EUR", "nl-NL,nl;q=0.9,en;q=0.8", "NL"),
    Marketplace("pl",     "https://www.amazon.pl",     "Poland",           "PLN", "pl-PL,pl;q=0.9,en;q=0.8", "PL"),
    Marketplace("se",     "https://www.amazon.se",     "Sweden",           "SEK", "sv-SE,sv;q=0.9,en;q=0.8", "SE"),
    Marketplace("co.jp",  "https://www.amazon.co.jp",  "Japan",            "JPY", "ja-JP,ja;q=0.9,en;q=0.8", "JP"),
    Marketplace("ca",     "https://www.amazon.ca",     "Canada",           "CAD", "en-CA,en;q=0.9,fr;q=0.8", "CA"),
    Marketplace("com.au", "https://www.amazon.com.au", "Australia",        "AUD", "en-AU,en;q=0.9",           "AU"),
    Marketplace("in",     "https://www.amazon.in",     "India",            "INR", "en-IN,en;q=0.9,hi;q=0.8", "IN"),
    Marketplace("com.br", "https://www.amazon.com.br", "Brazil",           "BRL", "pt-BR,pt;q=0.9,en;q=0.8", "BR"),
    Marketplace("com.mx", "https://www.amazon.com.mx", "Mexico",           "MXN", "es-MX,es;q=0.9,en;q=0.8", "MX"),
    Marketplace("sg",     "https://www.amazon.sg",     "Singapore",        "SGD", "en-SG,en;q=0.9",           "SG"),
    Marketplace("ae",     "https://www.amazon.ae",     "UAE",              "AED", "en-AE,en;q=0.9,ar;q=0.8", "AE"),
    Marketplace("sa",     "https://www.amazon.sa",     "Saudi Arabia",     "SAR", "ar-SA,ar;q=0.9,en;q=0.8", "SA"),
    Marketplace("com.tr", "https://www.amazon.com.tr", "Turkey",           "TRY", "tr-TR,tr;q=0.9,en;q=0.8", "TR"),
    Marketplace("com.be", "https://www.amazon.com.be", "Belgium",          "EUR", "nl-BE,nl;q=0.9,fr;q=0.8", "BE"),
    Marketplace("eg",     "https://www.amazon.eg",     "Egypt",            "EGP", "ar-EG,ar;q=0.9,en;q=0.8", "EG"),
]

# TLD → Marketplace
_BY_TLD: dict[str, Marketplace] = {m.tld: m for m in _REGISTRY}

# Common short aliases  →  TLD
_ALIASES: dict[str, str] = {
    "us":        "com",
    "usa":       "com",
    "uk":        "co.uk",
    "gb":        "co.uk",
    "jp":        "co.jp",
    "japan":     "co.jp",
    "au":        "com.au",
    "australia": "com.au",
    "br":        "com.br",
    "brazil":    "com.br",
    "mx":        "com.mx",
    "mexico":    "com.mx",
    "tr":        "com.tr",
    "turkey":    "com.tr",
    "be":        "com.be",
    "belgium":   "com.be",
    "eg":        "eg",
    "egypt":     "eg",
    # country codes that match ISO directly
    "de": "de", "fr": "fr", "it": "it", "es": "es",
    "nl": "nl", "pl": "pl", "se": "se", "ca": "ca",
    "in": "in", "sg": "sg", "ae": "ae", "sa": "sa",
}


def resolve(spec: str) -> Marketplace:
    """
    Resolve any marketplace specifier to a Marketplace object.

    Accepts:
        "de", "uk", "us", "jp"         — short code / alias
        "co.uk", "com.au"              — TLD
        "amazon.de", "www.amazon.co.uk"— domain
        "https://www.amazon.de/dp/..."  — full URL
        "amazon.com"                    — with or without www

    Falls back to amazon.com on unknown input (with a warning logged).
    """
    spec = spec.strip().lower()
    if not spec:
        return _BY_TLD["com"]

    # Full URL → extract hostname
    if spec.startswith("http"):
        m = re.search(r"amazon\.([a-z.]+)", spec)
        if m:
            spec = m.group(1)   # e.g. "co.uk", "de", "com"
        else:
            return _BY_TLD["com"]

    # Strip "www.amazon." or "amazon." prefix
    spec = re.sub(r"^(www\.)?amazon\.", "", spec)

    # Direct TLD match
    if spec in _BY_TLD:
        return _BY_TLD[spec]

    # Alias match
    tld = _ALIASES.get(spec)
    if tld:
        return _BY_TLD[tld]

    # Fallback
    import logging
    logging.getLogger(__name__).warning(
        "Unknown marketplace %r — falling back to amazon.com. "
        "Known: %s",
        spec, ", ".join(sorted(_BY_TLD.keys())),
    )
    return _BY_TLD["com"]


def from_url(url: str) -> Marketplace:
    """Extract the Marketplace from a product/search URL."""
    return resolve(url)


def list_all() -> list[Marketplace]:
    return list(_REGISTRY)


def detect_from_html(html: str) -> Marketplace:
    """
    Detect which marketplace served a page from its HTML content.
    Useful for post-hoc validation.
    """
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="https://www\.amazon\.([a-z.]+)/', html)
    if m:
        return resolve(m.group(1))
    return _BY_TLD["com"]
