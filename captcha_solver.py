"""
captcha_solver.py
------------------
Shared helper used by all three scrapers (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation in the main loop of all three
scrapers, regardless of what URL was requested (category hub, product page,
sign-in, checkout, anything) — this is deliberate, not scoped to any one
page. If Amazon renders a reCAPTCHA v3 challenge anywhere — it does on
account and payment flows — this fires.

Flow:
  1. Both detectors run and are reconciled (see reconcile_detections) to decide
     the variant: v3, v2-invisible or v2-checkbox. The parameters differ per
     variant and are not interchangeable — v3 params sent for a v2-invisible
     widget buy a token the site rejects.
  2. The challenge goes to 2captcha's API (v2 by default, legacy v1 as a
     one-shot fallback) with the parameters for that variant.
  3. The resulting token is injected into the page's `g-recaptcha-response`
     textarea and any bound callback is invoked.

Often none of this is needed. Over the Scraping Browser API,
`Captcha.setAutoSolve` can clear the challenge inside the browser before this
code gets a turn — treat `Captcha.solveFinished` as the success signal and keep
this path as the fallback rather than assuming every detection completes.
This module is the path for a browser you launched yourself.

No other captcha vendor is integrated (per spec: no competitors).
"""

from __future__ import annotations

import base64
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests

logger = logging.getLogger("captcha_solver")

# 2captcha has two generations of solver API and both are live.
#
#   v2 (current, documented at https://2captcha.com/api-docs):
#       POST https://api.2captcha.com/createTask     {clientKey, task:{...}}
#       POST https://api.2captcha.com/getTaskResult  {clientKey, taskId}
#     JSON in, JSON out, typed task objects (RecaptchaV3TaskProxyless etc.).
#
#   v1 (legacy, still accepted):
#       POST https://2captcha.com/in.php   form-encoded, method=userrecaptcha
#       GET  https://2captcha.com/res.php  polling
#
# This module speaks v2 by default and keeps v1 as a fallback, because the
# original build was written against v1 and every live result recorded in the
# README came through it. Switch with solve_recaptcha(..., api_version="v1").
TWOCAPTCHA_API_V2 = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_TASK_URL = f"{TWOCAPTCHA_API_V2}/createTask"
TWOCAPTCHA_GET_RESULT_URL = f"{TWOCAPTCHA_API_V2}/getTaskResult"
TWOCAPTCHA_BALANCE_URL = f"{TWOCAPTCHA_API_V2}/getBalance"

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"

# v2 rejects an arbitrary minScore: the documented values are these three.
V3_ALLOWED_MIN_SCORES = (0.3, 0.7, 0.9)

@dataclass
class CaptchaChallenge:
    kind: str            # "recaptcha_v3" | "recaptcha_v2_invisible" | "recaptcha_v2"
    sitekey: str
    action: str = "verify"
    page_url: str = ""
    # How the challenge was found: "html" (static markup) or "runtime"
    # (read out of the live page's reCAPTCHA client config). Recorded
    # because the two paths can disagree about the version, and the
    # runtime one is authoritative when they do.
    source: str = "html"
    # Raw `size` from the reCAPTCHA client config, when available:
    # "invisible" for v2-invisible and for v3, absent for a v2 checkbox.
    size: Optional[str] = None

    @property
    def is_v3(self) -> bool:
        return self.kind == "recaptcha_v3"

    @property
    def is_invisible_v2(self) -> bool:
        return self.kind == "recaptcha_v2_invisible"


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    """Scan raw page HTML for a reCAPTCHA v3 challenge, in either of two
    real-world formats seen so far:

    1. An inline <script> calling grecaptcha.execute('SITEKEY', {action: '...'})
       directly — seen on Kohl's.
    2. A <captcha-widget> custom element carrying the config as HTML
       attributes (data-captcha-type="recaptcha" data-version="v3"
       data-sitekey="..." data-action="..."), with the actual execute()
       call happening inside a bundled JS file, never appearing as
       readable inline script text at all — confirmed live on the sibling
       farfetch-scraper repo's target,
       sign-up modal via DevTools inspection. A previous version of this
       function, which only checked for format 1, reported "no captcha"
       on this exact page despite one being genuinely present — caught by
       manually inspecting the DOM, not by the detector itself, which is
       exactly the gap this format-2 check closes.
    """
    if "recaptcha" not in html.lower():
        return None

    # Format 2 first — a structured HTML attribute match is more reliable
    # than the format-1 regex when both happen to be present.
    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
            )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3", sitekey=key_match.group(1), page_url=page_url)

    return None


