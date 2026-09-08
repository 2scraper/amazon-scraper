"""
page_flow.py
------------
Amazon's page-state policy, shared by all three engines.

Why this module exists, when the rest of this family keeps each engine
self-contained: Amazon answers a request in five different ways, and four of
them want a different response.

    ok             parse it
    waf_challenge  wait — the browser clears it itself, for free
    throttled      retry, then a different exit
    captcha        solve it (this is the paid path)
    signin         stop; no proxy or solve changes it

On top of that, two page kinds need coaxing before they hold their content:
a best-seller grid lazy-loads 30 -> 50 cards, and a detail page is served in
two variants, one of which carries no reviews at all and cannot be talked
into it without a fresh session.

Three copies of that would drift, and the drift would be silent — an engine
that treats the WAF interstitial as a block reports exit 3 where its twin
reports exit 0 on the same page. The family already shares
output_writer.finish_run() for exactly this reason ("all three must agree on
exit codes, run status, and whether a run crashes or spends money"); this is
the same argument applied to the decisions that come before it.

The functions here are either pure or driven through small callables, so
each engine passes its own driver's primitives and keeps its browser
plumbing to itself:

    count(selector) -> int                how many elements match
    page_height() -> int                  document.body.scrollHeight
    scroll_to_bottom() -> None            jump to the end of the document
    scroll_into_view(selector) -> None    best-effort, may be a no-op
    sleep(ms) -> None                     the driver's own wait
    content() -> Optional[str]            current HTML, None if unavailable
    current_url() -> str                  the URL the browser is on

Deliberately no `evaluate(js)`: passing JavaScript from here would decide its
dialect for every driver, and they disagree — Playwright and pyppeteer take
`() => expr`, while Selenium's execute_script takes `return expr;`. Naming
the OPERATION instead keeps this module free of any driver's flavour.

Every value here is measured; the numbers are in the comments and in the
README, and the measurements are dated because Amazon's markup will move.
"""

import logging
from typing import Optional

from product_parser import detect_page_state, listing_kind

logger = logging.getLogger("page_flow")


# What "the page has painted" means, per mode. The listing form deliberately
# matches the PRIMARY anchors and not `a[href*="/dp/"]`: a captured search
# page carries 143 such links for 22 organic tiles, most of them in carousels
# that render before the grid does, so counting those would call the page
# ready while the results were still empty.
READY_SELECTOR_LISTING = ('div[data-component-type="s-search-result"][data-asin], '
                          '[id^="p13n-asin-index-"]')
READY_SELECTOR_PRODUCT = "#productTitle"
READY_SELECTOR_REVIEWS = 'div[data-hook="reviewContainer"]'

# Ordered most-durable first, per the family rule: a standards-based signal
# before a build artefact. `<link rel="next">` is FIRST and is known to be
# ABSENT — checked on both listing kinds on 2026-09-08, zero matches. It is
# kept because it costs one query and is the form that would survive a
# redesign, but it is explicitly not what makes pagination work here:
# `a.s-pagination-next` is a class Amazon can rename any day, and
# product_parser.page_url() is what actually backs pagination up.
NEXT_PAGE_SELECTOR = ("link[rel='next'], a.s-pagination-next, "
                      "a[aria-label*='next page'], li.a-last > a")

# How many primary anchors must appear before a LISTING counts as loaded
# rather than as a lucky single match. Must be > 1: waiting for one resolves
# on an unrelated element long before the grid paints. Five, against measured
# floors of 16 organic tiles on amazon.com, 16 on amazon.de, 60 on
# amazon.co.jp and 30-before-scroll on a best-seller grid.
MIN_CARD_MATCHES = 5

# How long to let the AWS WAF interstitial clear itself. Measured at 3.9s on
# amazon.co.uk; 25s is generous for a slow exit and still bounded, because
# every remote call in this project is bounded and an unbounded wait here
# would hang a run on a challenge that is never going to resolve.
WAF_WAIT_SECONDS = 25

# The container each mode's content hangs off, used to pull a lazy-load
# trigger precisely instead of jumping past it.
HYDRATION_ANCHORS = {
    "reviews": "#reviewsMedley, #customerReviews",
    "product": "#feature-bullets, #altImages",
}

