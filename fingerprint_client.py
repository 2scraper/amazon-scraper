#!/usr/bin/env python3
"""
fingerprint_client.py
----------------------
Client for 2captcha's Fingerprint API (https://2captcha.com/fingerprints/api),
plus the glue to apply a fingerprint to a Playwright context.

Why this exists separately from the Scraping Browser API: the Scraping Browser already
spoofs a fingerprint for you. This is for the other case — when you launch
your own Chromium (no --cdp-endpoint) and want it to present something other
than "headless Chrome on a Linux box in a datacenter". The two are
alternatives, not layers; there is no point fetching a fingerprint and then
connecting over CDP to a browser that has its own.

API surface used
----------------
  GET https://api.2captcha.com/fingerprint/random
  GET https://api.2captcha.com/fingerprint/generate
    key           your API key (also accepted as clientKey)
    format        "chromium" (default) | "raw"
    tags          platform/OS/browser filters, e.g. "Windows,Chrome,Desktop"
    country       ISO 3166-1 alpha-2
    min_browser_version / browser_version / force_browser_version
    build_version full version string — /generate only

`random` returns an existing record from their dataset; `generate` synthesises
one to spec. Both are billed per successful response, on a monthly plan with a
per-minute cap, so this module caches to disk by default: re-running a scrape
during development shouldn't re-bill every time.

Chromium-format response (the fields this module actually uses):
    id, country,
    screen:            {width, height}
    userAgent:         {value}
    navigator:         {platform, hardwareConcurrency, deviceMemory}
    webgl:             {vendor, renderer}
    speechSynthesis:   {voices: [...]}

What can and cannot be applied
------------------------------
Playwright sets `userAgent`, viewport and locale natively. `platform`,
`hardwareConcurrency`, `deviceMemory` and the WebGL vendor/renderer strings
have to be patched into the page before any site script runs, via
`add_init_script`. That patching is shallow by construction: it changes what
`navigator.*` and `WEBGL_debug_renderer_info` report, not what the GPU
actually is, so a fingerprinter that cross-checks reported WebGL strings
against real rendering output can still tell. Treat it as raising the floor,
not as a disguise — for anything adversarial, use the Scraping Browser, which
does this below the JS layer.

Usage
-----
    python3 fingerprint_client.py --tags "Windows,Chrome,Desktop" --country us
    python3 fingerprint_client.py --generate --build-version 145.0.7632.162

    # in code, with Playwright:
    fp = get_fingerprint(api_key, tags="Windows,Chrome,Desktop", country="us")
    context = browser.new_context(**playwright_context_kwargs(fp))
    context.add_init_script(playwright_init_script(fp))

Requires: pip install -r requirements.txt
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fingerprint_client")

API_BASE = "https://api.2captcha.com"
RANDOM_URL = f"{API_BASE}/fingerprint/random"
GENERATE_URL = f"{API_BASE}/fingerprint/generate"

DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "2captcha-fingerprints")


def _cache_path(cache_dir: str, params: dict, generate: bool) -> str:
    key = json.dumps({"generate": generate, **params}, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")


def get_fingerprint(api_key: str, *, tags: Optional[str] = None,
                    country: Optional[str] = None,
                    min_browser_version: Optional[int] = None,
                    browser_version: Optional[int] = None,
                    build_version: Optional[str] = None,
                    fmt: str = "chromium", generate: bool = False,
                    cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
                    refresh: bool = False, timeout: int = 30) -> dict:
    """Fetch one fingerprint. Cached on disk unless cache_dir is None.

    Caching matters here for a boring reason: both endpoints are billed per
    successful response and capped per minute, so an uncached call inside a
    scrape loop turns into a bill and then into rate-limit errors.
    """
    params = {"format": fmt}
    if tags:
        params["tags"] = tags
    if country:
        params["country"] = country
    if min_browser_version:
        params["min_browser_version"] = min_browser_version
    if browser_version:
        params["browser_version"] = browser_version
    if build_version:
        if not generate:
            raise ValueError("build_version is only accepted by /fingerprint/generate")
        params["build_version"] = build_version

    if cache_dir:
        path = _cache_path(cache_dir, params, generate)
        if os.path.exists(path) and not refresh:
            with open(path, encoding="utf-8") as f:
                fp = json.load(f)
            logger.info("Using cached fingerprint %s (%s)", fp.get("id"), path)
            return fp

    url = GENERATE_URL if generate else RANDOM_URL
    logger.info("GET %s %s", url, {k: v for k, v in params.items()})
    resp = requests.get(url, params={**params, "key": api_key}, timeout=timeout)

    if resp.status_code == 401:
        raise RuntimeError("Fingerprint API rejected the key (401). Note this is a "
                           "separate subscription from captcha solving — a working "
                           "solver key is not automatically enabled for fingerprints.")
    if resp.status_code == 429:
        raise RuntimeError("Fingerprint API rate limit hit (429). The per-minute cap "
                           "depends on your plan (30/100/300/unlimited). Cache the "
                           "result instead of fetching per request.")
    resp.raise_for_status()
    fp = resp.json()

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(cache_dir, params, generate), "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=2)
    logger.info("Fingerprint %s (%s) — %s", fp.get("id"), fp.get("country"),
                (fp.get("userAgent") or {}).get("value", "")[:70])
    return fp


def playwright_context_kwargs(fp: dict) -> dict:
    """The parts of a fingerprint Playwright can set natively on a context."""
    kwargs = {}
    ua = (fp.get("userAgent") or {}).get("value")
    if ua:
        kwargs["user_agent"] = ua
    screen = fp.get("screen") or {}
    if screen.get("width") and screen.get("height"):
        # A real window is smaller than the screen; a viewport exactly equal to
        # screen size is itself a signal.
        kwargs["viewport"] = {"width": int(screen["width"]),
                              "height": max(400, int(screen["height"]) - 120)}
        kwargs["screen"] = {"width": int(screen["width"]), "height": int(screen["height"])}
    country = fp.get("country")
    if country:
        kwargs["locale"] = f"en-{country.upper()}"
    return kwargs


def playwright_init_script(fp: dict) -> str:
    """JS to run before page scripts, patching what Playwright can't set.

    Values are baked in as JSON rather than interpolated as bare text so a
    string from the API can't terminate the script it's embedded in.
    """
    nav = fp.get("navigator") or {}
    webgl = fp.get("webgl") or {}
    payload = json.dumps({
        "platform": nav.get("platform"),
        "hardwareConcurrency": nav.get("hardwareConcurrency"),
        "deviceMemory": nav.get("deviceMemory"),
        "webglVendor": webgl.get("vendor"),
        "webglRenderer": webgl.get("renderer"),
    })
    return """