# ---------------------------------------------------------------------------
# Runtime detection (added 2026-08-24 — the static-HTML detector below no
# longer matched the sibling farfetch-scraper repo's target)
# ---------------------------------------------------------------------------
#
# What changed there: the sign-up modal used to render
#   <captcha-widget data-version="v3" data-sitekey="6Leif..." ...>
# with the whole config in HTML attributes. Verified live on 2026-08-24, that
# element is GONE. The modal now loads
#   https://recaptcha.net/recaptcha/api.js?render=explicit
# and configures the widget entirely in JavaScript, into a
#   <div id="register-captcha" class="g-recaptcha">
# container. There is no `data-sitekey`, no inline `grecaptcha.execute(...)`,
# and no `grecaptcha.render` anywhere in the served HTML — all four things
# detect_recaptcha_v3() keys off are absent. It returns None on this page
# even though a real reCAPTCHA is present and active.
#
# The sitekey is still recoverable, but only at runtime, from two places
# that exist in the live page and not in its HTML:
#   1. window.___grecaptcha_cfg.clients — the reCAPTCHA API's own client
#      registry. Property names inside it are minified and change between
#      releases, so this walks the object looking for a value shaped like a
#      sitekey (^6L[\w-]{30,}$) rather than trusting any key name.
#   2. the reCAPTCHA iframe's `k=` query parameter — a stable, documented
#      part of the widget's URL, used as a cross-check and fallback.
#
# The same client config also carries `size`, which is what tells v3 apart
# from v2-invisible. That distinction is not cosmetic: 2captcha needs
# `version=v3` + `action` + `min_score` for one and `invisible=1` for the
# other, and sending the wrong one burns balance for a token that won't
# validate.
RECAPTCHA_DISCOVERY_JS = r"""
() => {
  const out = {found: false, sitekey: null, size: null, action: null,
               enterprise: false, containerId: null, hints: []};

  const SITEKEY_RE = /^6L[\w-]{30,}$/;

  // --- 1. the reCAPTCHA client registry -----------------------------------
  // Minified property names, so match on value shape, not key name.
  try {
    const clients = (window.___grecaptcha_cfg || {}).clients || null;
    if (clients) {
      for (const id of Object.keys(clients)) {
        const seen = new Set();
        const walk = (o, depth) => {
          if (!o || depth > 4 || seen.has(o)) return;
          if (typeof o === 'object') seen.add(o);
          for (const k of Object.keys(o)) {
            let v;
            try { v = o[k]; } catch (e) { continue; }
            if (typeof v === 'string') {
              if (!out.sitekey && SITEKEY_RE.test(v)) { out.sitekey = v; out.found = true; }
              else if (v === 'invisible' || v === 'normal' || v === 'compact') out.size = out.size || v;
            } else if (v && typeof v === 'object') {
              walk(v, depth + 1);
            }
          }
        };
        walk(clients[id], 0);
      }
      if (out.sitekey) out.hints.push('sitekey from ___grecaptcha_cfg');
    }
  } catch (e) { out.hints.push('cfg walk failed: ' + e.message); }

  // --- 2. the widget iframe's k= parameter --------------------------------
  try {
    for (const f of document.querySelectorAll('iframe[src*="recaptcha"]')) {
      const m = /[?&]k=([\w-]{20,})/.exec(f.getAttribute('src') || '');
      if (m) {
        if (!out.sitekey) { out.sitekey = m[1]; out.found = true; out.hints.push('sitekey from iframe k= param'); }
        else if (out.sitekey !== m[1]) out.hints.push('iframe k= disagrees with cfg: ' + m[1]);
        break;
      }
    }
  } catch (e) { out.hints.push('iframe scan failed: ' + e.message); }

  // --- 3. legacy paths, still checked in case they come back --------------
  try {
    const w = document.querySelector('captcha-widget[data-sitekey]');
    if (w) {
      out.found = true;
      out.sitekey = out.sitekey || w.getAttribute('data-sitekey');
      const dv = w.getAttribute('data-version');
      if (dv) out.hints.push('legacy captcha-widget data-version=' + dv);
      const da = w.getAttribute('data-action');
      if (da && da !== 'null') out.action = da;
    }
    const gr = document.querySelector('[data-sitekey]');
    if (gr && !out.sitekey) {
      out.sitekey = gr.getAttribute('data-sitekey'); out.found = !!out.sitekey;
      out.hints.push('sitekey from a data-sitekey attribute');
    }
  } catch (e) { out.hints.push('legacy scan failed: ' + e.message); }

  // --- 4. context -------------------------------------------------------
  try {
    const c = document.querySelector('.g-recaptcha[id], [id*="captcha"]');
    if (c) out.containerId = c.id || null;
    out.enterprise = !!(window.grecaptcha && window.grecaptcha.enterprise);
    out.badge = !!document.querySelector('.grecaptcha-badge');
    // A bframe iframe is the interactive challenge. v3 never shows one;
    // v2-invisible does when it decides to challenge.
    out.challengeFrame = !!document.querySelector('iframe[src*="bframe"]');
    out.scripts = [...document.querySelectorAll('script[src*="recaptcha"]')]
                    .map(s => (s.getAttribute('src') || '').split('?')[0]);
    // The api.js `render` parameter is the strongest v2-vs-v3 signal there
    // is, per Google's own docs: v3 loads api.js?render=<SITE_KEY>, while
    // v2 (checkbox and invisible alike) loads api.js?render=explicit.
    out.renderParam = null;
    for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
      const m = /[?&]render=([^&]+)/.exec(sc.getAttribute('src') || '');
      if (m) { out.renderParam = decodeURIComponent(m[1]); break; }
    }
  } catch (e) { out.hints.push('context scan failed: ' + e.message); }

  return out;
}
"""