# A listing gets the full 20s: a grid can genuinely be slow to paint. A
# detail page gets 8s, because its content is either in the markup that
# arrived or it is not, and the remaining 12s would be spent waiting for a
# variant that is never going to fill itself.
CONTENT_TIMEOUT_MS = {"listing": 20000, "product": 8000, "reviews": 8000}

# Tracking parameters on Amazon's own pagination links. None of them select
# content — they are search-session and click-attribution ids — so a strict
# URL comparison would decide the site's link disagrees with the ?page=N
# convention on every single run, and every run would refuse --concurrency.
TRACKING_PARAMS = {"xpid", "qid", "ref", "ref_", "_encoding", "sprefix", "crid",
                   "sr", "th", "psc", "linkCode", "tag"}


def ready_selector(mode: str) -> str:
    return {"product": READY_SELECTOR_PRODUCT,
            "reviews": READY_SELECTOR_REVIEWS}.get(mode, READY_SELECTOR_LISTING)


def min_matches(mode: str) -> int:
    """How many readiness anchors mean "loaded", for this mode.

    A listing needs several. A detail page has exactly one `#productTitle`,
    so requiring more than one would time out on every successful fetch —
    the threshold has to follow the mode or it silently inverts.
    """
    return MIN_CARD_MATCHES if mode == "listing" else 0


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def needs_scrolling(mode: str, url: str) -> bool:
    """Whether this page grows, or fills in, as you scroll.

    Search results do not: a captured page held the same 16 tiles before and
    after scrolling to the bottom, because they are server-rendered. Two page
    kinds do, both measured — a best-seller grid goes 30 -> 50, and a detail
    page's image strip sits below the fold. Scrolling everything anyway would
    add seconds per page for nothing on the most common path.
    """
    return mode in ("product", "reviews") or listing_kind(url) == "bestsellers"


def wait_out_waf_challenge(content, current_url, sleep) -> bool:
    """Sit through the AWS WAF interstitial. True if the real page arrived.

    Amazon answers a suspicious request with HTTP 202 and a page whose only
    content is `challenge.js` plus a script that calls
    `AwsWafIntegration.getToken()` and then reloads. There is nothing to
    solve and nothing to pay for — a real browser produces the token itself.
    Measured clearing in 3.9s on amazon.co.uk on 2026-09-08.

    This is the single most important difference from the rest of this
    family: reporting it as a block (exit 3) would report a block that does
    not exist, and handing it to the captcha solver would bill for a
    challenge no solver can answer.
    """
    remaining, seen, waited = WAF_WAIT_SECONDS, False, 0
    while waited < remaining:
        html = content()
        if html is None:
            sleep(500)
            waited += 0.5
            continue
        if detect_page_state(html, url=current_url()) != "waf_challenge":
            if seen:
                logger.info("AWS WAF challenge cleared itself in ~%.0fs — no "
                            "solve needed.", waited)
            return True
        if not seen:
            logger.info("AWS WAF challenge page (the browser solves this one "
                        "itself) — waiting up to %ds for it to reload.",
                        WAF_WAIT_SECONDS)
            seen = True
        sleep(1000)
        waited += 1
    logger.warning("The AWS WAF challenge had not cleared after %ds. Parsing "
                   "whatever is there; the run will report what it finds.",
                   WAF_WAIT_SECONDS)
    return False


def scroll_until_stable(count, page_height, scroll_to_bottom, sleep,
                        selector: str, max_rounds: int = 30,
                        pause_ms: int = 500, stable_rounds: int = 3) -> int:
    """Scroll to the document's end until `selector`'s count stops growing.

    Two details were both learned from a run that silently lost data, and
    both matter:

      * The page is jumped to `document.body.scrollHeight` rather than
        wheeled a fixed distance. A 2400px wheel per round stopped three
        rounds short of the bottom on a 7600px grid, so the lazy-load
        trigger was never reached and the run took 30 of a 50-card chart
        while looking settled — the ranks made it visible (1-30, then page 2
        starting at 51).
      * The count must hold still for `stable_rounds` rounds, not one.
        Amazon's next batch takes longer to arrive than a single pause, so
        one quiet round is not the end of the content.

    Bounded by max_rounds so a page that appends forever cannot hold the run.
    """
    previous, previous_height, unchanged = -1, -1, 0
    for _ in range(max_rounds):
        current = count(selector)
        height = page_height()
        if current == previous and height == previous_height:
            unchanged += 1
            if unchanged >= stable_rounds:
                return current
        else:
            unchanged = 0
        previous, previous_height = current, height
        scroll_to_bottom()
        sleep(pause_ms)
    logger.info("Content was still growing after %d scroll rounds (%d matches) "
                "— continuing with what is loaded.", max_rounds, previous)
    return previous


