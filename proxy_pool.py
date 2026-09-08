"""
proxy_pool.py
--------------
A pool of proxy URLs and the rules for moving between them.

Why this exists as its own module: `--proxy` was a single static string
applied once at browser launch and never changed. That is the shape of a
demo, not of the thing proxies are bought for — the reason to hold a pool is
to spread a run across exits and to leave an exit that has started getting
challenged. The README sent readers to buy proxies without showing the
pattern; this is that pattern.

Kept engine-agnostic and side-effect free (no browser, no network) so the
rotation rules are covered by the offline suite rather than only by a live
run.

**Rotating the IP alone is not enough, and this is the part that is easy to
get wrong.** Carrying the same browser session across two exits is itself a
contradiction: cookies a bot manager issued against IP A, replayed from
IP B, are a stronger signal than either address on its own. So a caller must
build a FRESH browser context (new cookie jar, new storage) for every exit
this pool hands out — see playwright_scraper.py, which tears the browser
down and relaunches rather than swapping the proxy under a live session.
"""

from __future__ import annotations

import logging
import random
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("proxy_pool")

# `per-run` keeps one exit for the whole run — the safest default, since a
# single session that changes address mid-flight is more suspicious than one
# that does not. `per-page` takes a new exit for every page, which is what
# spreads volume; it costs a browser relaunch per page (see the module
# docstring for why that cost is mandatory rather than incidental).
ROTATE_MODES = ("per-run", "per-page")

# Schemes Playwright's `proxy.server` accepts. socks5 carries no credentials
# there (Chromium does not support authenticated SOCKS), so a socks5:// entry
# with a user:pass in it is rejected on load rather than silently ignored at
# request time.
_SUPPORTED_SCHEMES = ("http", "https", "socks5")


class ProxyError(ValueError):
    """A proxy list that cannot be used as given."""