def detect_recaptcha_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """Detect a reCAPTCHA by inspecting the LIVE page, not its HTML.

    `evaluate` is a callable that runs RECAPTCHA_DISCOVERY_JS in the page and
    returns the resulting dict — i.e. `page.evaluate` under Playwright,
    `driver.execute_script` under Selenium (wrap it so the arrow function is
    invoked), or an awaited `page.evaluate` under pyppeteer.

    Version inference, and why it is deliberately conservative:
      * `size == "invisible"` PLUS an interactive bframe iframe present is
        the signature of **v2-invisible**, not v3 — v3 never renders a
        challenge frame. This is what that site looked like as of
        2026-08-24, on the same sitekey that its old markup explicitly
        labelled `data-version="v3"`.
      * `size == "invisible"` with no challenge frame is treated as v3,
        which is v3's normal appearance (badge only).
      * a `normal`/`compact` size is a v2 checkbox.
    When the two signals disagree the ambiguity is recorded in the returned
    challenge's `kind` and in the log, rather than silently guessing — pick
    the wrong one and 2captcha returns a token the site rejects.
    """
    try:
        info = evaluate(RECAPTCHA_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        return None

    if not info or not info.get("found") or not info.get("sitekey"):
        return None

    size = info.get("size")
    render = info.get("renderParam")

    # Classification, strongest signal first.
    #
    # 1. api.js's `render` parameter. Per Google's docs, v3 loads
    #    api.js?render=<SITE_KEY> while v2 — checkbox and invisible alike —
    #    loads api.js?render=explicit. v3 has no `size` concept and never
    #    renders a challenge iframe at all.
    #      https://developers.google.com/recaptcha/docs/v3
    #      https://developers.google.com/recaptcha/docs/invisible
    #    So render=explicit rules v3 out outright, and render=<the sitekey>
    #    confirms it outright.
    # 2. `size`, which only exists for v2: invisible / normal / compact.
    # 3. presence of a bframe (interactive challenge) iframe — v2 only.
    if render and render != "explicit" and render == info.get("sitekey"):
        kind = "recaptcha_v3"
    elif render == "explicit":
        kind = "recaptcha_v2_invisible" if size == "invisible" else "recaptcha_v2"
    elif size in ("normal", "compact"):
        kind = "recaptcha_v2"
    elif info.get("challengeFrame") and size == "invisible":
        kind = "recaptcha_v2_invisible"
    else:
        kind = "recaptcha_v3"

    hints = info.get("hints") or []
    logger.info("reCAPTCHA found at runtime: kind=%s sitekey=%s size=%s render=%s "
                "challengeFrame=%s container=%s hints=%s",
                kind, info["sitekey"], size, render, info.get("challengeFrame"),
                info.get("containerId"), "; ".join(hints))
    if info.get("enterprise"):
        logger.warning("This is a reCAPTCHA ENTERPRISE widget — 2captcha needs its "
                       "enterprise method, which this project does not implement.")

    return CaptchaChallenge(
        kind=kind,
        sitekey=info["sitekey"],
        action=info.get("action") or "verify",
        page_url=page_url,
        source="runtime",
        size=size,
    )


def reconcile_detections(html_challenge: Optional[CaptchaChallenge],
                          runtime_challenge: Optional[CaptchaChallenge]
                          ) -> Optional[CaptchaChallenge]:
    """Pick between the two detectors when both find something.

    They can disagree on the SAME page, and on the site this was written
    against they did. A capture
    taken through the Scraping Browser on 2026-08-24 contains, simultaneously:

      * `<captcha-widget data-version="v3" data-sitekey="6Leif..."
         data-action="null">` — the site's own wrapper element, asserting v3
      * `recaptcha/api.js?render=explicit`, a client registered with
        `size: "invisible"`, and a `bframe` challenge iframe — the documented
        **v2-invisible** signature

    Both cannot be true. `data-version` is an attribute on the site's own
    component: it says what their code believes. `render=explicit` + `size` +
    the challenge frame describe the Google loader that is actually on the
    page, which is what Google enforces and therefore what 2captcha has to
    match. So the runtime reading wins, and the disagreement is logged rather
    than quietly resolved.

    Supporting detail: `data-action="null"` in that markup means there is no
    action string. v3 scores partly on the action; a real v3 integration
    passes one. An empty action fits a v2-invisible widget wearing a stale
    v3 label better than it fits working v3.

    (Also worth knowing: the same modal served to a European residential IP
    the same day had NO `<captcha-widget>` element at all — just the
    `render=explicit` loader. That site served more than one variant of this
    modal, so neither detector alone is enough.)
    """
    if runtime_challenge and not html_challenge:
        return runtime_challenge
    if html_challenge and not runtime_challenge:
        return html_challenge
    if not html_challenge and not runtime_challenge:
        return None

    if html_challenge.kind != runtime_challenge.kind:
        logger.warning(
            "Detectors disagree on this page: static markup says %s (from the "
            "site's own data-version), the live loader says %s (render/size/"
            "challenge-frame). Trusting the loader — that's what Google "
            "enforces and what 2captcha has to match.",
            html_challenge.kind, runtime_challenge.kind)
        # Keep the action if the static markup had a real one; the runtime
        # path often can't see it.
        if html_challenge.action and html_challenge.action != "verify":
            runtime_challenge.action = html_challenge.action
    return runtime_challenge


def _v2_task_for(challenge: CaptchaChallenge, min_score: float) -> dict:
    """Build the API-v2 `task` object for a challenge.

    Task type per variant, from https://2captcha.com/api-docs:
      * v3           -> RecaptchaV3TaskProxyless, with pageAction + minScore
      * v2 invisible -> RecaptchaV2TaskProxyless with isInvisible: true
      * v2 checkbox  -> RecaptchaV2TaskProxyless

    The `*Proxyless` types let 2captcha use its own IP pool. The non-proxyless
    variants (RecaptchaV2Task) exist for when the token must be produced from
    the same IP that will submit it; that needs proxyType/proxyAddress/
    proxyPort/proxyLogin/proxyPassword and is not wired up here — with the
    Scraping Browser API the page and the solve already share an exit IP.
    """
    if challenge.is_v3:
        # minScore is not free-form: 0.3 / 0.7 / 0.9 are the documented values.
        score = min(V3_ALLOWED_MIN_SCORES,
                    key=lambda allowed: abs(allowed - min_score))
        if score != min_score:
            logger.info("minScore %.2f is not one of %s — using %.1f.",
                        min_score, V3_ALLOWED_MIN_SCORES, score)
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "minScore": score,
        }
        # v3 scores partly on the action, so send it when it's a real one.
        # "verify" is this module's placeholder for "the page didn't say".
        if challenge.action and challenge.action != "verify":
            task["pageAction"] = challenge.action
        return task

    task = {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.is_invisible_v2:
        task["isInvisible"] = True
    return task


def _solve_with_2captcha_v2(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 180) -> str:
    """Solve via API v2: createTask, then poll getTaskResult."""
    task = _v2_task_for(challenge, min_score)
    logger.info("createTask: %s (sitekey=%s)", task["type"], challenge.sitekey)

    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task},
                            timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id},
                            timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            # v2 returns the same string under both names.
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if not token:
                raise RuntimeError(f"task ready but no token in solution: {solution}")
            logger.info("2captcha solved %s in ~%ds.", challenge.kind, waited)
            return token
        # status == "processing"

    raise TimeoutError(f"2captcha did not return a token within {max_wait}s")