def hydrate(count, page_height, scroll_to_bottom, sleep, scroll_into_view,
            mode: str, selector: str, threshold: int, attempts: int = 3) -> int:
    """Pull the lazy-load trigger for this mode, and confirm it fired.

    Scrolls the mode's own anchor into view first, because Amazon hydrates
    some widgets from an intersection observer on a specific element and
    jumping straight to the end of the document flies past it without ever
    intersecting it. Then the ordinary bottom-ward pass, for anything below.
    """
    anchor = HYDRATION_ANCHORS.get(mode)
    for attempt in range(1, attempts + 1):
        if anchor:
            scroll_into_view(anchor)
        loaded = scroll_until_stable(count, page_height, scroll_to_bottom,
                                     sleep, selector)
        if loaded > threshold:
            return loaded
        if attempt < attempts:
            logger.info("Nothing matched %s yet (attempt %d/%d) — scrolling "
                        "its anchor into view again.", selector, attempt, attempts)
            sleep(1500)
    return count(selector)


# Why an empty review widget needs a NEW SESSION rather than a reload or a
# scroll. Three measurements, in the order they were taken, because the first
# two conclusions were wrong:
#
#   1. Amazon serves two variants of the same /dp/ URL — one with the reviews
#      in the initial markup (13 containers, ~466 [data-hook] elements, 17.5k
#      page height), one where the widget is present but empty (5
#      [data-hook] elements, 10.8k height). Six scroll passes left the second
#      at zero, so it is not a lazy load.
#   2. Reloading does not help: a review-less context stayed review-less
#      across six consecutive navigations (0,0,0,0,0,0). The variant is
#      sticky to the SESSION.
#   3. A fresh context does help: 3 of 5 new contexts returned the full page,
#      and 4 of 8 in an earlier sample.
#
# So the variant is assigned per cookie jar, and a new browser is the only
# thing that re-rolls it — the same tool a proxy rotation uses, for the same
# reason: a session cannot be talked out of what it has already been
# assigned. Over a remote CDP endpoint the profile's cookies are not ours to
# discard, so there a review-less profile stays review-less and a different
# `pid` is the answer.
# How many sessions to spend re-rolling the served variant, and how many
# scroll passes to spend before giving up on one. Both are measured, not
# guessed.
#
# The variant hit rate is roughly 55%: 4 of 8 fresh contexts in one sample,
# 3 of 5 in another, and 5 of 9 across three Selenium configurations
# (headless, headless without the automation flag, and headful — the rate did
# not depend on any of them, which is worth knowing because it rules out the
# obvious suspicion that headless is being singled out). At that rate three
# sessions still come back empty about one run in eight, which is too often
# for a mode anyone would put in a pipeline; five bring it under one in fifty.
#
# This is deliberately NOT --retries. That flag is the user's budget for
# transient failures, and spending it on a coin flip Amazon is holding would
# mean a run that asked for one retry gets one chance at the content.
VARIANT_REROLL_ATTEMPTS = 5

# Scroll passes per session. One for reviews, because scrolling was measured
# NOT to be what fills the empty variant — six passes left it at zero — so
# further passes only add seconds to a session that is already lost. Three
# elsewhere, where lazy loading is real and a pass genuinely adds cards.
HYDRATE_ATTEMPTS = {"reviews": 1}


def session_attempts(mode: str, retries: int) -> int:
    """How many browser sessions one page may consume.

    Reviews get their own budget because their failure is a served variant
    rather than a transient fault; see VARIANT_REROLL_ATTEMPTS.
    """
    if mode == "reviews":
        return max(retries, VARIANT_REROLL_ATTEMPTS)
    return retries


def hydrate_attempts(mode: str) -> int:
    return HYDRATE_ATTEMPTS.get(mode, 3)


VARIANT_RETRY_NOTE = (
    "This session was served the variant of the page that carries no such "
    "content — starting a fresh browser session, which is what re-rolls it")
