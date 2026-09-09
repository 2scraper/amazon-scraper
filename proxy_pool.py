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

    # Every message below reports mask(line), never the line itself. These
    # strings hold a password, and an error message is a log: a CI run
    # printed a live proxy login and password into its own public log this
    # way, from a traceback nobody expected to carry a credential.
    shown = mask(line)
    parsed = urlparse(line)
    if parsed.scheme not in _SUPPORTED_SCHEMES:
        raise ProxyError(
            f"{source}: {shown!r} — scheme must be one of "
            f"{', '.join(_SUPPORTED_SCHEMES)} (got {parsed.scheme or 'none'}). "
            f"A bare host:port is not enough; write http://host:port.")
    if not parsed.hostname:
        raise ProxyError(f"{source}: {shown!r} — no host in that URL.")
    # The PORT is validated here and not left to the first caller that reads
    # it. `urlparse` does not parse a port until you ask for one, and then it
    # raises ValueError — so a malformed entry sailed through this function
    # and blew up much later inside to_playwright as an uncaught traceback:
    # exit 1 (crash) where it should have been exit 2 (bad usage), with no
    # message saying what was wrong with the value.
    #
    # The value that caused it is worth knowing, because it is the mistake a
    # new user makes: a line from a proxy LIST FILE
    # ("http://host:port:login:password") pasted where a proxy URL belongs.
    # The extra colons become part of the port.
    try:
        parsed.port
    except ValueError:
        raise ProxyError(
            f"{source}: {shown!r} — the port is not a number. If you copied "
            f"this from a proxy list file, that format is "
            f"scheme://host:port:login:password and this expects a URL: "
            f"http://login:password@host:port") from None
    if parsed.scheme == "socks5" and (parsed.username or parsed.password):
        raise ProxyError(
            f"{source}: {shown!r} — Chromium cannot authenticate a SOCKS5 "
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

    THIS FUNCTION MUST NEVER RAISE. It is the last thing standing between a
    password and a log, and it is called precisely when something is already
    wrong with the value. An earlier version read `parsed.port`, which
    `urlparse` computes lazily and which raises ValueError on a malformed
    authority — so the masker blew up on exactly the input that most needed
    masking, and the caller printed the raw string instead. That is how a
    live proxy login and password reached a public CI log.

    Anything it cannot take apart is redacted whole rather than echoed.
    """
    if not url:
        return "(none)"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        # `parsed.port` raises on a malformed authority; the raw netloc is
        # not safe to fall back to, because that is where the password is.
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ":?"
        creds = "***:***@" if (parsed.username or parsed.password) else ""
        scheme = parsed.scheme or "?"
        return f"{scheme}://{creds}{host}{port}"
    except Exception:  # noqa: BLE001 — a masker that raises is worse than a
        # vague one. Something is already wrong with this value; say so
        # without repeating it.
        return "(unparseable proxy URL, redacted)"


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
        # Duplicates are dropped, order preserved. A pool is a set of EXITS,
        # and repeating one does not make it two: a list of fifty identical
        # entries — which is what a copied-and-pasted proxy list often is —
        # reported "exit 2/50" on every rotation while every one of them left
        # from the same address, and the single-exit warning below never
        # fired because it counted entries. A user then believes a run is
        # spread over fifty addresses when it is burning one.
        #
        # Said out loud rather than done silently: a pool quietly smaller
        # than the file that produced it is the same kind of surprise.
        seen, unique = set(), []
        for entry in proxies:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)
        if len(unique) < len(proxies):
            logger.warning(
                "Proxy pool: %d entries collapsed to %d distinct exit(s) — "
                "%d duplicate(s) dropped. Repeating an address does not "
                "spread a run across more of them.",
                len(proxies), len(unique), len(proxies) - len(unique))
        self._proxies = unique
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