def get_balance(api_key: str) -> float:
    """Account balance via API v2 — handy for a preflight check."""
    r = requests.post(TWOCAPTCHA_BALANCE_URL, json={"clientKey": api_key}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("errorId"):
        raise RuntimeError(f"getBalance failed: {d.get('errorCode')}")
    return float(d["balance"])


def _solve_with_2captcha_v1(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 120) -> str:
    """Legacy API v1: submit to in.php, poll res.php.

    Kept as a fallback because every live result recorded in this project's
    README came through this path. Prefer v2 for new work.

    The parameters differ per variant and are NOT interchangeable — send v3
    parameters for a v2-invisible widget and you pay for a token the site
    then rejects:
      * v3            -> version=v3, action=..., min_score=...
      * v2 invisible  -> invisible=1, no action, no min_score
      * v2 checkbox   -> neither
    """
    payload = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }
    if challenge.is_v3:
        payload.update({"version": "v3", "action": challenge.action, "min_score": min_score})
    elif challenge.is_invisible_v2:
        payload["invisible"] = 1

    logger.info("Submitting to 2captcha as %s (sitekey=%s)", challenge.kind, challenge.sitekey)
    submit = requests.post(TWOCAPTCHA_IN_URL, data=payload, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        result = requests.get(TWOCAPTCHA_RES_URL, params={
            "key": api_key, "action": "get", "id": task_id, "json": 1,
        }, timeout=30).json()
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                     api_version: str = "v2", min_score: float = 0.7) -> str:
    """Public entry point: solve `challenge` through 2captcha and return the token.

    There used to be a `use_antidetect` branch here, behind a CLI flag of the
    same name, that POSTed to a hardcoded local "solve-captcha" endpoint.
    It was removed before publishing: that endpoint was a placeholder for a
    product that does not exist under that name, so the flag could not work for
    anyone who set it, and the real "the browser solves it for you" path is
    `Captcha.setAutoSolve` over the Scraping Browser API. A flag that cannot
    succeed is worse than a missing feature — it reads as an option.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "A captcha was detected but no 2captcha API key was given. Pass "
            "--twocaptcha-key, or set TWOCAPTCHA_KEY. Over the Scraping Browser "
            "API you may not need either: Captcha.setAutoSolve can clear it "
            "inside the browser."
        )
    solver = _solve_with_2captcha_v1 if api_version == "v1" else _solve_with_2captcha_v2
    return solver(twocaptcha_api_key, challenge, min_score=min_score)


INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


# Kept as an alias: this function used to be reCAPTCHA-v3-only, and the three
# scrapers plus both modal diagnostics import it under the old name. The
# solver now branches on challenge.kind, so the name is a misnomer — the
# alias exists so older call sites keep working rather than to encourage it.
solve_recaptcha_v3 = solve_recaptcha


# ===========================================================================
# Amazon's own image captcha
# ===========================================================================
# Amazon does not use reCAPTCHA on the paths this scraper reads. Its block
# page is a first-party image captcha — "Enter the characters you see below"
# with a JPEG of distorted text and a GET form to /errors/validateCaptcha.
# The reCAPTCHA machinery above is kept, not replaced: Amazon does serve
# reCAPTCHA on account and payment flows, different geos surface different
# challenges, and the family's rule is that detection stays broad. What
# changes here is only which detector fires on which page.
#
# NOT LIVE-VERIFIED. Every field in the shapes below comes from Amazon's
# captcha page markup, but no run during development was served one: 47
# navigations across five marketplaces produced AWS WAF challenges (which
# clear themselves) and 503 throttles, and no image captcha at all. So this
# path is written from the markup and exercised only by the offline fixture
# in smoke_test.py. Treat a live solve as unproven until someone reports one;
# see TROUBLESHOOTING.md.

AMAZON_CAPTCHA_MARKERS = (
    "/errors/validateCaptcha",
    "Enter the characters you see below",
    "Type the characters you see in this image",
    "opfcaptcha-prod",
)


@dataclass
class AmazonImageChallenge:
    """An Amazon image captcha, with everything needed to answer it.

    `amzn` and `amzn_r` are hidden form fields carrying the challenge id and
    the URL to return to. Both must be echoed back verbatim: the solution
    text alone is not enough, and dropping `amzn_r` lands the browser on the
    home page having spent a solve.
    """
    image_url: str
    page_url: str = ""
    form_action: str = "/errors/validateCaptcha"
    amzn: str = ""
    amzn_r: str = ""
    # Kept so log lines and the smoke suite can talk about this the same way
    # they talk about a reCAPTCHA challenge.
    kind: str = "amazon_image"
    source: str = "html"


_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+src="(https?://[^"]*(?:captcha|Captcha)[^"]*)"', re.IGNORECASE)
_HIDDEN_FIELD_RE = re.compile(
    r'<input[^>]+type="hidden"[^>]+name="(amzn(?:-r)?)"[^>]+value="([^"]*)"',
    re.IGNORECASE)
_HIDDEN_FIELD_ALT_RE = re.compile(
    r'<input[^>]+name="(amzn(?:-r)?)"[^>]+value="([^"]*)"', re.IGNORECASE)
_FORM_ACTION_RE = re.compile(
    r'<form[^>]+action="([^"]*validateCaptcha[^"]*)"', re.IGNORECASE)


def detect_amazon_captcha(html: str, page_url: str = "") -> Optional[AmazonImageChallenge]:
    """An AmazonImageChallenge if `html` is Amazon's captcha page, else None.

    Requires BOTH a captcha marker and an image URL. A marker alone is not
    enough: the string "/errors/validateCaptcha" appears in Amazon's own
    error-handling JavaScript on ordinary pages, so keying on it alone would
    report a captcha on every page of a healthy run — and under
    `--solve-captcha always` that would buy a solve per page.
    """
    if not html or not any(m in html for m in AMAZON_CAPTCHA_MARKERS):
        return None
    img = _CAPTCHA_IMG_RE.search(html)
    if not img:
        return None

    fields = dict(_HIDDEN_FIELD_RE.findall(html)) or dict(_HIDDEN_FIELD_ALT_RE.findall(html))
    action = _FORM_ACTION_RE.search(html)
    return AmazonImageChallenge(
        image_url=img.group(1),
        page_url=page_url,
        form_action=action.group(1) if action else "/errors/validateCaptcha",
        amzn=fields.get("amzn", ""),
        amzn_r=fields.get("amzn-r", ""),
    )


def _solve_image_v2(api_key: str, image_b64: str, poll_interval: int = 5,
                    max_wait: int = 180) -> str:
    """ImageToTextTask via API v2: createTask, then poll getTaskResult."""
    task = {"type": "ImageToTextTask", "body": image_b64}
    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task}, timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id}, timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            text = (result.get("solution") or {}).get("text")
            if not text:
                raise RuntimeError(
                    f"task ready but no text in solution: {result.get('solution')}")
            logger.info("2captcha read the captcha image in ~%ds.", waited)
            return text
    raise TimeoutError(f"2captcha did not return a solution within {max_wait}s")


def _solve_image_v1(api_key: str, image_b64: str, poll_interval: int = 5,
                    max_wait: int = 120) -> str:
    """Legacy API v1 image solve: in.php with method=base64, poll res.php."""
    submitted = requests.post(TWOCAPTCHA_IN_URL,
                              data={"key": api_key, "method": "base64",
                                    "body": image_b64, "json": 1}, timeout=30)
    submitted.raise_for_status()
    payload = submitted.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"in.php refused the image: {payload.get('request')}")
    task_id = payload["request"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.get(TWOCAPTCHA_RES_URL,
                           params={"key": api_key, "action": "get",
                                   "id": task_id, "json": 1}, timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("status") == 1:
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"res.php returned an error: {result.get('request')}")
    raise TimeoutError(f"2captcha did not return a solution within {max_wait}s")


def solve_amazon_captcha(challenge: AmazonImageChallenge,
                         twocaptcha_api_key: Optional[str],
                         fetch_image, api_version: str = "v2") -> str:
    """Solve `challenge` and return the characters to submit.

    `fetch_image(url) -> bytes` is supplied by the caller rather than done
    here with `requests`, and that is deliberate: the captcha image is bound
    to the session that was served the captcha. Downloading it from this
    process would fetch it over a different connection, from a different IP
    when a proxy is in use, and Amazon then rejects a correct answer to an
    image it does not consider ours. Every engine passes its browser's own
    request context.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "Amazon served an image captcha but no 2captcha API key was "
            "given. Pass --twocaptcha-key, or set TWOCAPTCHA_KEY.")
    image = fetch_image(challenge.image_url)
    if not image:
        raise RuntimeError(f"Could not fetch the captcha image {challenge.image_url}")
    image_b64 = base64.b64encode(image).decode("ascii")
    solver = _solve_image_v1 if api_version == "v1" else _solve_image_v2
    return solver(twocaptcha_api_key, image_b64)


def amazon_captcha_submit_url(challenge: AmazonImageChallenge, solution: str) -> str:
    """The URL that submits `solution`.

    Amazon's captcha form is a GET, so the answer is a navigation rather than
    a token injected into the DOM — which is why there is no Amazon
    equivalent of INJECT_TOKEN_JS. `amzn-r` is echoed back so the redirect
    after a correct answer returns to the page the run actually wanted.
    """
    base = urljoin(challenge.page_url or "https://www.amazon.com/",
                   challenge.form_action)
    query = [("amzn", challenge.amzn), ("amzn-r", challenge.amzn_r),
             ("field-keywords", solution)]
    parts = urlparse(base)
    existing = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k not in ("amzn", "amzn-r", "field-keywords")]
    return urlunparse(parts._replace(query=urlencode(existing + query)))