def parse_proxy_line(line: str, source: str = "<arg>") -> Optional[str]:
    """Validate one proxy URL. Returns it, or None for a blank/comment line.

    Raises ProxyError with the offending line named, because a typo in a
    proxy list otherwise surfaces as a connection failure on page 1 with
    nothing pointing at the cause.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parsed = urlparse(line)
    if parsed.scheme not in _SUPPORTED_SCHEMES:
        raise ProxyError(
            f"{source}: {line!r} — scheme must be one of "
            f"{', '.join(_SUPPORTED_SCHEMES)} (got {parsed.scheme or 'none'}). "
            f"A bare host:port is not enough; write http://host:port.")
    if not parsed.hostname:
        raise ProxyError(f"{source}: {line!r} — no host in that URL.")
    if parsed.scheme == "socks5" and (parsed.username or parsed.password):
        raise ProxyError(
            f"{source}: {line!r} — Chromium cannot authenticate a SOCKS5 "
            f"proxy, so credentials here would be silently dropped. Use an "
            f"http:// entry for an authenticated proxy.")
    return line


def load_proxy_file(path: str) -> List[str]:
    """Read a proxy-per-line file. Blank lines and `#` comments are skipped."""
    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            entry = parse_proxy_line(raw, source=f"{path}:{lineno}")
            if entry:
                proxies.append(entry)
    if not proxies:
        raise ProxyError(f"{path}: no proxy entries found (only blanks/comments?)")
    return proxies


def mask(url: Optional[str]) -> str:
    """A proxy URL safe to log: credentials replaced, host and port kept.

    Host and port stay visible on purpose — knowing WHICH exit a run used is
    the whole point of a rotation log, and it is not the secret.
    """
    if not url:
        return "(none)"
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    creds = "***:***@" if (parsed.username or parsed.password) else ""
    return f"{parsed.scheme}://{creds}{host}{port}"


def to_playwright(url: Optional[str]) -> Optional[dict]:
    """Playwright's `proxy=` dict for a proxy URL, or None.

    Credentials go in their own fields rather than in `server`. Playwright
    passes `server` down to Chromium as a command-line switch, so a
    user:pass left in there would land in the browser process's argv — where
    anything on the machine that can run `ps` can read it.
    """
    if not url:
        return None
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}{port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def split_credentials(url: Optional[str]):
    """(url_without_credentials, (username, password) or None).

    For the two engines that cannot take a proxy URL whole. Chromium's
    `--proxy-server=` switch has nowhere to put a password AND lands in the
    browser process's argv, where anything that can run `ps` reads it — so
    the address goes on the command line and the credentials go through the
    driver's own channel (pyppeteer's `page.authenticate`). Selenium has no
    such channel at all, which is why it strips these and warns.
    """
    if not url:
        return None, None
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    scrubbed = f"{parsed.scheme}://{parsed.hostname}{port}"
    if parsed.username or parsed.password:
        return scrubbed, (parsed.username or "", parsed.password or "")
    return scrubbed, None


class ProxyPool:
    """An ordered pool of exits, plus a cursor and a rotation policy."""

    def __init__(self, proxies: List[str], rotate: str = "per-run",
                 shuffle: bool = False, rng: Optional[random.Random] = None):
        if not proxies:
            raise ProxyError("a proxy pool needs at least one entry")
        if rotate not in ROTATE_MODES:
            raise ProxyError(f"rotate must be one of {ROTATE_MODES}, got {rotate!r}")
        self._proxies = list(proxies)
        if shuffle:
            # Two runs started at the same minute otherwise hammer the same
            # first exit in the list.
            (rng or random).shuffle(self._proxies)
        self.rotate = rotate
        self._index = 0
        # Counted so a run can report how many exits it actually burned.
        self.rotations = 0

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def proxies(self) -> List[str]:
        """A copy of the exits, for handing a rotated view to each worker.

        A copy rather than the list itself: a worker builds its own pool from
        this, and two threads sharing one mutable list is the bug that makes
        concurrency stop being worth it.
        """
        return list(self._proxies)

    @property
    def current(self) -> str:
        return self._proxies[self._index % len(self._proxies)]

    def advance(self, reason: str) -> str:
        """Move to the next exit and return it. Wraps around the list.

        Wrapping rather than exhausting: a pool of 3 used across 50 pages is
        a legitimate configuration, and refusing to continue would be worse
        than reusing an exit. The log line says which exit and why, so a run
        that is cycling a too-small pool is visible rather than silent.
        """
        if len(self._proxies) == 1:
            logger.warning("Asked to rotate (%s) but the pool holds one exit "
                           "(%s) — staying on it. Add more with --proxy-file.",
                           reason, mask(self.current))
            return self.current
        self._index = (self._index + 1) % len(self._proxies)
        self.rotations += 1
        logger.info("Rotated proxy (%s) -> %s [exit %d/%d, rotation #%d]",
                    reason, mask(self.current), (self._index % len(self._proxies)) + 1,
                    len(self._proxies), self.rotations)
        return self.current

    def rotates_per_page(self) -> bool:
        return self.rotate == "per-page"


def from_args(args) -> Optional[ProxyPool]:
    """Build a pool from --proxy-file / --proxy, or None if neither is set.

    `--proxy-file` wins when both are given, and says so: silently ignoring
    one of two conflicting options is how a run ends up on an exit the
    operator did not choose.
    """
    proxy_file = getattr(args, "proxy_file", None)
    single = getattr(args, "proxy", None)
    rotate = getattr(args, "proxy_rotate", "per-run")

    if proxy_file:
        if single:
            logger.warning("--proxy-file and --proxy both given; using the file "
                           "and ignoring the single --proxy.")
        proxies = load_proxy_file(proxy_file)
        logger.info("Loaded %d proxy exit(s) from %s, rotation: %s",
                    len(proxies), proxy_file, rotate)
        return ProxyPool(proxies, rotate=rotate,
                         shuffle=getattr(args, "proxy_shuffle", False))

    if single:
        entry = parse_proxy_line(single, source="--proxy")
        if entry is None:
            raise ProxyError("--proxy was given but is empty")
        return ProxyPool([entry], rotate="per-run")

    return None