(() => {
  const fp = %s;
  const def = (obj, prop, value) => {
    if (value === null || value === undefined) return;
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); }
    catch (e) { /* already non-configurable: leave it rather than throw */ }
  };
  def(Navigator.prototype, 'platform', fp.platform);
  def(Navigator.prototype, 'hardwareConcurrency', fp.hardwareConcurrency);
  def(Navigator.prototype, 'deviceMemory', fp.deviceMemory);

  // WEBGL_debug_renderer_info: 37445 = UNMASKED_VENDOR, 37446 = UNMASKED_RENDERER.
  // Patch both WebGL1 and WebGL2 — a fingerprinter that reads only WebGL2 would
  // otherwise see the real values and the mismatch is itself a signal.
  for (const proto of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!proto) continue;
    const original = proto.prototype.getParameter;
    proto.prototype.getParameter = function (p) {
      if (p === 37445 && fp.webglVendor) return fp.webglVendor;
      if (p === 37446 && fp.webglRenderer) return fp.webglRenderer;
      return original.apply(this, arguments);
    };
  }
})();
""" % payload


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch a browser fingerprint from 2captcha")
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="API key. Defaults to $TWOCAPTCHA_KEY (safer than argv).")
    p.add_argument("--tags", default=None, help='e.g. "Windows,Chrome,Desktop"')
    p.add_argument("--country", default=None, help="ISO 3166-1 alpha-2, e.g. us")
    p.add_argument("--min-browser-version", type=int, default=None)
    p.add_argument("--browser-version", type=int, default=None)
    p.add_argument("--build-version", default=None, help="/generate only, e.g. 145.0.7632.162")
    p.add_argument("--format", dest="fmt", choices=["chromium", "raw"], default="chromium")
    p.add_argument("--generate", action="store_true",
                   help="Use /fingerprint/generate instead of /fingerprint/random")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--refresh", action="store_true", help="Bypass a cached copy")
    p.add_argument("--show-init-script", action="store_true",
                   help="Print the Playwright init script for this fingerprint")
    args = p.parse_args()

    if not args.key:
        logger.error("No API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    try:
        fp = get_fingerprint(
            args.key, tags=args.tags, country=args.country,
            min_browser_version=args.min_browser_version,
            browser_version=args.browser_version, build_version=args.build_version,
            fmt=args.fmt, generate=args.generate,
            cache_dir=None if args.no_cache else DEFAULT_CACHE_DIR,
            refresh=args.refresh)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        logger.error("%s", e)
        return 2

    print(json.dumps(fp, indent=2, ensure_ascii=False))
    if args.show_init_script:
        print("\n// --- Playwright context kwargs ---")
        print("// " + json.dumps(playwright_context_kwargs(fp)))
        print("\n// --- add_init_script ---")
        print(playwright_init_script(fp))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
