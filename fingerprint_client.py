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
    tags          ONE OS-family tag, e.g. "Windows". Not a list — see --tags
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
    python3 fingerprint_client.py --tags Windows --country de
    python3 fingerprint_client.py --generate --build-version 145.0.7632.162

    # in code, with Playwright:
    fp = get_fingerprint(api_key, tags="Windows", country="de")
    context = browser.new_context(**playwright_context_kwargs(fp))
    context.add_init_script(playwright_init_script(fp))

Requires: pip install -r requirements.txt
"""

import argparse
import hashlib
import json
import logging
import re
import os
import sys
from typing import Optional

import requests

import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fingerprint_client")


# An API key must never reach a log, and the easiest way for one to get there
# is an exception message. `requests` puts the FULL URL — query string and all
# — into the text of HTTPError and of every connection error, so any endpoint
# that takes its key as a query parameter leaks it the moment something goes
# wrong. That is not hypothetical: a 400 from the fingerprint endpoint printed
# a live key to the terminal.
#
# Everything raised or logged from this module goes through here first. The
# endpoint and the status survive, because which call failed is the useful
# half and is not the secret.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact(text) -> str:
    return _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))

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
    try:
        resp = requests.get(url, params={**params, "key": api_key}, timeout=timeout)
    except requests.RequestException as exc:
        # The key is a query parameter on this endpoint, so the URL inside a
        # connection error carries it. Re-raised redacted.
        raise RuntimeError("Fingerprint API request failed: %s" % _redact(exc)) from None

    if resp.status_code == 401:
        raise RuntimeError("Fingerprint API rejected the key (401). Note this is a "
                           "separate subscription from captcha solving — a working "
                           "solver key is not automatically enabled for fingerprints.")
    if resp.status_code == 400:
        # Say what the API said, and name the overwhelmingly likely cause.
        # `tags` takes ONE OS-family value; a list — which the plural name and
        # a fingerprint's own multi-valued `data.tags` both invite — is
        # rejected outright. Measured 2026-09-09; see --tags.
        try:
            detail = resp.json().get("errorDescription") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        hint = ""
        if tags and any(sep in tags for sep in (",", "|", " ")):
            hint = (" — `tags` takes ONE OS-family value (Windows, "
                    "Microsoft Windows, Android), not a list; you passed %r"
                    % tags)
        raise RuntimeError("Fingerprint API rejected the request (400): %s%s"
                           % (_redact(detail), hint))
    if resp.status_code == 429:
        raise RuntimeError("Fingerprint API rate limit hit (429). The per-minute cap "
                           "depends on your plan (30/100/300/unlimited). Cache the "
                           "result instead of fetching per request.")
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        # requests puts resp.url — including `key=...` — into this message.
        raise RuntimeError("Fingerprint API returned %s: %s"
                           % (resp.status_code, _redact(exc))) from None
    fp = resp.json()

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(cache_dir, params, generate), "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=2)
    logger.info("Fingerprint %s (%s) — %s", fp.get("id"), fp.get("country"),
                (fingerprint_user_agent(fp) or "no user agent in response")[:70])
    return fp


def fingerprint_user_agent(fp: dict) -> Optional[str]:
    """The UA string, from wherever this response shape keeps it.

    `chromium` format nests it as `userAgent.userAgent`; `raw` format puts it
    at `data.ua`. An earlier version read `userAgent.value`, which exists in
    NEITHER — so `--fingerprint` silently never set a user agent at all, and
    the browser kept its own. That is safe on its own but defeats the point
    of the flag: the run then presented a German fingerprint's screen and
    locale with a local Chromium's UA, which is the identity MISMATCH the
    flag exists to avoid.
    """
    ua = fp.get("userAgent")
    if isinstance(ua, dict):
        for key in ("userAgent", "value", "ua"):
            if ua.get(key):
                return ua[key]
    if isinstance(ua, str) and ua:
        return ua
    data = fp.get("data")
    if isinstance(data, dict) and data.get("ua"):
        return data["ua"]
    return None


def playwright_context_kwargs(fp: dict) -> dict:
    """The parts of a fingerprint Playwright can set natively on a context.

    Everything here comes from the fingerprint itself rather than being
    derived from it. A derived value is a guess wearing the fingerprint's
    authority, and the guesses this used to make were poor ones — see
    `locale` below.
    """
    kwargs = {}
    ua = fingerprint_user_agent(fp)
    if ua:
        kwargs["user_agent"] = ua

    screen = fp.get("screen") or {}
    if screen.get("width") and screen.get("height"):
        width, height = int(screen["width"]), int(screen["height"])
        # The window, not the screen: a viewport exactly equal to screen size
        # is itself a signal. The fingerprint states its own outer size, so
        # use that when it is there and fall back to an estimate when it is
        # not.
        outer_w = int(screen.get("outerWidth") or width)
        outer_h = int(screen.get("outerHeight") or max(400, height - 120))
        kwargs["viewport"] = {"width": outer_w, "height": max(400, outer_h)}
        kwargs["screen"] = {"width": width, "height": height}

    # The device pixel ratio, which Playwright takes as its own context
    # option and which was previously dropped on the floor. Measured
    # 2026-09-11 against the live API and a live browser: a fingerprint
    # stating `deviceScaleFactor: 1.25` produced a browser reporting
    # `window.devicePixelRatio === 1`, so the identity contradicted itself
    # on an axis any fingerprinter reads for free -- and a contradiction is
    # exactly what this flag exists to avoid. Same shape as the three
    # defects the family notes already list for this file: a key the API
    # returns that nothing applies. Found by comparing a live browser
    # against the fingerprint rather than by reading the code.
    scale = (fp.get("screen") or {}).get("deviceScaleFactor")
    if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0:
        kwargs["device_scale_factor"] = float(scale)

    intl = fp.get("intl") or {}
    # The fingerprint's OWN locale. This used to be built as
    # f"en-{country}", which produced "en-DE" for a German fingerprint — an
    # English-speaking visitor in Germany is possible but it is not what the
    # fingerprint describes, and a locale that contradicts the rest of the
    # identity is exactly the mismatch this flag is meant to prevent. The
    # real value here is "de-DE".
    locale = intl.get("contentLocale")
    if not locale:
        languages = intl.get("languages")
        if isinstance(languages, list) and languages:
            locale = languages[0]
    if not locale and fp.get("country"):
        locale = "en-%s" % fp["country"].upper()
    if locale:
        kwargs["locale"] = locale

    # Playwright can set the timezone natively, and a fingerprint that says
    # Europe/Berlin while the browser reports UTC contradicts itself in a way
    # any script can read.
    if intl.get("timeZone"):
        kwargs["timezone_id"] = intl["timeZone"]
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
    # Reads `.env` as well as the exported variable, through the family's own
    # loader. It used to read `os.environ` alone, which meant a key put in
    # `.env` exactly as the README and .env.example instruct worked for every
    # engine and failed HERE with "No API key" — a documented mechanism not
    # applied on one path, which is the shape of half the defects §16 lists.
    #
    # `.env` has to be LOADED before it can be read: `env_value` looks at
    # os.environ, and `load_env` is what fills that from the file. Calling it
    # here rather than relying on an engine having called it is the whole
    # point — this is a standalone entry point.
    #
    # And it goes through `env_value` rather than `os.environ.get` so the
    # PLACEHOLDER rule applies. Measured both ways: with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported, `os.environ.get`
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader off to check a subscription when they simply never
    # filled the key in. `env_value` says so instead, by name.
    env_config.load_env()
    p.add_argument("--key", default=env_config.env_value("TWOCAPTCHA_KEY"),
                   help="API key. Defaults to TWOCAPTCHA_KEY from the "
                        "environment or .env (safer than argv).")
    # Measured against the live API on 2026-09-09, because the example this
    # file used to carry ("Windows,Chrome,Desktop") returns 400 every time:
    #   accepted -> Windows, Microsoft Windows, Android
    #   rejected -> Chrome, Desktop, Mobile, Unknown, and EVERY combination,
    #               with any separator tried (comma, space, pipe)
    # A returned fingerprint's own `data.tags` lists several values, which is
    # what makes the plural form look plausible; the filter takes one.
    p.add_argument("--tags", default=None, metavar="TAG",
                   help="ONE OS-family tag, not a list: Windows, "
                        "Microsoft Windows or Android. Chrome/Desktop/Mobile "
                        "are rejected by the API with 400, and no combination "
                        "is accepted. Use --country to narrow further.")
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
