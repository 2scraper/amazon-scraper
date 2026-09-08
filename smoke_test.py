#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# The encoding declaration is REQUIRED here and is not a Python-2 relic, so
# please do not tidy it away. CPython 3.9's tokenizer, with no declared
# encoding, fails on a multi-byte character that sits far inside a very long
# source line: the amazon.co.jp fixture below is one 21,000-character line
# whose first Japanese character is at column 3,654, and without this cookie
# `python3 smoke_test.py` dies with "Non-UTF-8 code starting with '\xe3' ...
# but no encoding declared" while `compile()` on the very same bytes
# succeeds. Reproduced on 3.9.6 down to a two-line file. Declaring the
# encoding is what PEP 263 is for.
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for amazon-scraper.

Run this FIRST, before touching a real browser or amazon.com, to confirm the
environment and the parsing/output/policy logic work:

    python3 smoke_test.py

Deliberately ONE file of plain functions with inline fixtures — no pytest, no
conftest, no fixtures directory. tests/test_smoke.py wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks that could drift from this one.

It must pass with NO engine library installed at all, so every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is REPORTED. CI's engine-smoke job installs all three and
fails if anything reports skipped, because "skipped, engine absent" reads
identically to a real import error.

Exits non-zero on any failure.
"""

import ast
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import fields

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, detect_amazon_captcha,
                            amazon_captcha_submit_url, AMAZON_CAPTCHA_MARKERS)
from diff_runs import diff_products
import env_config
import page_flow
from output_writer import (Product, Review, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                           EXIT_PARTIAL, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
import product_parser
from product_parser import (parse_products, parse_product_detail, parse_reviews,
                            page_url, category_from_url, listing_kind,
                            marketplace_host, marketplace_info, MARKETPLACES,
                            detect_page_state, detect_bot_challenge,
                            page_number_from_url, SELECTORS)
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)
# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# Every one of these is a REAL capture, taken 2026-09-08 from a European exit
# IP, with <script>/<style>/<svg> stripped and nothing else changed — the
# parser produces byte-identical rows from the trimmed and untrimmed forms,
# checked before they were pasted here. Hand-written HTML would test what we
# believe Amazon serves rather than what it does.

# A SPONSORED search tile carrying a coupon and a list price, captured
# from /s?k=wireless+headphones on amazon.com, 2026-09-08. Pins: the
# sponsored markers, the coupon label being rendered twice, and the
# `.a-price` node whose own text is the price concatenated with itself.
FIX_TILE_SPONSORED_COUPON = """<div class="sg-col-20-of-24 s-result-item s-asin sg-col-0-of-12 sg-col-16-of-20 AdHolder sg-col s-widget-spacing-small gsx-ies-anchor sg-col-12-of-16" data-asin="B0HGFFLDYL" data-cel-widget="search_result_2" data-component-id="16" data-component-type="s-search-result" data-index="3" data-uuid="ab2be9b1-4330-4f3e-a7f9-4b47b5fa7483" id="ab2be9b1-4330-4f3e-a7f9-4b47b5fa7483" role="listitem"><div class="sg-col-inner"><div cel_widget_id="MAIN-SEARCH_RESULTS-3" class="s-widget-container s-spacing-small s-widget-container-height-small celwidget slot=MAIN template=SEARCH_RESULTS widgetId=search-results_2" data-cel-widget="MAIN-SEARCH_RESULTS-3" data-csa-c-asin-instance-id="6c35a975-9668-437c-9eca-aca1fe7d714c" data-csa-c-content-id="search-results_2" data-csa-c-cs-type="Loom" data-csa-c-id="xpli8o-w4ta9d-txhyd1-cw4z04" data-csa-c-item-id="amzn1.asin.1.B0HGFFLDYL" data-csa-c-pos="2" data-csa-c-type="item" data-csa-op-log-render="">
<div class="rush-component" data-component-id="17" data-component-props='{"percentageShownToFire":"50","batchable":true,"requiredElementSelector":".s-image:visible","url":"https://unagi-na.amazon.com/1/events/com.amazon.eel.SponsoredProductsEventTracking.prod?qualifier=1788871867&amp;id=2531832705116201&amp;widgetName=sp_atf&amp;adId=301491933541802&amp;eventType=1&amp;adIndex=1"}' data-component-type="s-impression-logger">
<div class="rush-component s-featured-result-item" data-component-id="3" data-component-props='{"presenceCounterName":"sp_delivered","hiddenCounterName":"sp_hidden","testElementSelector":".s-image"}' data-component-type="s-impression-counter">
<span class="a-declarative" data-action="puis-card-container-declarative" data-csa-c-func-deps="aui-da-puis-card-container-declarative" data-csa-c-id="4305rp-mecv49-xoqmvh-p2mc59" data-csa-c-item-id="amzn1.asin.B0HGFFLDYL:amzn1.deal.592f04cf:amzn1.promotion.A30NTRO0CA4JD2" data-csa-c-owner="puis" data-csa-c-posx="2" data-csa-c-type="item" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="puis-card-container puis-overflow-hidden s-card-container aok-relative desktop-list-view puis-include-content-margin puis puis-v1v3dkg82cns7b2ucnfxri66dtm s-latency-cf-section puis-card-border" data-cy="asin-faceout-container"><div class="a-section"><div class="puisg-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24 puis-list-col-left"><div class="puisg-col-inner"><div class="a-section a-spacing-none aok-relative puis-status-badge-container s-list-status-badge-container"></div><div class="s-product-image-container aok-relative s-text-center s-image-overlay-grey puis-image-overlay-grey s-padding-left-small s-padding-right-small puis-flex-expand-height puis puis-v1v3dkg82cns7b2ucnfxri66dtm" data-cy="image-container"><div class="aok-relative"><span class="rush-component" data-component-type="s-product-image" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-hidden="true" class="a-link-normal s-no-outline" href="/sspa/click?ie=UTF8&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6&amp;url=%2FCancelling-Headphones-Bluetooth-Transparency-Graduation%2Fdp%2FB0HGFFLDYL%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ%26dib_tag%3Dse%26keywords%3Dwireless%2Bheadphones%26qid%3D1788871867%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" tabindex="-1"><div class="a-section aok-relative s-image-fixed-height"><img alt="Sponsored Ad - Hybrid Active Noise Cancelling Headphones, Over-Ear Bluetooth Headphone with Hi-Res Audio, 90H Playtime, De..." aria-hidden="true" class="s-image" data-image-index="2" data-image-latency="s-product-image" data-image-load="" data-image-source-density="1" src="https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY218_.jpg" srcset="https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY218_.jpg 1x, https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY327_FMwebp_QL65_.jpg 1.5x, https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY436_FMwebp_QL65_.jpg 2x, https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY545_FMwebp_QL65_.jpg 2.5x, https://m.media-amazon.com/images/I/61uYS4lWu3L._AC_UY654_FMwebp_QL65_.jpg 3x"/></div></a></span></div></div></div></div><div class="puisg-col puisg-col-0-of-4 puisg-col-0-of-8 puisg-col-4-of-12 puisg-col-8-of-16 puisg-col-12-of-20 puisg-col-12-of-24 puis-list-col-right"><div class="puisg-col-inner"><div class="a-section a-spacing-small a-spacing-top-small"><div class="a-section a-spacing-none puis-padding-right-small s-title-instructions-style" data-cy="title-recipe"><div class="a-row a-spacing-micro"><span class="a-declarative" data-a-popover='{"closeButtonLabel":"Close","closeButton":"true","dataStrategy":"preload","name":"sp-info-popover-B0HGFFLDYL","position":"triggerVertical","popoverLabel":"View Sponsored information or leave ad feedback"}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="k5u3n1-8ngkrs-9f9jbt-f6vac9" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="puis-label-popover puis-sponsored-label-text" href="javascript:void(0)" role="button" style="text-decoration: none;"><span class="puis-label-popover-default"><span aria-label="View Sponsored information or leave ad feedback" class="a-color-secondary">Sponsored</span></span><span class="puis-label-popover-hover"><span aria-hidden="true" class="a-color-base">Sponsored</span></span> <span class="aok-inline-block puis-sponsored-label-info-icon"></span></a></span><div class="a-popover-preload" id="a-popover-sp-info-popover-B0HGFFLDYL"><div class="puis puis-v1v3dkg82cns7b2ucnfxri66dtm"><span>You’re seeing this ad based on the product’s relevance to your search query.</span><div class="a-row"><span class="a-declarative" data-action="s-safe-ajax-modal-trigger" data-csa-c-func-deps="aui-da-s-safe-ajax-modal-trigger" data-csa-c-id="cs7vp0-8b1zdh-2aus09-adet0q" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-safe-ajax-modal-trigger='{"ajaxUrl":"/af/sp-loom/feedback-form?pl=%7B%22adPlacementMetaData%22%3A%7B%22searchTerms%22%3A%22d2lyZWxlc3MgaGVhZHBob25lcw%3D%3D%22%2C%22pageType%22%3A%22Search%22%2C%22feedbackType%22%3A%22sponsoredProductsLoom%22%2C%22slotName%22%3A%22TOP%22%7D%2C%22adCreativeMetaData%22%3A%7B%22adProgramId%22%3A1024%2C%22adCreativeDetails%22%3A%5B%7B%22asin%22%3A%22B0HGFFLDYL%22%2C%22title%22%3A%22Hybrid+Active+Noise+Cancelling+Headphones%2C+Over-Ear+Bluetooth+Headphone+with+Hi-Res+Audio%2C+90H+Playt%22%2C%22priceInfo%22%3A%7B%22amount%22%3A98.98%2C%22currencyCode%22%3A%22USD%22%7D%2C%22sku%22%3A%22DB-G901-black-FBA%22%2C%22adId%22%3A%22A0243947K8M2J9KLOEJM%22%2C%22campaignId%22%3A%22A01296282URPBA5YIQ3RA%22%2C%22advertiserIdNS%22%3Anull%2C%22selectionSignals%22%3Anull%7D%5D%7D%7D","dataStrategy":"ajax","header":"Leave feedback"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="#"><span>Leave ad feedback</span> </a> </span></div></div></div></div><a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" href="/sspa/click?ie=UTF8&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6&amp;url=%2FCancelling-Headphones-Bluetooth-Transparency-Graduation%2Fdp%2FB0HGFFLDYL%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ%26dib_tag%3Dse%26keywords%3Dwireless%2Bheadphones%26qid%3D1788871867%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1"><h2 aria-label="Sponsored Ad - Hybrid Active Noise Cancelling Headphones, Over-Ear Bluetooth Headphone with Hi-Res Audio, 90H Playtime, Deep Bass, Transparency Mode, Foldable Comfort Cups, Graduation Gifts for Travel Office - Black" class="a-size-medium a-spacing-none a-color-base a-text-normal"><span>Hybrid Active Noise Cancelling Headphones, Over-Ear Bluetooth Headphone with Hi-Res Audio, 90H Playtime, Deep Bass, Transparency Mode, Foldable Comfort Cups, Graduation Gifts for Travel Office - Black</span></h2></a> </div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="reviews-block"><div class="a-row a-size-small"><span aria-hidden="true" class="a-size-small a-color-base">4.6</span><span class="a-declarative" data-a-popover='{"url":"/review/widgets/average-customer-review/popover/ref=acr_search__popover?ie=UTF8&amp;asin=B0HGFFLDYL&amp;ref=acr_search__popover&amp;contextId=search","position":"triggerBottom","closeButton":true,"popoverLabel":"4.6 out of 5 stars, rating details","closeButtonLabel":""}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="4mxncs-ntvb1j-bxx67b-k09nth" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="4.6 out of 5 stars, rating details" class="a-popover-trigger a-declarative mvt-review-star-mini-popover" href="javascript:void(0)" role="button"><i aria-hidden="true" class="a-icon a-icon-star-mini a-star-mini-4-5 mvt-review-star-mini mvt-review-star-with-margin" data-cy="reviews-ratings-slot"><span class="a-icon-alt">4.6 out of 5 stars</span></i><i class="a-icon a-icon-popover"></i></a></span> <span class="rush-component" data-component-id="18" data-component-type="s-client-side-analytics" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="s-csa-instrumentation-wrapper alf-search-csa-instrumentation-wrapper" data-csa-c-asin="B0HGFFLDYL" data-csa-c-content-id="alf-customer-ratings-count-component" data-csa-c-id="k93r2t-1y1kmo-vgw9z2-fb4mm4" data-csa-c-layout="LIST" data-csa-c-slot-id="alf-reviews" data-csa-c-type="alf-af-component" data-csa-op-log-render="" style="display: inline-block"><a aria-label="28 ratings" class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="/sspa/click?ie=UTF8&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6&amp;url=%2FCancelling-Headphones-Bluetooth-Transparency-Graduation%2Fdp%2FB0HGFFLDYL%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ%26dib_tag%3Dse%26keywords%3Dwireless%2Bheadphones%26qid%3D1788871867%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1#customerReviews"><span aria-hidden="true" class="a-size-mini puis-normal-weight-text s-underline-text">(28)</span> </a> </div></span></div><div class="a-row a-size-base"><span class="a-size-base a-color-secondary">New on Amazon in past month</span></div></div><div class="puisg-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24"><div class="puisg-col-inner"><div class="a-section a-spacing-none a-spacing-top-micro puis-price-instructions-style" data-cy="price-recipe"><div class="a-row a-size-base a-color-base"><div class="a-row"><span class="aok-offscreen" id="price-link">Price, product page</span><a aria-describedby="price-link" class="a-link-normal s-no-hover s-underline-text s-underline-link-text s-link-style a-text-normal" href="/sspa/click?ie=UTF8&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6&amp;url=%2FCancelling-Headphones-Bluetooth-Transparency-Graduation%2Fdp%2FB0HGFFLDYL%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ%26dib_tag%3Dse%26keywords%3Dwireless%2Bheadphones%26qid%3D1788871867%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1"><span class="a-price" data-a-color="base" data-a-size="xl"><span class="a-offscreen">EUR 85.15</span><span aria-hidden="true"><span class="a-price-symbol">EUR</span><span class="a-price-whole">85<span class="a-price-decimal">.</span></span><span class="a-price-fraction">15</span></span></span> <span class="a-offscreen">Typical: EUR 170.33</span><div aria-hidden="Typical: EUR 170.33" class="a-section aok-inline-block"><span class="a-size-base a-color-secondary">Typical: </span><span class="a-price a-text-price" data-a-color="secondary" data-a-size="b" data-a-strike="true"><span class="a-offscreen">EUR 170.33</span><span aria-hidden="true">EUR170.33</span></span></div></a></div><div class="a-row"></div></div><div class="a-row a-size-small a-color-secondary"><span class="rush-component" data-component-id="19" data-component-props='{"asin":"B0HGFFLDYL"}' data-component-type="s-coupon-component" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><span class="s-coupon-clipped aok-block aok-hidden"><span class="a-size-base s-highlighted-text-padding s-coupon-highlight-color aok-inline-block">Save EUR 42.15</span> <span class="a-color-base">EUR 42.15 off coupon applied</span></span><span class="s-coupon-unclipped aok-block"><span class="a-size-base s-highlighted-text-padding s-coupon-highlight-color aok-inline-block">Save EUR 42.15</span> <span class="a-color-base"> with coupon</span></span></span> </div></div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="delivery-recipe"><div class="a-row a-size-base a-color-secondary s-align-children-center"><div class="a-section a-spacing-none a-padding-none udm-delivery-block" data-cy="delivery-block"><div class="a-row a-color-base udm-badge-block"><div class="a-column a-span12"></div></div><div class="a-row a-color-base udm-primary-delivery-message"><div class="a-column a-span12">EUR 8.23 delivery <span class="a-text-bold" id="WVCRIAFWG">Sat, Sep 19</span></div></div></div></div><div class="a-row a-size-base a-color-secondary s-align-children-center"><span class="a-size-small a-color-base">Ships to Netherlands</span></div></div><div class="a-section a-spacing-none a-spacing-top-mini"><div class="a-row"><div class="a-section puis-cta-capsule puis-cta-capsule-container-displacement"><div class="puis-atcb-container puis-cta-row-displacement" data-atcb-props='{"cartType":"DEFAULT","locale":"en-US","sessionId":"000-0000000-0000000","csrfToken":"REDACTED-CSRF-TOKEN"}' data-atcb-uid="atcb-B0HGFFLDYL-2" data-cy="add-to-cart"><div class="addToCartShoppingPortalCSRFToken aok-hidden"><!-- sp:csrf --><meta content="REDACTED-CSRF-TOKEN" name="anti-csrftoken-a2z"/><!-- sp:end-csrf --></div><div class="a-section puis-atcb-add-container"><div class="a-section atc-faceout-container" data-asin="B0HGFFLDYL"><form action="/cart/add-to-cart?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788871867&amp;discoveredAsins.0=B0HGFFLDYL" class="a-spacing-none" method="post"><!-- sp:csrf --><input name="anti-csrftoken-a2z" type="hidden" value="REDACTED-CSRF-TOKEN"/><!-- sp:end-csrf --><input name="clientName" type="hidden" value="EUIC_AddToCart_Search"/><input name="items[0.base][asin]" type="hidden" value="B0HGFFLDYL"/><input name="items[0.base][offerListingId]" type="hidden" value="REDACTED-OFFER-LISTING-ID"/><input name="items[0.base][quantity]" type="hidden" value="1"/><input name="minOrderQuantity" type="hidden" value="1"/><input name="maxOrderQuantity" type="hidden" value="20"/><input name="merchantId" type="hidden" value="A2KTQNDHJRLOZK"/><div class="a-section ax-replace a-spacing-none"><div class="ax-atc celwidget atc-btn-container" data-cel-widget="" data-csa-c-content-id="ax-atc-EUIC_AddToCart_Search-content" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="mltu2i-vg6u6w-fcxy2z-orbct7" data-csa-c-item-id="B0HGFFLDYL" data-csa-c-item-type="asin" data-csa-c-merchant-id="A2KTQNDHJRLOZK" data-csa-c-pos="2" data-csa-c-price-to-pay="85.152494" data-csa-c-slot-id="ax-atc-EUIC_AddToCart_Search" data-csa-c-type="item"><span class="a-declarative" data-action="puis-atcb-add-action-retail" data-puis-atcb-add-action-retail='{"asin":"B0HGFFLDYL","url":"https://data.amazon.com/api/marketplaces/ATVPDKIKX0DER/cart/carts/retail/items?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788871867&amp;discoveredAsins.0=B0HGFFLDYL","neoAtcUrl":"/cart/add-to-cart?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788871867&amp;discoveredAsins.0=B0HGFFLDYL","offerListingId":"REDACTED-OFFER-LISTING-ID","additionalParameters":{},"sponsoredLoggingUrl":"https://www.amazon.com/sspa/click?ie=UTF8&amp;action=clickAddToCart&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6","spAttributionURL":"https://www.amazon.com/sspa/click?ie=UTF8&amp;action=clickAddToCart&amp;spc=MToyNTMxODMyNzA1MTE2MjAxOjE3ODg4NzE4Njc6c3BfYXRmOjMwMTQ5MTkzMzU0MTgwMjo6MDo6","spAttributionMethod":"POST","messageSuccess":"Item Added","messageError":"Failed to add item"}' data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div data-csa-c-action-name="addToCart" data-csa-c-button-type="button" data-csa-c-content-id="s-search-add-to-cart-action" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="c6go3m-sekyc3-tke6co-zfcjme" data-csa-c-item-id="B0HGFFLDYL" data-csa-c-item-type="asin" data-csa-c-merchant-id="A2KTQNDHJRLOZK" data-csa-c-price-to-pay="85.152494" data-csa-c-type="action"><div class="a-button-stack"><span class="a-button a-button-primary a-button-icon puis-atcb-button" id="a-autoid-2"><span class="a-button-inner"><i class="a-icon a-icon-cart"></i><input aria-label="Add to cart" class="a-button-input" name="submit.addToCart" type="submit"/><span aria-hidden="true" class="a-button-text" id="a-autoid-2-announce">Add to cart</span></span></span></div></div></span></div></div></form></div></div><div class="a-section puis-atcb-error-container aok-hidden"><div class="a-box a-alert-inline a-alert-inline-error" role="alert"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content"><span class="a-size-mini puis-atcb-error-message"></span></div></div></div></div><div class="a-section puis-atcb-extra-container"></div></div></div></div></div></div></div><div class="puisg-col puisg-col-0-of-4 puisg-col-0-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-8-of-20 puisg-col-8-of-24"><div class="puisg-col-inner"></div></div></div><div class="puisg-row"></div></div></div></div></div></div></div></span>
</div>
</div>
</div></div></div>"""

# An ORGANIC tile with an "Overall Pick" badge and NO PRICE — Amazon
# withholds the price for some offers, and this is the shape that proves a
# null price is the site's answer rather than a parser failure. Same page,
# same capture.
FIX_TILE_BADGE_COM = """<div class="sg-col-20-of-24 s-result-item s-asin sg-col-0-of-12 sg-col-16-of-20 sg-col s-widget-spacing-small gsx-ies-anchor sg-col-12-of-16" data-asin="B0C3HCD34R" data-cel-widget="search_result_3" data-component-id="20" data-component-type="s-search-result" data-index="4" data-uuid="c7121e91-049d-4bcd-8b07-0429a5a11f9c" id="c7121e91-049d-4bcd-8b07-0429a5a11f9c" role="listitem"><div class="sg-col-inner"><div cel_widget_id="MAIN-SEARCH_RESULTS-4" class="s-widget-container s-spacing-small s-widget-container-height-small celwidget slot=MAIN template=SEARCH_RESULTS widgetId=search-results_3" data-cel-widget="MAIN-SEARCH_RESULTS-4" data-csa-c-asin-instance-id="de36c699-9988-4d08-bd49-8b06c8e3750e" data-csa-c-content-id="search-results_3" data-csa-c-cs-type="Loom" data-csa-c-id="g35hdd-62mrcb-f60fmc-dr6epw" data-csa-c-item-id="amzn1.asin.1.B0C3HCD34R" data-csa-c-pos="3" data-csa-c-type="item" data-csa-op-log-render=""><span class="a-declarative" data-action="puis-card-container-declarative" data-csa-c-func-deps="aui-da-puis-card-container-declarative" data-csa-c-id="429t6z-l5e556-m72kk6-pbwl5c" data-csa-c-item-id="amzn1.asin.B0C3HCD34R" data-csa-c-owner="puis" data-csa-c-posx="3" data-csa-c-type="item" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="puis-card-container puis-overflow-hidden s-card-container aok-relative desktop-list-view puis-include-content-margin puis puis-v1v3dkg82cns7b2ucnfxri66dtm s-latency-cf-section puis-card-border" data-cy="asin-faceout-container"><div class="a-section"><div class="puisg-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24 puis-list-col-left"><div class="puisg-col-inner"><div class="a-section a-spacing-none aok-relative puis-status-badge-container s-list-status-badge-container"><div class="a-section a-spacing-none s-badge-spacing"><span aria-label="Amazon's Choice"><div class="a-section"><div class="a-section"><span class="a-declarative" data-a-popover="{&quot;dataStrategy&quot;:&quot;preload&quot;,&quot;name&quot;:&quot;B0C3HCD34R-ac-popover-div&quot;,&quot;popoverLabel&quot;:&quot;Amazon's Choice: Overall Pick&quot;}" data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="83e8y6-1esya-56u0f5-41dm17" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm" id="B0C3HCD34R-ac-desktop-declarative" style="display: inline-block;"><span class="rush-component mvt-badge-padding-3 mvt-badge-placement-3 mvt-badge-rectangle-shape mvt-badge-border-radius mvt-badge-font" data-component-id="21" data-component-props='{"asin":"B0C3HCD34R","badgeType":"amazons-choice"}' data-component-type="s-status-badge-component" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="a-section aok-nowrap aok-block" data-csa-c-badge-text="Overall Pick" data-csa-c-content-id="search-ac-badge-v2" data-csa-c-device-env="WEB" data-csa-c-device-type="DESKTOP" data-csa-c-id="5xctoy-smncsx-ot5399-tq66v9" data-csa-c-interaction-events="click" data-csa-c-locale="en_US" data-csa-c-slot-id="search-multi-facet-ac-badge" data-csa-c-type="widget"><span class="a-badge" data-a-badge-type="status" id="B0C3HCD34R-amazons-choice"><span class="a-badge-label" data-a-badge-color="mvt-badge-color-squid-ink" id="B0C3HCD34R-amazons-choice-label"><span class="a-badge-label-inner a-text-ellipsis"><div class="a-section"><span class="a-badge-text" data-a-badge-color="mvt-badge-text-color-white">Overall Pick</span><span class="aok-inline-block aok-align-center ac-badge-info-icon"></span></div></span></span></span></div></span></span><div class="a-popover-preload" id="a-popover-B0C3HCD34R-ac-popover-div"><div class="a-section a-section ac-badge-popover ac-popover-text" data-csa-c-content-id="search-ac-badge-v2-popover-info" data-csa-c-device-env="WEB" data-csa-c-device-type="DESKTOP" data-csa-c-facet-name="Overall Pick" data-csa-c-id="bf8f3c-rz20v5-i3ttba-qfub5i" data-csa-c-locale="en_US" data-csa-c-slot-id="search-ac-badge-multi-facet-info" data-csa-c-type="widget"><span class="a-size-base-plus a-color-base a-text-bold">Amazon's Choice: Overall Pick</span><br/><span class="a-size-base a-color-base">Products highlighted as 'Overall Pick' are:</span><br/><ul class="a-unordered-list a-vertical a-spacing-top-mini ac-badge-popover-bullets"><li><span class="a-list-item a-size-base a-color-base">Rated 4+ stars</span></li><li><span class="a-list-item a-size-base a-color-base">Purchased often</span></li><li><span class="a-list-item a-size-base a-color-base">Returned infrequently</span></li></ul></div></div></div></div></span></div></div><div class="s-product-image-container aok-relative s-text-center s-image-overlay-grey puis-image-overlay-grey s-padding-left-small s-padding-right-small puis-flex-expand-height puis puis-v1v3dkg82cns7b2ucnfxri66dtm" data-cy="image-container"><div class="aok-relative"><span class="rush-component" data-component-type="s-product-image" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-hidden="true" class="a-link-normal s-no-outline" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=sr_1_3?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" tabindex="-1"><div class="a-section aok-relative s-image-fixed-height"><img alt="Soundcore by Anker Q20i Hybrid Active Noise Cancelling Headphones, Black | Over-Ear, Bluetooth, 40H ANC Playtime, Hi-Res A..." aria-hidden="true" class="s-image" data-image-index="3" data-image-latency="s-product-image" data-image-load="" data-image-source-density="1" src="https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY218_.jpg" srcset="https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY218_.jpg 1x, https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY327_FMwebp_QL65_.jpg 1.5x, https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY436_FMwebp_QL65_.jpg 2x, https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY545_FMwebp_QL65_.jpg 2.5x, https://m.media-amazon.com/images/I/51CnDMbXZzL._AC_UY654_FMwebp_QL65_.jpg 3x"/></div></a></span></div></div></div></div><div class="puisg-col puisg-col-0-of-4 puisg-col-0-of-8 puisg-col-4-of-12 puisg-col-8-of-16 puisg-col-12-of-20 puisg-col-12-of-24 puis-list-col-right"><div class="puisg-col-inner"><div class="a-section a-spacing-small a-spacing-top-small"><div class="a-section a-spacing-none puis-padding-right-small s-title-instructions-style" data-cy="title-recipe"><a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=sr_1_3?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3"><h2 aria-label="Soundcore by Anker Q20i Hybrid Active Noise Cancelling Headphones, Black | Over-Ear, Bluetooth, 40H ANC Playtime, Hi-Res Audio, Big Bass, Transparency Mode, Customize via App, Travel, Home, Office" class="a-size-medium a-spacing-none a-color-base a-text-normal"><span>Soundcore by Anker Q20i Hybrid Active Noise Cancelling Headphones, Black | Over-Ear, Bluetooth, 40H ANC Playtime, Hi-Res Audio, Big Bass, Transparency Mode, Customize via App, Travel, Home, Office</span></h2></a> </div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="reviews-block"><div class="a-row a-size-base"><div class="a-row a-size-base a-color-base"><span class="s-line-clamp-2"><span class="a-text-bold">Top Reviewed for Battery life</span></span></div></div><div class="a-row a-size-small"><span aria-hidden="true" class="a-size-small a-color-base">4.5</span><span class="a-declarative" data-a-popover='{"url":"/review/widgets/average-customer-review/popover/ref=acr_search__popover?ie=UTF8&amp;asin=B0C3HCD34R&amp;ref=acr_search__popover&amp;contextId=search","position":"triggerBottom","closeButton":true,"popoverLabel":"4.5 out of 5 stars, rating details","closeButtonLabel":""}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="eiz0ly-72tb6p-pd95qy-gas3r0" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="4.5 out of 5 stars, rating details" class="a-popover-trigger a-declarative mvt-review-star-mini-popover" href="javascript:void(0)" role="button"><i aria-hidden="true" class="a-icon a-icon-star-mini a-star-mini-4-5 mvt-review-star-mini mvt-review-star-with-margin" data-cy="reviews-ratings-slot"><span class="a-icon-alt">4.5 out of 5 stars</span></i><i class="a-icon a-icon-popover"></i></a></span> <span class="rush-component" data-component-id="22" data-component-type="s-client-side-analytics" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="s-csa-instrumentation-wrapper alf-search-csa-instrumentation-wrapper" data-csa-c-asin="B0C3HCD34R" data-csa-c-content-id="alf-customer-ratings-count-component" data-csa-c-id="mhwv4y-xlwu0c-4jnmnk-16w5qj" data-csa-c-layout="LIST" data-csa-c-slot-id="alf-reviews" data-csa-c-type="alf-af-component" data-csa-op-log-render="" style="display: inline-block"><a aria-label="73,731 ratings" class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=sr_1_3?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3#customerReviews"><span aria-hidden="true" class="a-size-mini puis-normal-weight-text s-underline-text">(73.7K)</span> </a> </div></span></div><div class="a-row a-size-base"><span class="a-size-base a-color-secondary">10K+ bought in past month</span></div></div><div class="puisg-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24"><div class="puisg-col-inner"><div class="a-section a-spacing-none a-spacing-top-micro puis-price-instructions-style" data-cy="price-recipe"><div class="a-row a-size-base a-color-base"><div class="a-row"></div><div class="a-row"></div></div></div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="delivery-recipe"><div class="a-row a-size-base a-color-secondary s-align-children-center"></div></div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="certification-recipe"><div class="a-section a-spacing-none s-align-children-center"><div class="a-section a-spacing-none s-pc-faceout-container"><div> <div class="s-align-children-center"><span class="a-declarative" data-action="s-pc-sidesheet-open" data-csa-c-func-deps="aui-da-s-pc-sidesheet-open" data-csa-c-id="fpnz9k-kzojiq-ijqdtp-94hal0" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-sidesheet-open='{"renderId":"rhhm0yhk7rdrf2oznuskhilkpm","contentKeys":"","certificationHeadingId":"s-pc-certification-heading","closeButtonLabel":"Close","dwellMetric":"provenanceCertifications_desktop_cpf_badge_t","preloadDomId":"pc-side-sheet-B0C3HCD34R","popoverLabel":"Product certifications","interactLoggingMetricsList":["provenanceCertifications_desktop_cpf_badge"]}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="a-link-normal s-no-underline s-pc-badge s-align-children-center aok-block" data-cy="s-pc-faceout-badge" href="javascript:void(0)" role="button"><div class="a-section s-pc-attribute-pill-text s-margin-bottom-none s-margin-bottom-none aok-block s-pc-certification-faceout"><span class="faceout-image-view"></span><img alt="" class="s-image" height="18px" src="https://m.media-amazon.com/images/I/11++B3A2NEL._SS200_.png" width="18px"/> <span class="a-size-base a-color-base">Energy efficiency +3 more</span><div class="s-margin-bottom-none s-pc-sidesheet-chevron aok-nowrap"><i class="a-icon a-icon-popover aok-align-center" role="presentation"></i></div></div></a></span></div></div></div></div><div class="a-section puis puis-v1v3dkg82cns7b2ucnfxri66dtm aok-hidden" id="pc-side-sheet-B0C3HCD34R"><div class="a-section s-pc-container-side-sheet"><div class="s-align-children-center a-spacing-small" id="s-pc-certification-heading"><div aria-level="2" class="s-align-children-center s-pc-certification" role="heading"><span class="faceout-image-view"></span><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/11++B3A2NEL._SS200_.png" style="height:24px;width:24px;"></div> <span class="a-size-medium-plus a-color-base a-text-bold">Sustainability features</span></div></div><div class="a-spacing-medium s-pc-link-container"><span class="a-size-base a-color-base">This product has sustainability features recognized by trusted certifications.</span> </div><div class="a-section a-spacing-base"><div class="a-row a-expander-container a-spacing-none a-expander-section-container a-section-expander-container" data-a-expander-name="pc_sidesheet_expander" data-eometric="provenanceCertifications_desktop_cpf_badge_eo"><a aria-controls="pc-side-sheet-B0C3HCD34R-ec-0" aria-expanded="true" class="a-expander-header a-declarative a-expander-section-header a-link-section-expander a-size-medium" data-a-expander-toggle='{"allowLinkDefault":true, "expand_prompt":"", "collapse_prompt":""}' data-action="a-expander-toggle" href="javascript:void(0)" id="pc-side-sheet-B0C3HCD34R-eh-0" role="button"><i class="a-icon a-icon-section-collapse"></i><span aria-level="3" class="a-expander-prompt" role="heading"><span class="s-pc-expander-title a-text-bold">Energy efficiency</span></span></a><div aria-labelledby="pc-side-sheet-B0C3HCD34R-eh-0" aria-role="region" class="a-expander-content a-spacing-none a-expander-section-content a-section-expander-inner" data-expanded="true" id="pc-side-sheet-B0C3HCD34R-ec-0"><div class="a-section a-spacing-none"><div class="a-section a-spacing-small"><span class="a-size-base a-color-base">Conserves energy compared to similar products.</span></div><div class="a-section a-spacing-mini"><span class="a-size-small a-color-secondary">As certified by</span></div><div class="a-section s-pc-expander-certs s-pc-attribute s-pc-link-container"><span class="a-declarative" data-action="s-pc-sidesheet-secondary" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary" data-csa-c-id="ci2wdv-9t2skn-laxblh-m24tds" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-sidesheet-secondary='{"ctMetric":"provenanceCertifications_desktop_cpf_badge_cf","ctDwellMetric":"provenanceCertifications_desktop_cpf_badge_st","preloadDomId":"pc-side-sheet-B0C3HCD34R-0-0","previousPreloadDomId":"pc-side-sheet-B0C3HCD34R"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="Learn more about TCO Certified" class="a-link-normal aok-inline-block" href="#" id="pc-side-sheet-B0C3HCD34R-trigger-0-0" rel="noopener" role="button" target="javascript:void(0)"><div class="a-section s-margin-bottom-none aok-inline-block s-pc-attribute-pill s-pc-certification"><span class="faceout-image-view"></span><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:24px;width:24px;"></div> <div class="a-section aok-inline-block aok-align-center s-pc-attribute-pill-text s-margin-bottom-none"><span class="a-size-base">TCO Certified</span></div></div><span class="aok-offscreen">Learn more about TCO Certified</span></a></span><div aria-hidden="true" class="a-section aok-hidden" id="pc-side-sheet-B0C3HCD34R-0-0"><div class="s-pc-container-side-sheet s-margin-bottom-none"><div class="a-section a-spacing-base"><div class="a-section a-spacing-none s-pc-certification"><span class="a-declarative" data-action="s-pc-sidesheet-secondary-back" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary-back" data-csa-c-id="alh16i-9vrcly-vzv128-2p1fgd" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><button aria-label="Back to certifications for this product" class="s-pc-detail-view-secondary-back" type="button"><span class="s-pc-detail-view-back-icon aok-block"></span></button></span><div class="a-section a-spacing-none s-pc-certification"><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:36px;width:36px;"></div> <h2 class="a-spacing-none a-size-medium a-text-bold">TCO Certified</h2></div></div></div><hr aria-hidden="true" class="a-divider-normal"/><div class="a-section"><span class="a-size-base a-color-base">TCO Certified IT and technology products are certified based on lower environmental and climate impact, safer chemicals and circular design which reduces e-waste. Criteria for social responsibility are also included to reduce the risk of human rights violations, child labour, and health and safety issues for workers in the supply chain. To ensure that all criteria are being met, independent verification organisations conduct the product testing and supply chain assessments against these criteria.</span></div></div></div></div></div></div></div><div class="a-row a-expander-container a-spacing-none a-expander-section-container a-section-expander-container" data-a-expander-name="pc_sidesheet_expander" data-eometric="provenanceCertifications_desktop_cpf_badge_eo"><a aria-controls="pc-side-sheet-B0C3HCD34R-ec-1" aria-expanded="false" class="a-expander-header a-declarative a-expander-section-header a-link-section-expander a-size-medium" data-a-expander-toggle='{"allowLinkDefault":true, "expand_prompt":"", "collapse_prompt":""}' data-action="a-expander-toggle" href="javascript:void(0)" id="pc-side-sheet-B0C3HCD34R-eh-1" role="button"><i class="a-icon a-icon-section-expand"></i><span aria-level="3" class="a-expander-prompt" role="heading"><span class="s-pc-expander-title a-text-bold">Safer chemicals</span></span></a><div aria-labelledby="pc-side-sheet-B0C3HCD34R-eh-1" aria-role="region" class="a-expander-content a-spacing-none a-expander-section-content a-section-expander-inner" data-expanded="false" id="pc-side-sheet-B0C3HCD34R-ec-1" style="display:none"><div class="a-section a-spacing-none"><div class="a-section a-spacing-small"><span class="a-size-base a-color-base">Made with chemicals safer for human health and the environment.</span></div><div class="a-section a-spacing-mini"><span class="a-size-small a-color-secondary">As certified by</span></div><div class="a-section s-pc-expander-certs s-pc-attribute s-pc-link-container"><span class="a-declarative" data-action="s-pc-sidesheet-secondary" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary" data-csa-c-id="pbr1bf-hi2a8x-ve1qso-bychxh" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-sidesheet-secondary='{"ctMetric":"provenanceCertifications_desktop_cpf_badge_cf","ctDwellMetric":"provenanceCertifications_desktop_cpf_badge_st","preloadDomId":"pc-side-sheet-B0C3HCD34R-1-0","previousPreloadDomId":"pc-side-sheet-B0C3HCD34R"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="Learn more about TCO Certified" class="a-link-normal aok-inline-block" href="#" id="pc-side-sheet-B0C3HCD34R-trigger-1-0" rel="noopener" role="button" target="javascript:void(0)"><div class="a-section s-margin-bottom-none aok-inline-block s-pc-attribute-pill s-pc-certification"><span class="faceout-image-view"></span><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:24px;width:24px;"></div> <div class="a-section aok-inline-block aok-align-center s-pc-attribute-pill-text s-margin-bottom-none"><span class="a-size-base">TCO Certified</span></div></div><span class="aok-offscreen">Learn more about TCO Certified</span></a></span><div aria-hidden="true" class="a-section aok-hidden" id="pc-side-sheet-B0C3HCD34R-1-0"><div class="s-pc-container-side-sheet s-margin-bottom-none"><div class="a-section a-spacing-base"><div class="a-section a-spacing-none s-pc-certification"><span class="a-declarative" data-action="s-pc-sidesheet-secondary-back" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary-back" data-csa-c-id="5ffqi5-7cez8n-i0m4sn-94yubc" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><button aria-label="Back to certifications for this product" class="s-pc-detail-view-secondary-back" type="button"><span class="s-pc-detail-view-back-icon aok-block"></span></button></span><div class="a-section a-spacing-none s-pc-certification"><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:36px;width:36px;"></div> <h2 class="a-spacing-none a-size-medium a-text-bold">TCO Certified</h2></div></div></div><hr aria-hidden="true" class="a-divider-normal"/><div class="a-section"><span class="a-size-base a-color-base">TCO Certified IT and technology products are certified based on lower environmental and climate impact, safer chemicals and circular design which reduces e-waste. Criteria for social responsibility are also included to reduce the risk of human rights violations, child labour, and health and safety issues for workers in the supply chain. To ensure that all criteria are being met, independent verification organisations conduct the product testing and supply chain assessments against these criteria.</span></div></div></div></div></div></div></div><div class="a-row a-expander-container a-spacing-none a-expander-section-container a-section-expander-container" data-a-expander-name="pc_sidesheet_expander" data-eometric="provenanceCertifications_desktop_cpf_badge_eo"><a aria-controls="pc-side-sheet-B0C3HCD34R-ec-2" aria-expanded="false" class="a-expander-header a-declarative a-expander-section-header a-link-section-expander a-size-medium" data-a-expander-toggle='{"allowLinkDefault":true, "expand_prompt":"", "collapse_prompt":""}' data-action="a-expander-toggle" href="javascript:void(0)" id="pc-side-sheet-B0C3HCD34R-eh-2" role="button"><i class="a-icon a-icon-section-expand"></i><span aria-level="3" class="a-expander-prompt" role="heading"><span class="s-pc-expander-title a-text-bold">Manufacturing practices</span></span></a><div aria-labelledby="pc-side-sheet-B0C3HCD34R-eh-2" aria-role="region" class="a-expander-content a-spacing-none a-expander-section-content a-section-expander-inner" data-expanded="false" id="pc-side-sheet-B0C3HCD34R-ec-2" style="display:none"><div class="a-section a-spacing-none"><div class="a-section a-spacing-small"><span class="a-size-base a-color-base">Manufactured using processes that reduce the risk of negative environmental impact.</span></div><div class="a-section a-spacing-mini"><span class="a-size-small a-color-secondary">As certified by</span></div><div class="a-section s-pc-expander-certs s-pc-attribute s-pc-link-container"><span class="a-declarative" data-action="s-pc-sidesheet-secondary" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary" data-csa-c-id="evuh2m-g8st8q-2bia6e-8qic04" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-sidesheet-secondary='{"ctMetric":"provenanceCertifications_desktop_cpf_badge_cf","ctDwellMetric":"provenanceCertifications_desktop_cpf_badge_st","preloadDomId":"pc-side-sheet-B0C3HCD34R-2-0","previousPreloadDomId":"pc-side-sheet-B0C3HCD34R"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="Learn more about TCO Certified" class="a-link-normal aok-inline-block" href="#" id="pc-side-sheet-B0C3HCD34R-trigger-2-0" rel="noopener" role="button" target="javascript:void(0)"><div class="a-section s-margin-bottom-none aok-inline-block s-pc-attribute-pill s-pc-certification"><span class="faceout-image-view"></span><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:24px;width:24px;"></div> <div class="a-section aok-inline-block aok-align-center s-pc-attribute-pill-text s-margin-bottom-none"><span class="a-size-base">TCO Certified</span></div></div><span class="aok-offscreen">Learn more about TCO Certified</span></a></span><div aria-hidden="true" class="a-section aok-hidden" id="pc-side-sheet-B0C3HCD34R-2-0"><div class="s-pc-container-side-sheet s-margin-bottom-none"><div class="a-section a-spacing-base"><div class="a-section a-spacing-none s-pc-certification"><span class="a-declarative" data-action="s-pc-sidesheet-secondary-back" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary-back" data-csa-c-id="9z5kvr-gws1i-a8jas-sfkdo8" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><button aria-label="Back to certifications for this product" class="s-pc-detail-view-secondary-back" type="button"><span class="s-pc-detail-view-back-icon aok-block"></span></button></span><div class="a-section a-spacing-none s-pc-certification"><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:36px;width:36px;"></div> <h2 class="a-spacing-none a-size-medium a-text-bold">TCO Certified</h2></div></div></div><hr aria-hidden="true" class="a-divider-normal"/><div class="a-section"><span class="a-size-base a-color-base">TCO Certified IT and technology products are certified based on lower environmental and climate impact, safer chemicals and circular design which reduces e-waste. Criteria for social responsibility are also included to reduce the risk of human rights violations, child labour, and health and safety issues for workers in the supply chain. To ensure that all criteria are being met, independent verification organisations conduct the product testing and supply chain assessments against these criteria.</span></div></div></div></div></div></div></div><div class="a-row a-expander-container a-spacing-none a-expander-section-container a-section-expander-container" data-a-expander-name="pc_sidesheet_expander" data-eometric="provenanceCertifications_desktop_cpf_badge_eo"><a aria-controls="pc-side-sheet-B0C3HCD34R-ec-3" aria-expanded="false" class="a-expander-header a-declarative a-expander-section-header a-link-section-expander a-size-medium" data-a-expander-toggle='{"allowLinkDefault":true, "expand_prompt":"", "collapse_prompt":""}' data-action="a-expander-toggle" href="javascript:void(0)" id="pc-side-sheet-B0C3HCD34R-eh-3" role="button"><i class="a-icon a-icon-section-expand"></i><span aria-level="3" class="a-expander-prompt" role="heading"><span class="s-pc-expander-title a-text-bold">Worker well-being</span></span></a><div aria-labelledby="pc-side-sheet-B0C3HCD34R-eh-3" aria-role="region" class="a-expander-content a-spacing-none a-expander-section-content a-section-expander-inner" data-expanded="false" id="pc-side-sheet-B0C3HCD34R-ec-3" style="display:none"><div class="a-section a-spacing-none"><div class="a-section a-spacing-small"><span class="a-size-base a-color-base">Manufactured on farms or in facilities that protect the rights and/or health of workers.</span></div><div class="a-section a-spacing-mini"><span class="a-size-small a-color-secondary">As certified by</span></div><div class="a-section s-pc-expander-certs s-pc-attribute s-pc-link-container"><span class="a-declarative" data-action="s-pc-sidesheet-secondary" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary" data-csa-c-id="zetfwn-5wwvm1-8bkp6h-bpdxkc" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-sidesheet-secondary='{"ctMetric":"provenanceCertifications_desktop_cpf_badge_cf","ctDwellMetric":"provenanceCertifications_desktop_cpf_badge_st","preloadDomId":"pc-side-sheet-B0C3HCD34R-3-0","previousPreloadDomId":"pc-side-sheet-B0C3HCD34R"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="Learn more about TCO Certified" class="a-link-normal aok-inline-block" href="#" id="pc-side-sheet-B0C3HCD34R-trigger-3-0" rel="noopener" role="button" target="javascript:void(0)"><div class="a-section s-margin-bottom-none aok-inline-block s-pc-attribute-pill s-pc-certification"><span class="faceout-image-view"></span><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:24px;width:24px;"></div> <div class="a-section aok-inline-block aok-align-center s-pc-attribute-pill-text s-margin-bottom-none"><span class="a-size-base">TCO Certified</span></div></div><span class="aok-offscreen">Learn more about TCO Certified</span></a></span><div aria-hidden="true" class="a-section aok-hidden" id="pc-side-sheet-B0C3HCD34R-3-0"><div class="s-pc-container-side-sheet s-margin-bottom-none"><div class="a-section a-spacing-base"><div class="a-section a-spacing-none s-pc-certification"><span class="a-declarative" data-action="s-pc-sidesheet-secondary-back" data-csa-c-func-deps="aui-da-s-pc-sidesheet-secondary-back" data-csa-c-id="y46npt-rovt9n-lxyl2c-ltyo5j" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><button aria-label="Back to certifications for this product" class="s-pc-detail-view-secondary-back" type="button"><span class="s-pc-detail-view-back-icon aok-block"></span></button></span><div class="a-section a-spacing-none s-pc-certification"><div alt="" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/31LhUjMqiEL._SS200_.jpg" style="height:36px;width:36px;"></div> <h2 class="a-spacing-none a-size-medium a-text-bold">TCO Certified</h2></div></div></div><hr aria-hidden="true" class="a-divider-normal"/><div class="a-section"><span class="a-size-base a-color-base">TCO Certified IT and technology products are certified based on lower environmental and climate impact, safer chemicals and circular design which reduces e-waste. Criteria for social responsibility are also included to reduce the risk of human rights violations, child labour, and health and safety issues for workers in the supply chain. To ensure that all criteria are being met, independent verification organisations conduct the product testing and supply chain assessments against these criteria.</span></div></div></div></div></div></div></div></div><div class="a-section s-pc-sticky-footer"><a class="a-link-normal" href="/climatepledgefriendly"><span class="a-declarative" data-action="s-pc-program-footer" data-csa-c-func-deps="aui-da-s-pc-program-footer" data-csa-c-id="r9nol0-5yoxg3-xkk9cy-wka2qd" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-pc-program-footer='{"allowLinkDefault":true,"clickMetric":"provenanceCertifications_desktop_cpf_badge_lmlk"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="a-section s-pc-program-name"><div aria-level="2" class="a-section a-spacing-none" role="heading"><span class="faceout-image-view"></span><div alt="Climate Pledge Friendly" class="a-image-wrapper a-manually-loaded s-image" data-a-image-source="https://m.media-amazon.com/images/I/21AGu0JFvKL.svg" style="height:24px;width:186px;"></div> </div><div class="a-section aok-inline-block aok-align-center s-margin-bottom-none"><span class="a-size-base a-color-base">Discover more products with sustainability features.</span> <span class="a-size-base a-color-link">Learn more</span></div></div></span></a></div></div></div></div><div class="a-section a-spacing-none a-spacing-top-mini"><div class="puis-see-details-content"><div data-csa-c-content-id="s-search-see-details-button" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="2tnxgj-61vqwt-564cg9-v4t0pn" data-csa-c-item-id="B0C3HCD34R" data-csa-c-item-type="asin" data-csa-c-type="action"><div class="a-button-stack"><span class="a-button a-button-base" id="a-autoid-3"><span class="a-button-inner"><a class="a-button-text" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=sr_1_3_so_HEADPHONES?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" id="a-autoid-3-announce">See options</a></span></span></div></div></div></div><div class="a-section a-spacing-none a-spacing-top-mini" data-cy="secondary-offer-recipe"><div class="a-row a-size-base a-color-secondary"><span class="a-size-base a-color-secondary">No featured offers available</span><br/><span class="a-color-base">EUR 28.15</span><span class="a-letter-space"></span><span class="a-declarative" data-action="s-show-all-offers-display" data-csa-c-func-deps="aui-da-s-show-all-offers-display" data-csa-c-id="ipd6cz-5jp3cx-eembxk-mejukw" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-s-show-all-offers-display='{"assetMismatch":"Abandon","fallbackUrl":"/gp/offer-listing/B0C3HCD34R/ref=sr_1_3_olp?keywords=wireless+headphones&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;qid=1788871867&amp;sr=8-3","url":"/gp/aod/ajax/ref=sr_1_3_aod?asin=B0C3HCD34R&amp;pc=sp&amp;keywords=wireless+headphones&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;qid=1788871867&amp;sr=8-3"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="a-link-normal s-link-style s-underline-text s-underline-link-text" href="/gp/offer-listing/B0C3HCD34R/ref=sr_1_3_olp?keywords=wireless+headphones&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;qid=1788871867&amp;sr=8-3">(2 used &amp; new offers)</a></span><div class="a-section aok-hidden" id="all-offers-display"><div class="a-spinner-wrapper aok-hidden" id="all-offers-display-spinner"><span class="a-spinner a-spinner-medium"></span></div></div><span class="a-declarative" data-action="close-all-offers-display" data-csa-c-func-deps="aui-da-close-all-offers-display" data-csa-c-id="f1ur75-b7xzox-4zlj8-568wp2" data-csa-c-type="widget" data-render-id="rhhm0yhk7rdrf2oznuskhilkpm" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="a-section aok-hidden aod-darken-background" id="aod-background"></div></span></div></div><div class="a-section a-spacing-none a-spacing-top-mini s-color-swatch-container-list-view"><div aria-label="colors available" class="a-section s-color-swatch-container s-color-swatch-container-left-aligned" role="group"><div class="s-color-swatch-internal-container" role="list"><div class="a-section s-color-swatch-outer-circle s-color-swatch-pad s-color-swatch-outer-circle-selected"><div data-csa-c-content-id="color-swatch-link" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="jjxt46-hpryg9-9ak05n-nm087u" data-csa-c-interaction-events="click" data-csa-c-product-type="HEADPHONES" data-csa-c-swatch-is-selected="true" data-csa-c-swatch-position="1" data-csa-c-swatch-url="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=cs_sr_dp_loc_1?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" data-csa-c-type="link" role="listitem"><a aria-current="true" aria-label="Black" class="a-link-normal" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3HCD34R/ref=cs_sr_dp_loc_1?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" role="link"><span class="s-color-swatch-inner-circle-fill" style="background-color: #49494A"><span class="s-color-swatch-inner-circle-border"></span></span></a></div></div><div class="a-section s-color-swatch-outer-circle s-color-swatch-pad"><div data-csa-c-content-id="color-swatch-link" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="yut719-5f4zuo-mf10n8-6jjwnr" data-csa-c-interaction-events="click" data-csa-c-product-type="HEADPHONES" data-csa-c-swatch-is-selected="false" data-csa-c-swatch-position="2" data-csa-c-swatch-url="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0CQXMXJC5/ref=cs_sr_dp_loc_2?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" data-csa-c-type="link" role="listitem"><a aria-current="false" aria-label="Almond White" class="a-link-normal" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0CQXMXJC5/ref=cs_sr_dp_loc_2?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" role="link"><span class="s-color-swatch-inner-circle-fill" style="background-color: #EAE1D4"><span class="s-color-swatch-inner-circle-border"></span></span></a></div></div><div class="a-section s-color-swatch-outer-circle s-color-swatch-pad"><div data-csa-c-content-id="color-swatch-link" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="un5d05-tnt8qe-373lek-9mofln" data-csa-c-interaction-events="click" data-csa-c-product-type="HEADPHONES" data-csa-c-swatch-is-selected="false" data-csa-c-swatch-position="3" data-csa-c-swatch-url="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3KWT5V6/ref=cs_sr_dp_loc_3?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" data-csa-c-type="link" role="listitem"><a aria-current="false" aria-label="Blue" class="a-link-normal" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0C3KWT5V6/ref=cs_sr_dp_loc_3?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" role="link"><span class="s-color-swatch-inner-circle-fill" style="background-color: #4B4D60"><span class="s-color-swatch-inner-circle-border"></span></span></a></div></div><div class="a-section s-color-swatch-outer-circle s-color-swatch-pad"><div data-csa-c-content-id="color-swatch-link" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="r9ctig-4hnxpa-kxrjgu-vnyt2u" data-csa-c-interaction-events="click" data-csa-c-product-type="HEADPHONES" data-csa-c-swatch-is-selected="false" data-csa-c-swatch-position="4" data-csa-c-swatch-url="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0F4884LN3/ref=cs_sr_dp_loc_4?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" data-csa-c-type="link" role="listitem"><a aria-current="false" aria-label="Pink" class="a-link-normal" href="/soundcore-Cancelling-Headphones-Bluetooth-Transparency/dp/B0F4884LN3/ref=cs_sr_dp_loc_4?dib=eyJ2IjoiMSJ9.G-geUtgtY0Em3zqcFXosR_es5oPQtjNjIK5JOJFxaDPaDYbC95e_Qvi62l6GW2ZRTSG_IoMRhbRtZtTVNjmac-KxaSWqBHeCXyawRm86ff8x3JrlcFc1C3DiDEknLG5DxqT41eL9UmLWmV5l6dzZCHNaZBV_FzP3VQK8LNDWJbxUNXkG4jIiIeXpTQlsKNon8YTgvl8-ZaWqMMcqphh2ar8Hcs0H12SDwXe-TkBbtHg.qWrR5567F48HBZF4vCemeboLyftXZjHuGQL9m4Me3DQ&amp;dib_tag=se&amp;keywords=wireless+headphones&amp;qid=1788871867&amp;sr=8-3" role="link"><span class="s-color-swatch-inner-circle-fill" style="background-color: #E3C8C7"><span class="s-color-swatch-inner-circle-border"></span></span></a></div></div></div></div></div></div></div><div class="puisg-col puisg-col-0-of-4 puisg-col-0-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-8-of-20 puisg-col-8-of-24"><div class="puisg-col-inner"></div></div></div><div class="puisg-row"></div></div></div></div></div></div></div></span></div></div></div>"""

# An amazon.de tile: comma decimals with a trailing NBSP-separated euro
# sign ("15,10 \u00a0\u20ac"), and a badge label SPLIT across two spans
# ("Amazons " + "Tipp"), which is what reading one span gets wrong.
FIX_TILE_DE_SPLIT_BADGE = """<div class="sg-col-4-of-4 sg-col-20-of-24 s-result-item s-asin sg-col-16-of-20 sg-col sg-col-12-of-12 s-widget-spacing-small sg-col-8-of-8 sg-col-12-of-16" data-asin="B0DCNWN8NZ" data-cel-widget="search_result_2" data-component-id="6" data-component-type="s-search-result" data-index="3" data-uuid="dee89b02-e6ff-409d-85da-cd582e09a579" id="dee89b02-e6ff-409d-85da-cd582e09a579" role="listitem"><div class="sg-col-inner"><div cel_widget_id="MAIN-SEARCH_RESULTS-3" class="s-widget-container s-spacing-small s-widget-container-height-small celwidget slot=MAIN template=SEARCH_RESULTS widgetId=search-results_1" data-cel-widget="MAIN-SEARCH_RESULTS-3" data-csa-c-asin-instance-id="10d4f2df-cc28-4ae2-9f61-40ea4762ed0c" data-csa-c-content-id="search-results_1" data-csa-c-cs-type="Loom" data-csa-c-id="xpqhft-syba60-6hmid4-vu8rk8" data-csa-c-item-id="amzn1.asin.1.B0DCNWN8NZ" data-csa-c-pos="1" data-csa-c-type="item" data-csa-op-log-render=""><span class="a-declarative" data-action="puis-card-container-declarative" data-csa-c-func-deps="aui-da-puis-card-container-declarative" data-csa-c-id="hud99h-ycy8vl-5g79lt-wpw7p0" data-csa-c-item-id="amzn1.asin.B0DCNWN8NZ:amzn1.promotion.A2Q5LB7MPBHODF" data-csa-c-owner="puis" data-csa-c-posx="1" data-csa-c-type="item" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="puis-card-container puis-overflow-hidden s-card-container aok-relative desktop-list-view puis-include-content-margin puis puis-v1v3dkg82cns7b2ucnfxri66dtm s-latency-cf-section puis-card-border" data-cy="asin-faceout-container"><div class="a-section"><div class="puisg-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24 puis-list-col-left"><div class="puisg-col-inner"><div class="a-section a-spacing-none aok-relative puis-status-badge-container s-list-status-badge-container"><div class="a-section a-spacing-none s-badge-spacing"><span aria-label="Amazons Tipp"><span class="rush-component mvt-badge-padding-3 mvt-badge-placement-3 mvt-badge-rectangle-shape mvt-badge-border-radius mvt-badge-font" data-component-id="7" data-component-props='{"asin":"B0DCNWN8NZ","badgeType":"amazons-choice"}' data-component-type="s-status-badge-component" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="a-row a-badge-region"><span aria-labelledby="B0DCNWN8NZ-amazons-choice-label B0DCNWN8NZ-amazons-choice-supplementary" class="a-badge" data-a-badge-supplementary-position="right" data-a-badge-type="status" id="B0DCNWN8NZ-amazons-choice" role="group" tabindex="0"><span aria-hidden="true" class="a-badge-label" data-a-badge-color="mvt-badge-color-squid-ink" id="B0DCNWN8NZ-amazons-choice-label"><span class="a-badge-label-inner a-text-ellipsis"><span class="a-badge-text" data-a-badge-color="mvt-badge-text-color-white">Amazons </span><span class="a-badge-text" data-a-badge-color="mvt-badge-text-color-white">Tipp</span></span></span><span aria-hidden="true" class="a-badge-supplementary-text a-text-ellipsis" id="B0DCNWN8NZ-amazons-choice-supplementary">für "kopfhoerer"</span></span></div></span></span></div></div><div class="s-product-image-container aok-relative s-text-center s-image-overlay-grey puis-image-overlay-grey s-padding-left-small s-padding-right-small puis-flex-expand-height puis puis-v1v3dkg82cns7b2ucnfxri66dtm" data-cy="image-container"><div class="aok-relative"><span class="rush-component" data-component-type="s-product-image" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-hidden="true" class="a-link-normal s-no-outline" href="/Apple-MYQY3ZM-A-EarPods-USB-C/dp/B0DCNWN8NZ/ref=sr_1_1?dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;dib_tag=se&amp;keywords=kopfhoerer&amp;qid=1788872080&amp;sr=8-1" tabindex="-1"><div class="a-section aok-relative s-image-fixed-height"><img alt="Apple EarPods (USB-C) ​​​​​​​" aria-hidden="true" class="s-image" data-image-index="1" data-image-latency="s-product-image" data-image-load="" data-image-source-density="1" src="https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY218_.jpg" srcset="https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY218_.jpg 1x, https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY327_FMwebp_QL65_.jpg 1.5x, https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY436_FMwebp_QL65_.jpg 2x, https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY545_FMwebp_QL65_.jpg 2.5x, https://m.media-amazon.com/images/I/51oMc4XRaaL._AC_UY654_FMwebp_QL65_.jpg 3x"/></div></a></span></div></div></div></div><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-8-of-12 puisg-col-8-of-16 puisg-col-12-of-20 puisg-col-12-of-24 puis-list-col-right"><div class="puisg-col-inner"><div class="a-section a-spacing-small a-spacing-top-small"><div class="a-section a-spacing-none puis-padding-right-small s-title-instructions-style puis-desktop-list-title-instructions-style" data-cy="title-recipe"><a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" href="/Apple-MYQY3ZM-A-EarPods-USB-C/dp/B0DCNWN8NZ/ref=sr_1_1?dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;dib_tag=se&amp;keywords=kopfhoerer&amp;qid=1788872080&amp;sr=8-1"><h2 aria-label="Apple EarPods (USB-C) ​​​​​​​" class="a-size-medium a-spacing-none a-color-base a-text-normal"><span>Apple EarPods (USB-C) ​​​​​​​</span></h2></a> </div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="reviews-block"><div class="a-row a-size-base"><span class="a-color-base puis-bold-weight-text">Am besten bewertet</span></div><div class="a-row a-size-small"><span aria-hidden="true" class="a-size-small a-color-base">4,6</span><span class="a-declarative" data-a-popover='{"url":"/review/widgets/average-customer-review/popover/ref=acr_search__popover?ie=UTF8&amp;asin=B0DCNWN8NZ&amp;ref=acr_search__popover&amp;contextId=search","position":"triggerBottom","closeButton":true,"popoverLabel":"4,6 von 5 Sternen, Details zur Bewertung","closeButtonLabel":""}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="afb26o-h7xhhw-5ukm8n-8ksfx7" data-csa-c-type="widget" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="4,6 von 5 Sternen, Details zur Bewertung" class="a-popover-trigger a-declarative mvt-review-star-mini-popover" href="javascript:void(0)" role="button"><i aria-hidden="true" class="a-icon a-icon-star-mini a-star-mini-4-5 mvt-review-star-mini mvt-review-star-with-margin" data-cy="reviews-ratings-slot"><span class="a-icon-alt">4,6 von 5 Sternen</span></i><i class="a-icon a-icon-popover"></i></a></span> <span class="rush-component" data-component-id="8" data-component-type="s-client-side-analytics" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="s-csa-instrumentation-wrapper alf-search-csa-instrumentation-wrapper" data-csa-c-asin="B0DCNWN8NZ" data-csa-c-content-id="alf-customer-ratings-count-component" data-csa-c-id="7hcdk-7dp18s-7kg49j-sqqqed" data-csa-c-layout="LIST" data-csa-c-slot-id="alf-reviews" data-csa-c-type="alf-af-component" data-csa-op-log-render="" style="display: inline-block"><a aria-label="16.516 Bewertungen" class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="/Apple-MYQY3ZM-A-EarPods-USB-C/dp/B0DCNWN8NZ/ref=sr_1_1?dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;dib_tag=se&amp;keywords=kopfhoerer&amp;qid=1788872080&amp;sr=8-1#customerReviews"><span aria-hidden="true" class="a-size-mini puis-normal-weight-text s-underline-text">(16.516)</span> </a> </div></span></div><div class="a-row a-size-base"><span class="a-size-base a-color-secondary">5000+ Mal im letzten Monat gekauft</span></div></div><div class="puisg-row puis-desktop-list-row"><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-4-of-20 puisg-col-4-of-24"><div class="puisg-col-inner"><div class="a-section a-spacing-none a-spacing-top-micro puis-price-instructions-style" data-cy="price-recipe"><div class="a-row a-size-base a-color-base"><div class="a-row"><span class="aok-offscreen" id="price-link">Preis, Produktseite</span><a aria-describedby="price-link" class="a-link-normal s-no-hover s-underline-text s-underline-link-text s-link-style a-text-normal" href="/Apple-MYQY3ZM-A-EarPods-USB-C/dp/B0DCNWN8NZ/ref=sr_1_1?dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;dib_tag=se&amp;keywords=kopfhoerer&amp;qid=1788872080&amp;sr=8-1"><span class="a-price" data-a-color="base" data-a-size="xl"><span class="a-offscreen">15,10 €</span><span aria-hidden="true"><span class="a-price-whole">15<span class="a-price-decimal">,</span></span><span class="a-price-fraction">10</span><span class="a-price-symbol">€</span></span></span> <span class="a-offscreen">UVP: 19,00 €</span><div aria-hidden="UVP: 19,00 €" class="a-section aok-inline-block"><span class="a-size-base a-color-secondary">UVP: </span><span class="a-price a-text-price" data-a-color="secondary" data-a-size="b" data-a-strike="true"><span class="a-offscreen">19,00 €</span><span aria-hidden="true">19,00€</span></span></div></a></div><div class="a-row"></div></div><div class="a-row a-size-base a-color-secondary"><span class="a-size-base s-highlighted-text-padding s-promotion-highlight-color aok-inline-block">Spare 5 %</span><span class="a-color-base">  bei 4 ausgewählten Artikeln</span></div></div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="delivery-recipe"><div class="a-row a-size-base a-color-secondary s-align-children-center"><div class="a-section a-spacing-none a-padding-none udm-delivery-block" data-cy="delivery-block"><div class="a-row a-color-base udm-badge-block"><div class="a-column a-span12"></div></div><div class="a-row a-color-base udm-primary-delivery-message"><div class="a-column a-span12">Lieferung für 3,99 € <span class="a-text-bold" id="WVCRIAFWG">Fr., 30. Okt.</span></div></div><div class="a-row a-color-base udm-secondary-delivery-message"><div class="a-column a-span12">Oder schnellste Lieferung <span class="a-text-bold" id="WVCRIAFWG">Mi., 16. Sept.</span></div></div></div></div></div><div class="a-section a-spacing-none a-spacing-top-mini"><div class="a-row"><div class="a-section puis-cta-capsule puis-cta-capsule-container-displacement"><div class="puis-atcb-container puis-cta-row-displacement" data-atcb-props='{"cartType":"DEFAULT","locale":"de-DE","sessionId":"000-0000000-0000000","csrfToken":"REDACTED-CSRF-TOKEN"}' data-atcb-uid="atcb-B0DCNWN8NZ-1" data-cy="add-to-cart"><div class="addToCartShoppingPortalCSRFToken aok-hidden"><!-- sp:csrf --><meta content="REDACTED-CSRF-TOKEN" name="anti-csrftoken-a2z"/><!-- sp:end-csrf --></div><div class="a-section puis-atcb-add-container"><div class="a-section atc-faceout-container" data-asin="B0DCNWN8NZ"><form action="/cart/add-to-cart?ref=sr_atc_rt_add_d_1&amp;sr=8-1&amp;qid=1788872080&amp;discoveredAsins.0=B0DCNWN8NZ" class="a-spacing-none" method="post"><!-- sp:csrf --><input name="anti-csrftoken-a2z" type="hidden" value="REDACTED-CSRF-TOKEN"/><!-- sp:end-csrf --><input name="clientName" type="hidden" value="EUIC_AddToCart_Search"/><input name="items[0.base][asin]" type="hidden" value="B0DCNWN8NZ"/><input name="items[0.base][offerListingId]" type="hidden" value="REDACTED-OFFER-LISTING-ID"/><input name="items[0.base][quantity]" type="hidden" value="1"/><input name="minOrderQuantity" type="hidden" value="1"/><input name="maxOrderQuantity" type="hidden" value="2"/><input name="merchantId" type="hidden" value="A3JWKAKR8XB7XF"/><div class="a-section ax-replace a-spacing-none"><div class="ax-atc celwidget atc-btn-container" data-cel-widget="" data-csa-c-content-id="ax-atc-EUIC_AddToCart_Search-content" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="8v3xim-11kzyz-eq365c-atn4z2" data-csa-c-item-id="B0DCNWN8NZ" data-csa-c-item-type="asin" data-csa-c-merchant-id="A3JWKAKR8XB7XF" data-csa-c-pos="1" data-csa-c-price-to-pay="15.1" data-csa-c-slot-id="ax-atc-EUIC_AddToCart_Search" data-csa-c-type="item"><span class="a-declarative" data-action="puis-atcb-add-action-retail" data-puis-atcb-add-action-retail='{"asin":"B0DCNWN8NZ","url":"https://data.amazon.de/api/marketplaces/A1PA6795UKMFR9/cart/carts/retail/items?ref=sr_atc_rt_add_d_1&amp;sr=8-1&amp;qid=1788872080&amp;discoveredAsins.0=B0DCNWN8NZ","neoAtcUrl":"/cart/add-to-cart?ref=sr_atc_rt_add_d_1&amp;sr=8-1&amp;qid=1788872080&amp;discoveredAsins.0=B0DCNWN8NZ","offerListingId":"REDACTED-OFFER-LISTING-ID","additionalParameters":{},"messageSuccess":"Artikel hinzugefügt","messageError":"Artikel konnte nicht hinzugefügt werden"}' data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div data-csa-c-action-name="addToCart" data-csa-c-button-type="button" data-csa-c-content-id="s-search-add-to-cart-action" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="orf7tb-pnui7y-baqqs9-udbk7t" data-csa-c-item-id="B0DCNWN8NZ" data-csa-c-item-type="asin" data-csa-c-merchant-id="A3JWKAKR8XB7XF" data-csa-c-price-to-pay="15.1" data-csa-c-type="action"><div class="a-button-stack"><span class="a-button a-button-primary a-button-icon puis-atcb-button" id="a-autoid-3"><span class="a-button-inner"><i class="a-icon a-icon-cart"></i><input aria-label="In den Einkaufswagen" class="a-button-input" name="submit.addToCart" type="submit"/><span aria-hidden="true" class="a-button-text" id="a-autoid-3-announce">In den Einkaufswagen</span></span></span></div></div></span></div></div></form></div></div><div class="a-section puis-atcb-error-container aok-hidden"><div class="a-box a-alert-inline a-alert-inline-error" role="alert"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content"><span class="a-size-mini puis-atcb-error-message"></span></div></div></div></div><div class="a-section puis-atcb-extra-container"></div></div></div></div></div><div class="a-section a-spacing-none a-spacing-top-mini" data-cy="secondary-offer-recipe"><div class="a-row a-size-base a-color-secondary"><span class="a-size-base a-color-secondary">Andere Angebote</span><br/><span class="a-color-base">14,50 €</span><span class="a-letter-space"></span><span class="a-declarative" data-action="s-show-all-offers-display" data-csa-c-func-deps="aui-da-s-show-all-offers-display" data-csa-c-id="60rkyh-fmr7fs-ysvteo-o3bcg9" data-csa-c-type="widget" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-s-show-all-offers-display='{"assetMismatch":"Abandon","fallbackUrl":"/gp/offer-listing/B0DCNWN8NZ/ref=sr_1_1_olp?keywords=kopfhoerer&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;qid=1788872080&amp;sr=8-1","url":"/gp/aod/ajax/ref=sr_1_1_aod?asin=B0DCNWN8NZ&amp;pc=sp&amp;keywords=kopfhoerer&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;qid=1788872080&amp;sr=8-1"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="a-link-normal s-link-style s-underline-text s-underline-link-text" href="/gp/offer-listing/B0DCNWN8NZ/ref=sr_1_1_olp?keywords=kopfhoerer&amp;dib_tag=se&amp;dib=eyJ2IjoiMSJ9.gZtXwV-ynrlO2y3XsNsDHxNiVvdOjJ3S1OTOAllQVNXXt3_mZnE5l3hAfynWAG21RL0xvlLQ-0qF3Go0srtUeNkW_uOVjFRiXrkOdSxwchKqKJbwFIl2TDEQ2eVoHeLAErpVdCudJDHxfIWoFlSZB-Ovmulp-37-cOSD0MjiQv6dxGABLyQqpkqWHFAhA8lrLCDe2IntMqxqlo72VpvW5ciRvb4ATkdmGcXaaYwMfGM.RGbW8cu_VDdk0Q7LgRz-wLwSDdpMXcapG6nkB7hwUUY&amp;qid=1788872080&amp;sr=8-1">(2+ gebrauchte und neue Artikel) </a></span><div class="a-section aok-hidden" id="all-offers-display"><div class="a-spinner-wrapper aok-hidden" id="all-offers-display-spinner"><span class="a-spinner a-spinner-medium"></span></div></div><span class="a-declarative" data-action="close-all-offers-display" data-csa-c-func-deps="aui-da-close-all-offers-display" data-csa-c-id="tpcmm2-wfnubp-zfes4y-fpxaqy" data-csa-c-type="widget" data-render-id="r88gnnj1yh9n225vt75rflgil3" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="a-section aok-hidden aod-darken-background" id="aod-background"></div></span></div></div></div></div><div class="puisg-col puisg-col-4-of-4 puisg-col-4-of-8 puisg-col-4-of-12 puisg-col-4-of-16 puisg-col-8-of-20 puisg-col-8-of-24 puis-desktop-list-right-col"><div class="puisg-col-inner"></div></div></div><div class="puisg-row"></div></div></div></div></div></div></div></span></div></div></div>"""

# An amazon.co.jp tile. Its rating reads "5\u3064\u661f\u306e\u3046\u3061\u200b4.2" — the scale FIRST — which
# an "N out of 5" pattern cannot match at all. Before this was handled,
# every rating on this marketplace came back null while the page showed them.
FIX_TILE_JP_RATING = """<div class="sg-col-4-of-4 sg-col-4-of-24 sg-col-4-of-12 s-result-item s-asin sg-col-4-of-16 AdHolder sg-col s-widget-spacing-small sg-col-4-of-8 sg-col-4-of-20" data-asin="B0C9QQY6TP" data-component-type="s-search-result" data-index="3" data-uuid="19a44481-761e-4410-86e9-3900922519ba" id="19a44481-761e-4410-86e9-3900922519ba" role="listitem"><div class="sg-col-inner"><div cel_widget_id="MAIN-SEARCH_RESULTS-3" class="s-widget-container s-spacing-small s-widget-container-height-small celwidget slot=MAIN template=SEARCH_RESULTS widgetId=search-results_2" data-csa-c-asin-instance-id="aec8cad1-94be-4e2b-a054-b4f278f4ab4c" data-csa-c-content-id="search-results_2" data-csa-c-cs-type="Loom" data-csa-c-id="iuz4nq-yfcqu0-ok5ob4-y7gmkf" data-csa-c-item-id="amzn1.asin.1.B0C9QQY6TP" data-csa-c-pos="2" data-csa-c-type="item" data-csa-op-log-render="">
<div class="rush-component s-expand-height" data-component-props='{"percentageShownToFire":"50","batchable":true,"requiredElementSelector":".s-image:visible","url":"https://unagi-fe.amazon.com/1/events/com.amazon.eel.SponsoredProductsEventTracking.prod?qualifier=1788872156&amp;id=6307074884473315&amp;widgetName=sp_atf&amp;adId=300257099086262&amp;eventType=1&amp;adIndex=1"}' data-component-type="s-impression-logger">
<div class="rush-component s-featured-result-item s-expand-height" data-component-props='{"presenceCounterName":"sp_delivered","hiddenCounterName":"sp_hidden","testElementSelector":".s-image"}' data-component-type="s-impression-counter">
<span class="a-declarative" data-action="puis-card-container-declarative" data-csa-c-func-deps="aui-da-puis-card-container-declarative" data-csa-c-id="m9hh0j-bvlmd4-ajn7cy-qkky1q" data-csa-c-item-id="amzn1.asin.B0C9QQY6TP" data-csa-c-owner="puis" data-csa-c-posx="2" data-csa-c-type="item" data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div class="puis-card-container puis-overflow-hidden s-card-container aok-relative puis-expand-height puis-include-content-margin puis puis-v1v3dkg82cns7b2ucnfxri66dtm s-latency-cf-section puis-card-border" data-cy="asin-faceout-container"><div class="a-section a-spacing-base desktop-grid-content-view"><div class="s-product-image-container aok-relative s-text-center s-image-overlay-grey puis-image-overlay-grey s-padding-left-small s-padding-right-small puis-spacing-small s-height-equalized puis puis-v1v3dkg82cns7b2ucnfxri66dtm" data-cy="image-container"><span class="rush-component" data-component-type="s-product-image" data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-hidden="true" class="a-link-normal s-no-outline" href="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" tabindex="-1" target="_blank"><div class="a-section aok-relative s-image-square-aspect"><img alt="スポンサー広告 - FIFINE ゲーミングヘッドセット 3.5mm/USB有線接続 PC/スマホ/Switch/PS4/PS5に対応 配信/実況用 黒 H9 | 7.1サラウンドサウンド/50mmドライバー搭載 脱着式全指向性マイク/多機能ボ..." aria-hidden="true" class="s-image" data-image-index="2" data-image-latency="s-product-image" data-image-load="" data-image-source-density="1" src="https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL320_.jpg" srcset="https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL320_.jpg 1x, https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL480_FMwebp_QL65_.jpg 1.5x, https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL640_FMwebp_QL65_.jpg 2x, https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL800_FMwebp_QL65_.jpg 2.5x, https://m.media-amazon.com/images/I/71A9RebYT6L._AC_UL960_FMwebp_QL65_.jpg 3x"/></div></a></span><div class="a-row"><div class="a-section puis-cta-capsule puis-cta-capsule-container-displacement"><div class="puis-intentionally-empty-container-for-cta"></div></div></div></div><div class="a-section a-spacing-small puis-padding-left-small puis-padding-right-small"><div class="a-section a-spacing-none a-text-center"><div aria-label="ご利用が可能な色" class="a-section s-color-swatch-container s-color-swatch-container-left-aligned" role="group"><div class="s-color-swatch-internal-container" role="list"></div><div data-csa-c-content-id="color-swatch-more-link" data-csa-c-id="inpl2m-v5g9r0-je5pll-uqi0nj" data-csa-c-interaction-events="click" data-csa-c-product-type="HEADPHONES" data-csa-c-swatch-more-url="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" data-csa-c-swatch-remaining-count="その他+1色/柄" data-csa-c-type="link"><a aria-label="その他+1色/柄" class="a-link-normal s-color-swatch-link puis-spacing-small" href="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" role="link"><u>その他+1色/柄</u></a></div></div></div><div class="a-section a-spacing-none a-spacing-top-small s-title-instructions-style" data-cy="title-recipe"><div class="a-row a-spacing-micro"><span class="a-declarative" data-a-popover='{"closeButtonLabel":"閉じる","closeButton":"true","dataStrategy":"preload","name":"sp-info-popover-B0C9QQY6TP","position":"triggerVertical","popoverLabel":"スポンサー情報を表示、または広告フィードバックを残す"}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="ahs4r-dpgbqw-kpwcpd-581uu1" data-csa-c-type="widget" data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="puis-label-popover puis-sponsored-label-text" href="javascript:void(0)" role="button" style="text-decoration: none;"><span class="puis-label-popover-default"><span aria-label="スポンサー情報を表示、または広告フィードバックを残す" class="a-color-secondary">スポンサー</span></span><span class="puis-label-popover-hover"><span aria-hidden="true" class="a-color-base">スポンサー</span></span> <span class="aok-inline-block puis-sponsored-label-info-icon"></span></a></span><div class="a-popover-preload" id="a-popover-sp-info-popover-B0C9QQY6TP"><div class="puis puis-v1v3dkg82cns7b2ucnfxri66dtm"><span>この広告は、検索クエリに対する商品の関連性に基づいて表示されています。</span><div class="a-row"><span class="a-declarative" data-action="s-safe-ajax-modal-trigger" data-csa-c-func-deps="aui-da-s-safe-ajax-modal-trigger" data-csa-c-id="het92w-yyvs86-7dt2rt-qz6ewt" data-csa-c-type="widget" data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-s-safe-ajax-modal-trigger='{"ajaxUrl":"/af/sp-loom/feedback-form?pl=%7B%22adPlacementMetaData%22%3A%7B%22searchTerms%22%3A%22aGVhZHBob25lcw%3D%3D%22%2C%22pageType%22%3A%22Search%22%2C%22feedbackType%22%3A%22sponsoredProductsLoom%22%2C%22slotName%22%3A%22TOP%22%7D%2C%22adCreativeMetaData%22%3A%7B%22adProgramId%22%3A1024%2C%22adCreativeDetails%22%3A%5B%7B%22asin%22%3A%22B0C9QQY6TP%22%2C%22title%22%3A%22FIFINE+%E3%82%B2%E3%83%BC%E3%83%9F%E3%83%B3%E3%82%B0%E3%83%98%E3%83%83%E3%83%89%E3%82%BB%E3%83%83%E3%83%88+3.5mm%2FUSB%E6%9C%89%E7%B7%9A%E6%8E%A5%E7%B6%9A+PC%2F%E3%82%B9%E3%83%9E%E3%83%9B%2FSwitch%2FPS4%2FPS5%E3%81%AB%E5%AF%BE%E5%BF%9C+%E9%85%8D%E4%BF%A1%2F%E5%AE%9F%E6%B3%81%E7%94%A8+%E9%BB%92+H9+%7C+7.1%E3%82%B5%E3%83%A9%E3%82%A6%E3%83%B3%E3%83%89%E3%82%B5%E3%82%A6%E3%83%B3%E3%83%89%2F50mm%E3%83%89%E3%83%A9%E3%82%A4%E3%83%90%E3%83%BC%E6%90%AD%E8%BC%89+%E8%84%B1%E7%9D%80%E5%BC%8F%22%2C%22priceInfo%22%3A%7B%22amount%22%3A6099.0%2C%22currencyCode%22%3A%22JPY%22%7D%2C%22sku%22%3A%22JP260406H9%22%2C%22adId%22%3A%22A0898836ZDWS5H6CS2HE%22%2C%22campaignId%22%3A%22A02532663A2QIUITVM2DZ%22%2C%22advertiserIdNS%22%3Anull%2C%22selectionSignals%22%3Anull%7D%5D%7D%7D","dataStrategy":"ajax","header":"フィードバックを残す"}' data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="#"><span>こちらへ</span> </a> </span></div></div></div></div><a class="a-link-normal s-line-clamp-4 s-link-style a-text-normal" href="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" target="_blank"><h2 aria-label="スポンサー広告 - FIFINE ゲーミングヘッドセット 3.5mm/USB有線接続 PC/スマホ/Switch/PS4/PS5に対応 配信/実況用 黒 H9 | 7.1サラウンドサウンド/50mmドライバー搭載 脱着式全指向性マイク/多機能ボックス付き ネットワーク/ライブ配信用のおすすめ密閉型軽量ヘッドホン" class="a-size-base-plus a-spacing-none a-color-base a-text-normal"><span>FIFINE ゲーミングヘッドセット 3.5mm/USB有線接続 PC/スマホ/Switch/PS4/PS5に対応 配信/実況用 黒 H9 | 7.1サラウンドサウンド/50mmドライバー搭載 脱着式全指向性マイク/多機能ボックス付き ネットワーク/ライブ配信用のおすすめ密閉型軽量ヘッドホン</span></h2></a> </div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="reviews-block"><div class="a-row a-size-small"><span aria-hidden="true" class="a-size-small a-color-base">4.2</span><span class="a-declarative" data-a-popover='{"url":"/review/widgets/average-customer-review/popover/ref=acr_search__popover?ie=UTF8&amp;asin=B0C9QQY6TP&amp;ref=acr_search__popover&amp;contextId=search","position":"triggerBottom","closeButton":true,"popoverLabel":"5つ星のうち4.2、評価詳細","closeButtonLabel":""}' data-action="a-popover" data-csa-c-func-deps="aui-da-a-popover" data-csa-c-id="u5jeau-qi6pe-2065um-3mtpff" data-csa-c-type="widget" data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><a aria-label="5つ星のうち4.2、評価詳細" class="a-popover-trigger a-declarative mvt-review-star-mini-popover" href="javascript:void(0)" role="button"><i aria-hidden="true" class="a-icon a-icon-star-mini a-star-mini-4 mvt-review-star-mini mvt-review-star-with-margin" data-cy="reviews-ratings-slot"><span class="a-icon-alt">5つ星のうち4.2</span></i><i class="a-icon a-icon-popover"></i></a></span> <a aria-label="339 レーティング" class="a-link-normal s-underline-text s-underline-link-text null s-link-style" href="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1#customerReviews" target="_blank"><span aria-hidden="true" class="a-size-mini puis-normal-weight-text s-underline-text">(339)</span> </a> </div><div class="a-row a-size-base"><span class="a-size-base a-color-secondary">過去1か月で100点以上購入されました</span></div></div><div class="a-section a-spacing-none a-spacing-top-small s-price-instructions-style" data-cy="price-recipe"><div class="a-row a-size-base a-color-base"><div class="a-row"><span class="aok-offscreen" id="price-link">価格、商品詳細ページ</span><a aria-describedby="price-link" class="a-link-normal s-no-hover s-underline-text s-underline-link-text s-link-style a-text-normal" href="/sspa/click?ie=UTF8&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6&amp;url=%2FFIFINE-%25E3%2582%25B2%25E3%2583%25BC%25E3%2583%259F%25E3%2583%25B3%25E3%2582%25B0%25E3%2583%2598%25E3%2583%2583%25E3%2583%2589%25E3%2582%25BB%25E3%2583%2583%25E3%2583%2588-USB%25E6%259C%2589%25E7%25B7%259A%25E6%258E%25A5%25E7%25B6%259A-Switch-H9%2Fdp%2FB0C9QQY6TP%2Fref%3Dsr_1_2_sspa%3Fdib%3DeyJ2IjoiMSJ9.3z6DLEVNk3bQrCyqlUof0_WHx2fF8ZDJlO5EQAMqvJJ1w0j75H8Oeud32XO5fV4Rf4gmL_GWXXgXF6F2aGR9G-wx4UlZhx_3FEdTepbksaCXRnQquvMwkSRsUVYlCgaOHcXS_ZAWA8wgEPZ3GlXzZb2h7GFQVUYrpUqUPu32r9IM-60W8BEf4ituIGhvqsX30mKF8nWFdS-c9-advg3cv1YcDpXM7JEj_oOpsXKI8WyI8CLGGeL6NeJRd2ZNGHRvj7SgnQj7OMKJa_c124egiIF20yRgKKs0MEulPXVvWNc.vOdimrdp4tiZm16JKQ87aVLhWwIRUa-eKRvI4HuNBEA%26dib_tag%3Dse%26keywords%3Dheadphones%26qid%3D1788872156%26sr%3D8-2-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1" target="_blank"><span class="a-price" data-a-color="price" data-a-size="l"><span class="a-offscreen">EUR 33.61</span><span aria-hidden="true"><span class="a-price-symbol">EUR</span><span class="a-price-whole">33<span class="a-price-decimal">.</span></span><span class="a-price-fraction">61</span></span></span></a></div><div class="a-row"></div></div><div class="a-row a-size-small a-color-secondary"><span class="a-size-base a-color-price">610ポイント(10%)</span></div></div><div class="a-section a-spacing-none a-spacing-top-micro" data-cy="delivery-recipe"><div class="a-row a-size-base a-color-secondary s-align-children-center"><div class="a-section a-spacing-none a-padding-none udm-delivery-block" data-cy="delivery-block"><div class="a-row a-color-base udm-badge-block"><div class="a-column a-span12"></div></div><div class="a-row a-color-base udm-primary-delivery-message"><div class="a-column a-span12">配送料 EUR 26.57 <span class="a-text-bold">9月15日 火曜日</span>にお届け</div></div></div></div><div class="a-row a-size-base a-color-secondary s-align-children-center"><span class="a-size-small a-color-base">オランダに発送</span></div></div><div class="a-section a-spacing-none a-spacing-top-mini"><div class="a-row"><div class="a-section puis-cta-capsule puis-cta-capsule-container-displacement"><div class="puis-atcb-container puis-cta-row-displacement" data-atcb-props='{"cartType":"DEFAULT","locale":"ja-JP","sessionId":"000-0000000-0000000","csrfToken":"REDACTED-CSRF-TOKEN"}' data-atcb-uid="atcb-B0C9QQY6TP-2" data-cy="add-to-cart"><div class="addToCartShoppingPortalCSRFToken aok-hidden"><!-- sp:csrf --><meta content="REDACTED-CSRF-TOKEN" name="anti-csrftoken-a2z"/><!-- sp:end-csrf --></div><div class="a-section puis-atcb-add-container"><div class="a-section atc-faceout-container" data-asin="B0C9QQY6TP"><form action="/cart/add-to-cart?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788872156&amp;discoveredAsins.0=B0C9QQY6TP" class="a-spacing-none" method="post"><!-- sp:csrf --><input name="anti-csrftoken-a2z" type="hidden" value="REDACTED-CSRF-TOKEN"/><!-- sp:end-csrf --><input name="clientName" type="hidden" value="EUIC_AddToCart_Search"/><input name="items[0.base][asin]" type="hidden" value="B0C9QQY6TP"/><input name="items[0.base][offerListingId]" type="hidden" value="REDACTED-OFFER-LISTING-ID"/><input name="items[0.base][quantity]" type="hidden" value="1"/><input name="minOrderQuantity" type="hidden" value="1"/><input name="maxOrderQuantity" type="hidden" value="30"/><input name="merchantId" type="hidden" value="A3NIRL972X02X6"/><div class="a-section ax-replace a-spacing-none"><div class="ax-atc celwidget atc-btn-container" data-csa-c-content-id="ax-atc-EUIC_AddToCart_Search-content" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="6gkv3g-e2o30g-5px6xt-4e6efi" data-csa-c-item-id="B0C9QQY6TP" data-csa-c-item-type="asin" data-csa-c-merchant-id="A3NIRL972X02X6" data-csa-c-pos="2" data-csa-c-price-to-pay="33.605490" data-csa-c-slot-id="ax-atc-EUIC_AddToCart_Search" data-csa-c-type="item"><span class="a-declarative" data-action="puis-atcb-add-action-retail" data-puis-atcb-add-action-retail='{"asin":"B0C9QQY6TP","url":"https://data.amazon.co.jp/api/marketplaces/A1VC38T7YXB528/cart/carts/retail/items?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788872156&amp;discoveredAsins.0=B0C9QQY6TP","neoAtcUrl":"/cart/add-to-cart?ref=sr_atc_rt_add_d_2_sspa&amp;sr=8-2&amp;qid=1788872156&amp;discoveredAsins.0=B0C9QQY6TP","offerListingId":"REDACTED-OFFER-LISTING-ID","additionalParameters":{},"sponsoredLoggingUrl":"https://www.amazon.co.jp/sspa/click?ie=UTF8&amp;action=clickAddToCart&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6","spAttributionURL":"https://www.amazon.co.jp/sspa/click?ie=UTF8&amp;action=clickAddToCart&amp;spc=MTo2MzA3MDc0ODg0NDczMzE1OjE3ODg4NzIxNTY6c3BfYXRmOjMwMDI1NzA5OTA4NjI2Mjo6MDo6","spAttributionMethod":"POST","messageSuccess":"商品が追加されました","messageError":"商品の追加に失敗しました"}' data-render-id="rof25c2l5zxhx23z08fkxc4zel" data-version-id="v1v3dkg82cns7b2ucnfxri66dtm"><div data-csa-c-action-name="addToCart" data-csa-c-button-type="button" data-csa-c-content-id="s-search-add-to-cart-action" data-csa-c-device-env="WEB" data-csa-c-device-os="UNRECOGNIZED" data-csa-c-device-type="DESKTOP" data-csa-c-id="jg31pd-mqxw9x-kf2ch2-7qksfu" data-csa-c-item-id="B0C9QQY6TP" data-csa-c-item-type="asin" data-csa-c-merchant-id="A3NIRL972X02X6" data-csa-c-price-to-pay="33.605490" data-csa-c-type="action"><div class="a-button-stack"><span class="a-button a-button-primary a-button-icon puis-atcb-button"><span class="a-button-inner"><i class="a-icon a-icon-cart"></i><input aria-label="カートに入れる" class="a-button-input" name="submit.addToCart" type="submit"/><span aria-hidden="true" class="a-button-text">カートに入れる</span></span></span></div></div></span></div></div></form></div></div><div class="a-section puis-atcb-error-container aok-hidden"><div class="a-box a-alert-inline a-alert-inline-error" role="alert"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content"><span class="a-size-mini puis-atcb-error-message"></span></div></div></div></div><div class="a-section puis-atcb-extra-container"></div></div></div></div></div></div></div></div></span>
</div>
</div>
</div></div></div>"""

# One card from a /zgbs/ grid: the published rank ("#1"), and price and
# title in classes whose names carry a BUILD HASH
# (`_cDEzb_p13n-sc-price_3mJ9Z`), which is why the parser matches on a
# substring rather than the whole class.
FIX_BESTSELLER_CARD = """<div class="a-cardui _cDEzb_grid-cell_1uMOS expandableGrid p13n-grid-content" data-a-card-type="basic" id="p13n-asin-index-0"><div class="_cDEzb_iveVideoWrapper_JJ34T" data-asin="B08JHCVHTY"><div class="a-section zg-bdg-ctr"><div class="a-section zg-bdg-body zg-bdg-clr-body aok-float-left"><span class="zg-bdg-text">#1</span></div><div class="a-section zg-bdg-tri zg-bdg-clr-tri aok-float-left"></div></div><div class="zg-grid-general-faceout"><span><div class="p13n-sc-uncoverable-faceout" id="B08JHCVHTY"><a aria-hidden="true" class="a-link-normal aok-block" href="/Blink-Plus-Plan-monthly-auto-renewal/dp/B08JHCVHTY/ref=zg_bs_g_electronics_d_sccl_1/144-6944351-3411828?psc=1" tabindex="-1"><div class="a-section a-spacing-mini _cDEzb_noop_3Xbw5"><img alt="blink plus plan with monthly auto-renewal" class="a-dynamic-image p13n-sc-dynamic-image p13n-product-image" data-a-dynamic-image='{"https://images-na.ssl-images-amazon.com/images/I/31YHGbJsldL._AC_UL300_SR300,200_.png":[300,200],"https://images-na.ssl-images-amazon.com/images/I/31YHGbJsldL._AC_UL600_SR600,400_.png":[600,400],"https://images-na.ssl-images-amazon.com/images/I/31YHGbJsldL._AC_UL900_SR900,600_.png":[900,600]}' height="200px" src="https://images-na.ssl-images-amazon.com/images/I/31YHGbJsldL._AC_UL600_SR600,400_.png" style="max-width:300px;max-height:200px"/></div></a><div><div><a class="a-link-normal aok-block" href="/Blink-Plus-Plan-monthly-auto-renewal/dp/B08JHCVHTY/ref=zg_bs_g_electronics_d_sccl_1/144-6944351-3411828?psc=1" role="link"><span><div class="_cDEzb_p13n-sc-css-line-clamp-3_g3dy1">blink plus plan with monthly auto-renewal</div></span></a><div class="a-row"><div class="a-icon-row"><a aria-label="4.4 out of 5 stars, 279,961 ratings" class="a-link-normal" href="/product-reviews/B08JHCVHTY/ref=zg_bs_g_electronics_d_sccl_1_cr/144-6944351-3411828"><i aria-hidden="true" class="a-icon a-icon-star-small a-star-small-4-5 aok-align-top"><span class="a-icon-alt">4.4 out of 5 stars</span></i> <span aria-hidden="true" class="a-size-small">279,961</span></a></div></div><div class="a-row"><div class="a-row"><div class="_cDEzb_p13n-sc-price-animation-wrapper_3PzN2"><a class="a-link-normal aok-block a-text-normal" href="/Blink-Plus-Plan-monthly-auto-renewal/dp/B08JHCVHTY/ref=zg_bs_g_electronics_d_sccl_1/144-6944351-3411828?psc=1" role="link"><div class="a-row"><span class="a-size-base a-color-price"><span class="_cDEzb_p13n-sc-price_3mJ9Z">EUR 10.31</span></span></div></a></div></div></div></div></div></div></span></div><div class="watch-button-placeholder aok-hidden"></div></div></div>"""

# One review as rendered on /dp/B07K5214NZ to a visitor with no account.
# Pins the accessibility filler that is part of the body node's text, and the
# review's own data-asin being a colour VARIANT of the product asked for.
#
# ANONYMISED, and the only fixture here that is edited beyond stripping
# <script>/<style>. A real customer's display name, profile permalink, review
# id, title and body text were replaced with obvious placeholders before this
# was committed: the checks read the STRUCTURE of a review, not the person, and
# republishing someone's name and words inside a public repo is a different act
# from Amazon showing them on its own page. Everything Amazon generates around
# them — the accessibility filler, the date wording, the badges, the markup —
# is untouched, because that is what the checks actually exercise.
FIX_REVIEW_CONTAINER = """<div data-asin="B07K5214NZ" data-hook="reviewContainer" data-isandroidapp="false" data-iscustomerrecognized="false" data-isiosapp="false" data-iskindleapp="false" data-ismshop="false" data-locale="en-US" data-localelanguagename="English" data-marketplaceid="ATVPDKIKX0DER" data-mobile="false" data-reviewid="R000EXAMPLEREVIEW1" data-sourcelanguage="en-US" data-sourcelanguagename="English" data-translatable="false"><div class="a-section aok-relative" data-csa-c-id="u0po3c-jamixx-3kbety-pahr5y" data-csa-c-slot-id="customer_review-R000EXAMPLEREVIEW1" data-csa-c-type="widget" data-hook="review" id="R000EXAMPLEREVIEW1"><div class="a-row a-spacing-mini" data-hook="genome-widget"><a class="a-profile _Y3Itd_profile-link_2n23E" data-a-size="small" data-csa-c-id="5uwxu4-cu05qi-1h0jje-eshxi" data-csa-c-slot-id="customer_review-R000EXAMPLEREVIEW1_profile" data-csa-c-type="element" href="/gp/profile/amzn1.account.EXAMPLEACCOUNTIDEXAMPLE?ref=cm_cr_dp_d_bdcrb_top"><div aria-hidden="true" class="a-profile-avatar-wrapper"><div class="a-profile-avatar"><img class="" data-src="https://m.media-amazon.com/images/S/amazon-avatars-global/default._SX48_.png" src="https://m.media-amazon.com/images/S/amazon-avatars-global/default._SX48_.png"/></div></div><div class="a-profile-content"><span class="a-profile-name">Anonymised Reviewer</span></div></a></div><div class="_Y3Itd_single-review-star-rating-bar-desktop_1T97A"><i class="a-icon a-icon-star a-star-5" data-hook="review-star-rating"><span class="a-icon-alt">5 out of 5 stars</span></i><span class="a-letter-space"></span></div><a class="a-size-base a-color-base a-link-normal a-text-bold" href="/portal/customer-reviews/srp/-/R000EXAMPLEREVIEW1/ref=cm_cr_dp_d_rvw_ttl?_encoding=UTF8&amp;ie=UTF8"><h5 class="_Y3Itd_single-review-title_2aKRE" data-hook="reviewTitle" lang="en-US">Comfortable and sounds good</h5></a><div class="a-row a-spacing-none" data-hook="review-by-line"><span class="a-size-base a-color-tertiary" data-hook="review-date">Reviewed in the United States on August 10, 2026</span></div><div class="a-row a-spacing-mini" data-hook="product-variation-attributes"><a class="a-link-normal" data-hook="format-strip" href="/portal/customer-reviews/B07K5214NZ/ref=cm_cr_dp_d_rvw_fmt?_encoding=UTF8&amp;formatType=current_format"><span class="a-size-base a-color-secondary">Color: Rose</span></a><i class="a-icon a-icon-text-separator" role="presentation"></i><span class="_Y3Itd_inline-contributor_33jDb"><div data-hook="review-badges"><a class="a-link-normal" href="/gp/help/customer/display.html/ref=cm_cr_dp_d_rvw_avp?ie=UTF8&amp;nodeId=G8UYX7LALQC8V9KA"><span class="a-size-mini a-color-state a-text-bold" data-hook="avp-badge">Verified Purchase</span></a></div></span></div><div class="_Y3Itd_single-review-text-container_325WM" data-hook="reviewTextContainer"><div class="a-cardui-deck _Y3Itd_card-deck_37S9P" data-a-remove-bottom-gutter="true" data-a-remove-top-gutter="true" data-hook="reviewText" name="a-cardui-deck-autoname-2"><div class="a-teaser-describedby-collapsed a-hidden" id="a-cardui-deck-autoname-2-teaser-describedby-collapsed">Brief content visible, double tap to read full content.</div><div class="a-teaser-describedby-expanded a-hidden" id="a-cardui-deck-autoname-2-teaser-describedby-expanded">Full content visible, double tap to read brief content.</div><div class="a-cardui _Y3Itd_peek-expand_3G9Ub" data-a-card-type="basic" data-csa-c-id="s7x91w-t2vd2y-pzzyr-t54gz8" data-csa-c-slot-id="true" data-csa-c-type="element" name="a-cardui-deck-autoname-2-card0"><div class="a-cardui-body _Y3Itd_no-padding_1A9ha"><div class="a-reactive-container a-reactive-container-transition" style="height: 40px;"><div><div class="a-cardui-content a-cardui-uninitialized" data-a-max-height="300" style="max-height:300px"><div class="_Y3Itd_relative_1SChh"><div class="_Y3Itd_contain-rich-content_2IORW" data-hook="reviewRichContentContainer" lang="en-US"><p><span>Comfortable over long sessions and the sound is good for the price. Placeholder text: this review's wording was replaced when the fixture was committed.</span></p></div></div></div></div><div class="a-reactive-container-gradient"></div></div></div><div class="a-cardui-footer _Y3Itd_no-padding_1A9ha a-hidden" data-hook="reviewExpandButtonContainer"><div class="a-cardui-expand-control-footer _Y3Itd_read-more-less_2uDYd" data-csa-c-id="xhpzn3-pkbbhp-mzi63n-bgmgid" data-csa-c-type="widget" data-csa-interaction-events="click"><a class="a-cardui-expand-control-footer-button" href="javascript:void(0)" role="button"><span class="a-expander-icon"><i class="a-css-icon a-css-icon-draw a-css-icon-expand"></i></span><span class="a-see-more a-color-link"><span class="a-see-more-text" style="">Read more</span><span class="a-see-less-text" style="display:none">Read less</span></span></a></div></div></div></div><div class="_Y3Itd_single-review-translation-spinner-container_ypxf2" data-hook="translationSpinner" style="display: none !important;"><div class="a-spinner-wrapper _Y3Itd_single-review-translation-spinner_1tBUJ"><span class="a-spinner a-spinner-medium"></span></div></div></div><div class="a-section a-spacing-small"><div class="a-section a-spacing-none _Y3Itd_single-review-image-tile-section_15c2C"><button class="_Y3Itd_single-review-thumbnail-container_9KgXc" data-mix-operations="SingleReviewMediaImageThumbnailClickHandler" data-physicalid="EXAMPLEPHOTOID" data-reviewid="R000EXAMPLEREVIEW1"><img alt="Comfortable and sounds good" class="_Y3Itd_single-review-image-tile_3c5HW" data-hook="review-image-tile" src="https://m.media-amazon.com/images/I/61dJGjXnoOL._SY500_.jpg"/></button><button class="_Y3Itd_single-review-thumbnail-container_9KgXc" data-mix-operations="SingleReviewMediaImageThumbnailClickHandler" data-physicalid="EXAMPLEPHOTOID" data-reviewid="R000EXAMPLEREVIEW1"><img alt="Comfortable and sounds good" class="_Y3Itd_single-review-image-tile_3c5HW" data-hook="review-image-tile" src="https://m.media-amazon.com/images/I/61cx2K3E8WL._SY500_.jpg"/></button><div id="mediaModal_R000EXAMPLEREVIEW1"><div class="a-popover-preload" id="a-popover-cr-top-reviews-media-modal_R000EXAMPLEREVIEW1"><div class="_Y3Itd_media-popover-header-container_1kb6I"></div><div class="_Y3Itd_single-review-media-popover-container_FDNFG" data-hook="media-popover-container"><div class="_Y3Itd_modal-content-container_1p_Aj" data-closebuttonaria="" data-csa-c-id="v1a253-d4ptsk-v6m0d5-uw6f6q" data-csa-c-slot-id="cm_cr_image_popover" data-csa-c-type="widget" data-hook="modal-content-container" data-modaltitlearia="" data-reviewbind="SingleReviewMediaBlock" data-reviewid="R000EXAMPLEREVIEW1"><div class="_Y3Itd_media-popover-body-container_3kalb"><div class="_Y3Itd_media-popover-media-container_39x8v"><button class="_Y3Itd_left-icon-container_1saao" data-mix-operations="leftClickHandler"><span class="_Y3Itd_cr-icon_S_3MZ"><img aria-label="" class="_Y3Itd_button-icon_1Xsqv" role="img" src="https://m.media-amazon.com/images/S/sash//23pID5Mp1WTA-31.svg"/></span></button><div class="_Y3Itd_media-popover-modal-media-list-view-container_1uARM" data-reviewbind="MainMedia"><div class="_Y3Itd_media-popover-image-view-container_1XEVZ" data-csa-c-id="yxdhzg-sxfnqx-vlkl6y-tuv5ef" data-csa-c-slot-id="cm_cr_image_popover_61dJGjXnoOL_0" data-csa-c-type="element" data-lazyimagesource="https://m.media-amazon.com/images/I/61dJGjXnoOL.jpg" data-mediaid="61dJGjXnoOL"><img alt="Comfortable and sounds good" class="_Y3Itd_media-popover-image-view_1J8fN" src="https://m.media-amazon.com/images/I/61dJGjXnoOL._SY500_.jpg"/></div><div class="_Y3Itd_media-popover-image-view-container_1XEVZ" data-csa-c-id="h5cmfi-bhw3ms-iaz3rh-nxzqcd" data-csa-c-slot-id="cm_cr_image_popover_61cx2K3E8WL_1" data-csa-c-type="element" data-lazyimagesource="https://m.media-amazon.com/images/I/61cx2K3E8WL.jpg" data-mediaid="61cx2K3E8WL"><img alt="Comfortable and sounds good" class="_Y3Itd_media-popover-image-view_1J8fN" src="https://m.media-amazon.com/images/I/61cx2K3E8WL._SY500_.jpg"/></div></div><button class="_Y3Itd_right-icon-container_2ryP4" data-mix-operations="rightClickHandler"><span class="_Y3Itd_cr-icon_S_3MZ"><img aria-label="" class="_Y3Itd_button-icon_1Xsqv" role="img" src="https://m.media-amazon.com/images/S/sash//7D8iRtQ0DrKAF4O.svg"/></span></button></div><div class="_Y3Itd_media-popover-review-container_1w2Ex"><div class="a-section _Y3Itd_cr-media-popover-sidepanel-container_2rHXZ"><div class="a-section a-spacing-small _Y3Itd_cr-media-popover-sidepanel-header-section_KhwYb"><div class="a-row a-spacing-mini" data-hook="genome-widget"><a class="a-profile _Y3Itd_profile-link_2n23E" data-a-size="small" data-csa-c-id="j25ug0-h25e1q-r60gnq-txb946" data-csa-c-slot-id="profile" data-csa-c-type="element" href="/gp/profile/amzn1.account.EXAMPLEACCOUNTIDEXAMPLE"><div aria-hidden="true" class="a-profile-avatar-wrapper"><div class="a-profile-avatar"><img class="a-lazy-loaded" data-src="https://m.media-amazon.com/images/S/amazon-avatars-global/default._SX48_.png" src="https://images-na.ssl-images-amazon.com/images/G/01/x-locale/common/grey-pixel.gif" style=""/></div></div><div class="a-profile-content"><span class="a-profile-name">Anonymised Reviewer</span></div></a></div><div class="a-section a-spacing-none"><div class="_Y3Itd_single-review-star-rating-bar-desktop_1T97A"><i class="a-icon a-icon-star a-star-5" data-hook="review-star-rating"><span class="a-icon-alt">5 out of 5 stars</span></i><span class="a-letter-space"></span></div><a class="a-size-base a-color-base a-link-normal a-text-bold" href="/portal/customer-reviews/srp/-/R000EXAMPLEREVIEW1/ref=cm_cr_dp_d_rvw_ttl?_encoding=UTF8&amp;ie=UTF8"><h5 class="_Y3Itd_single-review-title_2aKRE" data-hook="reviewTitle" lang="en-US">Comfortable and sounds good</h5></a><div class="a-row a-spacing-none" data-hook="review-by-line"><span class="a-size-base a-color-tertiary" data-hook="review-date">Reviewed in the United States on August 10, 2026</span></div><div class="a-row a-spacing-mini" data-hook="product-variation-attributes"><a class="a-link-normal" data-hook="format-strip" href="/portal/customer-reviews/B07K5214NZ/ref=cm_cr_dp_d_rvw_fmt?_encoding=UTF8&amp;formatType=current_format"><span class="a-size-base a-color-secondary">Color: Rose</span></a><i class="a-icon a-icon-text-separator" role="presentation"></i><span class="_Y3Itd_inline-contributor_33jDb"><div data-hook="review-badges"><a class="a-link-normal" href="/gp/help/customer/display.html/ref=cm_cr_dp_d_rvw_avp?ie=UTF8&amp;nodeId=G8UYX7LALQC8V9KA"><span class="a-size-mini a-color-state a-text-bold" data-hook="avp-badge">Verified Purchase</span></a></div></span></div></div><p><span>Comfortable over long sessions and the sound is good for the price. Placeholder text: this review's wording was replaced when the fixture was committed.</span></p><ul class="_Y3Itd_media-popover-thumbnail-image-container_2nWBS" data-reviewbind="MediaThumbnailsBlock"><li class="_Y3Itd_media-popover-list-item-style_d1lj6" data-reviewbind="MediaThumbnail"><button aria-pressed="false" class="_Y3Itd_media-popover-thumbnail-image-button_2LRc_" data-mediaid="61dJGjXnoOL" data-mix-operations="thumbnailClickHandler" data-reviewid="R000EXAMPLEREVIEW1" data-thumbidx="0"><img alt="" class="_Y3Itd_media-popover-thumbnail-image-view_2J_oP" height="80" src="https://m.media-amazon.com/images/I/61dJGjXnoOL.jpg" width="80"/></button></li><li class="_Y3Itd_media-popover-list-item-style_d1lj6" data-reviewbind="MediaThumbnail"><button aria-pressed="false" class="_Y3Itd_media-popover-thumbnail-image-button_2LRc_" data-mediaid="61cx2K3E8WL" data-mix-operations="thumbnailClickHandler" data-reviewid="R000EXAMPLEREVIEW1" data-thumbidx="1"><img alt="" class="_Y3Itd_media-popover-thumbnail-image-view_2J_oP" height="80" src="https://m.media-amazon.com/images/I/61cx2K3E8WL.jpg" width="80"/></button></li></ul></div></div></div></div></div></div></div></div></div></div><div class="a-section a-spacing-top-small _Y3Itd_helpful-votes-container_3iphI"><span class="a-size-base a-color-tertiary" data-hook="helpful-vote-statement">4 people found this helpful</span></div><div class="a-section a-spacing-small a-spacing-top-small"><div class="_Y3Itd_single-review-vote-action-bar_10AyR"><div class="a-section a-spacing-none _Y3Itd_cr-voting-section_1cNTp" data-hook="helpfulVoteWidget"><div class="_Y3Itd_cr-helpful-button_1u3UK cr-vote-component" data-asin="B07K5214NZ" data-csa-c-id="2aurja-zgv57s-e9m5k9-ts37bj" data-csa-c-slot-id="customer_review-R000EXAMPLEREVIEW1_helpful_button" data-csa-c-type="element" data-hook="helpfulVoteButton" data-id="R000EXAMPLEREVIEW1" data-mix-operations="HelpfulVoteClickHandler" data-return-to="DpReview"><span class="a-button a-button-base" id="a-autoid-30"><span class="a-button-inner"><input aria-labelledby="a-autoid-30-announce" class="a-button-input" type="submit"/><span aria-hidden="true" class="a-button-text" id="a-autoid-30-announce"><div class="_Y3Itd_cr-helpful-text-desktop_2dXQG">Helpful</div></span></span></span></div><span class="_Y3Itd_hidden_1J5OD" data-hook="helpfulVoteLoadingMessage">Sending feedback...</span><div aria-atomic="true" aria-live="polite" class="a-box a-alert-inline a-alert-inline-success _Y3Itd_hidden_1J5OD a-spacing-block" data-hook="helpfulVoteSuccessMessage"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content">Thank you for your feedback.</div></div></div><div class="a-box a-alert-inline a-alert-inline-error _Y3Itd_hidden_1J5OD a-spacing-block" data-hook="helpfulVoteErrorMessage" role="alert"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content">Sorry, we failed to record your vote. Please try again</div></div></div></div><i aria-hidden="true" aria-label="|" class="a-icon a-icon-text-separator" role="img"></i><div data-asin="B07K5214NZ" data-csa-c-id="k80phr-dnaiuz-xyps8w-dx3hh9" data-csa-c-slot-id="customer_review-R000EXAMPLEREVIEW1_report_button" data-csa-c-type="element" data-dialog-kind="modal" data-hook="reportAbuseButton" data-id="R000EXAMPLEREVIEW1" data-mix-operations="ReportAbuseOpenDialogClickHandler" data-return-to="DpReview"><a aria-label="Report Review" class="a-size-base a-link-normal a-color-secondary a-text-normal" href="javascript:void(0)" role="button">Report</a></div><span class="_Y3Itd_hidden_ySuSy" data-hook="reportAbuseSendingMessage">Sending feedback...</span><div aria-atomic="true" aria-live="polite" class="a-box a-alert-inline a-alert-inline-success _Y3Itd_hidden_ySuSy a-spacing-block" data-hook="reportAbuseSuccessMessage"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content">Thanks, we'll investigate in the next few days.</div></div></div><div class="a-box a-alert-inline a-alert-inline-error _Y3Itd_hidden_ySuSy a-spacing-block" data-hook="reportAbuseErrorMessage" role="alert"><div class="a-box-inner a-alert-container"><i aria-hidden="true" class="a-icon a-icon-alert"></i><div class="a-alert-content">Sorry, We failed to report this review. Please try again</div></div></div><div class="a-section aok-hidden" data-closebuttonaria="Close Button" data-dialogheader="Report this review?" id="reportAbuseConfirmationDialog_R000EXAMPLEREVIEW1"><div class="_Y3Itd_reportAbuseConfirmationContainer_1UPzg"><div class="a-section a-padding-large"><div class="a-row"><p>We'll check if this review meets our <a class="a-link-normal" href="/gp/help/customer/display.html?nodeId=GLHXEX85MENUE4XF&amp;ref=cm_cr_cg" rel="noopener" target="_blank">community guidelines<span class="aok-offscreen">Opens in a new tab</span></a>. If it doesn't, we'll remove it.</p></div><div class="a-row a-spacing-top-base"><div class="_Y3Itd_reportAbuseConfirmationActions_27dEm"><div data-id="R000EXAMPLEREVIEW1" data-mix-operations="ReportAbuseCancelClickHandler"><span class="a-button a-button-base" id="a-autoid-31"><span class="a-button-inner"><input aria-labelledby="a-autoid-31-announce" class="a-button-input" data-hook="reportAbuseCancelButton" type="submit"/><span aria-hidden="true" class="a-button-text" id="a-autoid-31-announce">Cancel</span></span></span></div><div data-id="R000EXAMPLEREVIEW1" data-mix-operations="ReportAbuseConfirmClickHandler"><span class="a-button a-button-primary" id="a-autoid-32"><span class="a-button-inner"><input aria-labelledby="a-autoid-32-announce" class="a-button-input" data-hook="reportAbuseConfirmButton" type="submit"/><span aria-hidden="true" class="a-button-text" id="a-autoid-32-announce">Report</span></span></span></div></div></div></div></div></div></div></div></div><div class="aok-hidden" id="cr-review-media-popover-R000EXAMPLEREVIEW1"><div class="a-section _Y3Itd_media-popover-container_2Vf8U"><div class="_Y3Itd_media-popover-list-container_38-H0"><div class="_Y3Itd_media-popover-container-overlay_1nuZ1" data-hook="ReviewMediaPopover" id="review-media-popover-content-R000EXAMPLEREVIEW1"><div class="_Y3Itd_media-popover-content-wrapper_3MziH"><div class="_Y3Itd_media-popover-background-wrapper_2UMSb"><div class="_Y3Itd_page-indicator-container_1X84N"><div class="_Y3Itd_media-popover-page-indicator_10KaA" data-hook="PageIndicator"><div class="_Y3Itd_highlighted_2vw-O _Y3Itd_media-popover-page-indicator-element_1BBdT"></div><div class="_Y3Itd_media-popover-page-indicator-element_1BBdT"></div></div></div><div class="_Y3Itd_media-popover-topbar_knKqX"><button class="_Y3Itd_close-button_2RG2d" data-csa-c-id="2qbaek-uoffdu-79505b-7o2jwm" data-csa-c-type="element" data-hook="ReviewMediaPopoverCloseButton" data-mix-operations="ReviewMediaPopoverCloseButtonClickHandler" data-popover-id="R000EXAMPLEREVIEW1"><i class="a-icon a-icon-close-white a-icon-medium" role="presentation"></i></button></div><div class="_Y3Itd_media-popover-media-list-view-container_1kFg1" data-hook="ReviewMediaList"><button class="_Y3Itd_media-popover-media-list-overlay_2x3M5" data-mix-operations="ReviewMediaPopoverNextMediaClickHandler" data-popover-id="R000EXAMPLEREVIEW1"><div class="_Y3Itd_media-popover-image-view-container_3FDDs" data-lazyimagesource="https://m.media-amazon.com/images/I/61dJGjXnoOL.jpg" data-physicalid="EXAMPLEPHOTOID"><img alt="Comfortable and sounds good" class="_Y3Itd_media-popover-image-view_3Eum-" src="https://images-na.ssl-images-amazon.com/images/G/01/x-locale/common/transparent-pixel._V192234675_.gif"/></div><div class="_Y3Itd_media-popover-image-view-container_3FDDs" data-lazyimagesource="https://m.media-amazon.com/images/I/61cx2K3E8WL.jpg" data-physicalid="EXAMPLEPHOTOID"><img alt="Comfortable and sounds good" class="_Y3Itd_media-popover-image-view_3Eum-" src="https://images-na.ssl-images-amazon.com/images/G/01/x-locale/common/transparent-pixel._V192234675_.gif"/></div></button></div></div></div></div></div></div></div></div>"""

# Fragments of a real /dp/ page. detail_price_desktop is present and EMPTY
# while detail_price_core holds "EUR19.61" — the shape that had the parser
# reporting the struck-through list price as the price.
FIX_DETAIL_TITLE = """<span class="a-size-large product-title-word-break" id="productTitle">        ZIHNIC Bluetooth Headphones Over-Ear, Foldable Wireless and Wired Stereo Headset Micro SD/TF, FM for Cell Phone,PC,Soft Earmuffs &amp;Light Weight for Prolonged Wearing(Rose Gold)       </span>"""

FIX_DETAIL_BYLINE = """<a class="a-link-normal" href="/stores/ENJOYMUSICLIFEWITHZIHNICHEADPHONES/page/9BDC9FE8-8DA4-42D8-9E5E-2BEB971DB508?lp_asin=B07K5214NZ&amp;ref_=ast_bln&amp;store_ref=bl_ast_dp_brandlogo_sto" id="bylineInfo">Visit the ZIHNIC Store</a>"""

FIX_DETAIL_PRICE_DESKTOP = """<div class="celwidget" data-cel-widget="corePriceDisplay_desktop_feature_div" data-csa-c-asin="B07K5214NZ" data-csa-c-content-id="corePriceDisplay_desktop" data-csa-c-id="do5gvx-n6uezk-qwlif2-nxcbzr" data-csa-c-is-in-initial-active-row="false" data-csa-c-slot-id="corePriceDisplay_desktop_feature_div" data-csa-c-type="widget" data-feature-name="corePriceDisplay_desktop" id="corePriceDisplay_desktop_feature_div">
<div class="a-section apex-core-price-identifier"> <div class="a-section a-spacing-none aok-align-center aok-relative"> <span class="aok-offscreen" data-pricetopay-label="{priceToPay}" data-pricetopay-savings-label="{priceToPay} with {savings} percent savings" id="apex-pricetopay-accessibility-label"> EUR 19.61 with 5 percent savings </span> <span class="apex-savings-container">
<span aria-hidden="true" class="a-size-large a-color-price savingPriceOverride aok-align-center reinventPriceSavingsPercentageMargin savingsPercentage apex-savings-percentage">-5%</span> </span>
<span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay apex-pricetopay-value" data-a-color="base" data-a-size="xl"><span class="a-offscreen"> </span><span aria-hidden="true"><span class="a-price-symbol">EUR</span><span class="a-price-whole">19<span class="a-price-decimal">.</span></span><span class="a-price-fraction">61</span></span></span> <span class="a-size-mini a-color-base aok-align-center aok-nowrap" id="taxInclusiveMessage"> </span> <div class="a-section a-spacing-none aok-relative aok-inline-block"> </div> </div> <div class="a-section a-spacing-small aok-align-center"> <span> <span class="apex-basisprice-feature"> <span class="aok-relative"><span class="a-size-small aok-offscreen apex-basisprice-offscreen-label" data-basisprice-label="{label} {price}">Typical price: EUR 20.64</span><span aria-hidden="true" class="a-size-small a-color-secondary aok-align-center basisPrice"><span class="apex-basisprice-label">Typical price:</span> <span class="a-price a-text-price apex-basisprice-value" data-a-color="secondary" data-a-size="s" data-a-strike="true"><span class="a-offscreen">EUR20.64</span><span aria-hidden="true">EUR20.64</span></span> </span></span> <span class="a-size-small aok-align-center basisPriceLegalMessage apex-basisprice-legal-message-icon"> <span class="a-declarative" data-a-popover='{"name":"basisPriceLegalMessageDisplayPreload-6c545bd8-02dd-4c28-becd-67eb7102c234","position":"triggerBottom","closeButton":"true"}' data-action="a-popover"> <a class="a-align-center a-link-normal aok-inline-block" href="#"> <img aria-label="Learn more about Amazon pricing and savings" height="15" role="img" src="https://m.media-amazon.com/images/S/sash//GN8m8-lU2_Dj38v.svg" width="12"/> </a> </span> <div class="a-popover-preload" id="a-popover-basisPriceLegalMessageDisplayPreload-6c545bd8-02dd-4c28-becd-67eb7102c234"> <div aria-label="Details" aria-modal="true" class="a-section a-spacing-none apex-basisprice-legal-message-popover-content" data-popover-name="basisPriceLegalMessageDisplayPreload-6c545bd8-02dd-4c28-becd-67eb7102c234" role="dialog"> <span class="a-size-base apex-basisprice-legal-message-popover-content-text">The Typical Price is determined using the 90-day median price paid by customers for the product in the Amazon store. We exclude prices paid by customers for the product when it has been on promotion for a limited time and prices paid in the Amazon Haul or Amazon Now store.<br/><a class="a-link-normal" href="/gp/help/customer/display.html?nodeId=GQ6B6RH72AX8D2TD&amp;ref_=dp_hp&amp;language=en_US" target="_blank">Learn more</a></span> </div> </div> 
</span> </span> </span> </div> </div> </div>"""

FIX_DETAIL_PRICE_CORE = """<div class="celwidget" data-cel-widget="corePrice_feature_div" data-csa-c-asin="B07K5214NZ" data-csa-c-content-id="corePrice" data-csa-c-id="6oixgt-awf1gv-fx2hwr-fri8eu" data-csa-c-is-in-initial-active-row="false" data-csa-c-slot-id="corePrice_feature_div" data-csa-c-type="widget" data-feature-name="corePrice" id="corePrice_feature_div">
<div data-csa-c-content-id="apex_with_rio_cx" data-csa-c-id="qifsxz-9erd57-f6bhux-ladmup" data-csa-c-slot-id="apex_dp_offer_display" data-csa-c-type="widget">
<div class="a-section a-spacing-micro"> <div class="a-section apex-core-price-identifier"> <span class="a-price aok-align-center apex-pricetopay-value" data-a-color="base" data-a-size="xl"><span class="a-offscreen">EUR19.61</span><span aria-hidden="true"><span class="a-price-symbol">EUR</span><span class="a-price-whole">19<span class="a-price-decimal">.</span></span><span class="a-price-fraction">61</span></span></span> <span class="a-size-mini a-color-base aok-align-center aok-nowrap" id="taxInclusiveMessage"> </span> </div> </div> </div>
</div>"""

FIX_DETAIL_ACR = """<div data-asin="B07K5214NZ" data-ref="dpx_acr_pop_" id="averageCustomerReviews">
<span class="a-declarative" data-acrstarslink-click-metrics="{}" data-action="acrStarsLink-click-metrics"> <span class="reviewCountTextLinkedHistogram noUnderline" id="acrPopover" title="4.4 out of 5 stars">
<span class="a-declarative" data-a-popover='{"max-width":"700","closeButton":"true","closeButtonLabel":"Close","position":"triggerBottom","popoverLabel":"Customer Reviews Ratings Summary","url":"/gp/customer-reviews/widgets/average-customer-review/popover/ref=dpx_acr_pop_?contextId=dpx&amp;asin=B07K5214NZ"}' data-action="a-popover"> <a class="a-popover-trigger a-declarative mvt-cm-cr-review-stars-mini-popover" href="javascript:void(0)" role="button"> <span aria-hidden="true" class="a-size-small a-color-base"> 4.4 </span> <i class="a-icon a-icon-star-mini a-star-mini-4-5 mvt-cm-cr-review-stars-mini"><span class="a-icon-alt">4.4 out of 5 stars</span></i> <i class="a-icon a-icon-popover"></i></a> </span> </span>
</span> <span class="a-letter-space"></span> <span class="a-declarative" data-acrlink-click-metrics="{}" data-action="acrLink-click-metrics"> <a class="a-link-normal" href="#averageCustomerReviewsAnchor" id="acrCustomerReviewLink"> <span aria-label="87,349 Reviews" class="a-size-small" id="acrCustomerReviewText">(87,349)</span> </a> </span> 
</div>"""

FIX_DETAIL_AVAILABILITY = """<div class="a-section a-spacing-base a-spacing-top-micro }" id="availability"> <span class="a-size-medium a-color-success primary-availability-message"> In Stock </span>  <br/> </div>"""

FIX_DETAIL_BREADCRUMBS = """<div class="a-subheader a-breadcrumb feature" data-cel-widget="wayfinding-breadcrumbs_feature_div" data-feature-name="wayfinding-breadcrumbs" id="wayfinding-breadcrumbs_feature_div"><ul class="a-unordered-list a-horizontal a-size-small"><li><span class="a-list-item"><a class="a-link-normal a-color-tertiary" href="/electronics-store/b/ref=dp_bc_1?ie=UTF8&amp;node=172282">Electronics</a></span></li><li aria-hidden="true" class="a-breadcrumb-divider" role="presentation"><span class="a-list-item a-color-tertiary">›</span></li><li><span class="a-list-item"><a class="a-link-normal a-color-tertiary" href="/Headphones-Earbuds-Accessories/b/ref=dp_bc_2?ie=UTF8&amp;node=24046923011">Headphones, Earbuds &amp; Accessories</a></span></li><li aria-hidden="true" class="a-breadcrumb-divider" role="presentation"><span class="a-list-item a-color-tertiary">›</span></li><li><span class="a-list-item"><a class="a-link-normal a-color-tertiary" href="/Headphones-Accessories-Supplies/b/ref=dp_bc_3?ie=UTF8&amp;node=172541">Headphones &amp; Earbuds</a></span></li><li aria-hidden="true" class="a-breadcrumb-divider" role="presentation"><span class="a-list-item a-color-tertiary">›</span></li><li><span class="a-list-item"><a aria-current="page" class="a-link-normal a-color-tertiary" href="/Over-Ear-Headphones/b/ref=dp_bc_4?ie=UTF8&amp;node=12097479011">Over-Ear Headphones</a></span></li></ul></div>"""

FIX_DETAIL_BULLETS = """<div class="a-section a-spacing-medium a-spacing-top-small" id="feature-bullets"> <hr aria-hidden="true" class="a-divider-normal"/> <h1 class="a-size-base-plus a-text-bold"> About this item </h1> <ul class="a-unordered-list a-vertical a-spacing-mini"> <li class="a-spacing-mini"><span class="a-list-item"> 【ASTONISHING SOUND PRODUCTION 】: High Definition Stereo Headphones, specially developed software and noise reduction technology designed to prevent you from heating ambient noises and makes you focus on what you want to hear. Lose yourself in immersive music even in the lowest volume levels! The goal that provide Customers with outstanding sound quality is our constant pursuit.  </span></li> <li class="a-spacing-mini"><span class="a-list-item"> 【BUILT FOR YOUR COMFORTABILITY 】: The Earmuff is made by artificial leather, ensuring lasting comfort. They are foldable and stretchable, which allows you to find the perfect fit without constraint and excellent durability. Zihnic is the best choice for travel, sport and daily use by Unisex Kids, Teens and Adults.  </span></li> <li class="a-spacing-mini"><span class="a-list-item"> SEAMLESS BLUETOOTH CONNECTION】: Built to provide a quick and stable Bluetooth connection . Just slide the on/off button and the headphones will be in ready to pair mode. The Wireless Headphones are compatible with all Bluetooth or 3.5mm plug cable enabled devices! You can also receive calls and have hands-free communication through the special noise reduction technology microphone Zihnic Headphones are compatible with all Phones X, 8 Plus, Samsung S9, S8, Pads, Pods, Huawei, Nexus, Amazon Alex.  </span></li> <li class="a-spacing-mini"><span class="a-list-item"> ERGONOMIC DESIGN】: Zihnic headphones are built from materials that are extremely nice to touch which provides the model premium outlook. The super soft memory-protein foam leather earmuffs and headbands contribute to maximum comfort regardless of how long you use them. Zihnic headphones also come with a protective Premium Case - a great way to reduce wear and tear.  </span></li> <li class="a-spacing-mini"><span class="a-list-item"> LONG BATTERY LIFE &amp; DUAL MODE】: Zihnic Headphones are Rechargeable. 450mAh battery, 14 hours of music time, 2.5 hours Fast Charging. After 20 hours of playtime, you can switch to wired mode and enjoy your music NON-STOP. You do not need to worry about power shortage problem for the long travel. By choosing Zihnic, You are covered with 12 Months warranty and 100% Customer satisfaction in addition to 24/7 Customer Support service.  </span></li> </ul> <div class="a-section" data-csa-c-content-id="voyager-product-details-jumplink" data-csa-c-id="1xdbyj-yy34na-verv2k-oja5va" data-csa-c-slot-id="voyager-product-details-jumplink" data-csa-c-type="link"> <span class="caretnext">›</span> <a class="a-link-normal" href="#productDetails" id="seeMoreDetailsLink"> See more product details </a> </div> </div>"""

# Amazon's AWS WAF interstitial, served verbatim with HTTP 202 on
# amazon.co.uk. It clears ITSELF in a browser (measured 3.9s), so this
# fixture exists to pin that it is NOT reported as a block and NOT sent to
# a captcha solver.
FIX_WAF_CHALLENGE_PAGE = """<!DOCTYPE html><html lang="en"><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title></title>
    <style>
        body {
            font-family: "Arial";
        }
    </style>
    <script type="text/javascript">
    window.awsWafCookieDomainList = [];
    window.gokuProps = {
"key":"AQIDAHjcYu/GjX+QlghicBgQ/7bFaQZ+m5FKCMDnO+vTbNg96AEMRnD6F+MvkJJqBh8rzCTUAAAAfjB8BgkqhkiG9w0BBwagbzBtAgEAMGgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM5XOiALsNwvWxyKFTAgEQgDsSpBZAKGlrGTnmvKW8ipAVOF24QPSIJzRVAVt5EwIm35SZiMIEMlo9Pylxofy6VjXjnwBfNnSAmwfeCA==",
          "iv":"NC4WbQEYXgAAMWrw",
          "context":"7Y4zw4n2F1cuCtYmrQDRj3AooF/xDqqWRSDrzOon04tVVSPi85B9gLBpgSVNhgdSq6VYOHAsw/WQvvlgzdXZQo/Lbr152ZJa8ntNwHEQ5zuamh+aKi7DUpqrY5zItws1zWcPYhS5fb4tFKSsZ2uzY8e03+VOlIU87902vvF7WvgPmja7pE3a7P4/tAK3CzS+3NIxEQC/Xs+wwTIRd4ne3TwHYszdkd+Gxq1CddMmgcXzjQNPESNeBUIvMbpU8UkHZUdEHUBhSyx4p6pcadJq5L11IPHcTu80c4Y6h5yK9pRNZAoYwKMerRvnIAXl4OYrNwu4jjS3/5emWB1MP68fShEVDeECl9J1BwRVZdCMY3z04ZBcOP1RdtPRULKa97zLXICToA=="
};
    </script>
    <script src="https://1c5c1ecf7303.535706ac.eu-west-1.token.awswaf.com/1c5c1ecf7303/2d9e2c31d174/d26d5ce40abb/challenge.js"></script>
</head>
<body>
    <div id="challenge-container"></div>
    <script type="text/javascript">
        AwsWafIntegration.saveReferrer();
        AwsWafIntegration.checkForceRefresh().then((forceRefresh) => {
            if (forceRefresh) {
                AwsWafIntegration.forceRefreshToken().then(() => {
                    window.location.reload(true);
                });
            } else {
                AwsWafIntegration.getToken().then(() => {
                    window.location.reload(true);
                });
            }
        });
    </script>
    <noscript>
        <h1>JavaScript is disabled</h1>
        In order to continue, we need to verify that you're not a robot.
        This requires JavaScript. Enable JavaScript and then reload the page.
    </noscript>

</body></html>"""

# Amazon's 503 throttle page from amazon.de — "Tut uns Leid". Retryable: a
# fresh context got 200 seconds later.
FIX_THROTTLE_PAGE_DE = """<html><head>
    <meta http-equiv="content-type" content="text/html; charset=UTF-8">
    <meta charset="utf-8">
    <meta http-equiv="X-UA-Compatible" content="IE=edge,chrome=1">
<title>
Tut uns Leid!
</title>

<style type="text/css"><!--
.serif { font-family: times,serif; font-size: small; }
.sans { font-family: verdana,arial,helvetica,sans-serif; font-size: small; }
.small { font-family: verdana,arial,helvetica,sans-serif; font-size: x-small; }
.h1 { font-family: verdana,arial,helvetica,sans-serif; color: #CC6600; font-size: small; }
.h3color { font-family: verdana,arial,helvetica,sans-serif; color: #CC6600; font-size: x-small; }
.tiny { font-family: verdana,arial,helvetica,sans-serif; font-size: xx-small; }
.listprice { font-family: arial,verdana,helvetica,sans-serif; text-decoration: line-through; font-size: x-small; }
.price { font-family: verdana,arial,helvetica,sans-serif; color: #990000; font-size: x-small; }
--></style>
</head>

<body bgcolor="#FFFFFF" link="#003399" alink="#FF9933" vlink="#996633" text="#000000">

<!--
        To discuss automated access to Amazon data please contact api-services-support@amazon.com.
        For information about migrating to our APIs refer to our Marketplace APIs at https://developer.amazonservices.de/ref=rm_5_sv, or our Product Advertising API at https://partnernet.amazon.de/gp/advertising/api/detail/main.html/ref=rm_5_ac for advertising use cases.
-->

<center>
<a href="https://www.amazon.de/ref=cs_503_logo/">
<img src="https://images-eu.ssl-images-amazon.com/images/G/03/general/de-logo-153x37.gif" width="153" height="37" alt="Amazon.de" border="0"></a>
<p>

<table cellpadding="3" width="90%" bgcolor="#ffffff" border="0" cellspacing="2" align="center">
<tbody><tr>
<td>
<h2>Tut uns Leid!</h2>


Während wir Ihre Eingabe ausführen wollten, ist ein technischer Fehler aufgetreten. Wir arbeiten bereits daran und werden sobald wie möglich wieder für Sie da sein. Bitte schauen Sie später wieder vorbei.<p>

Für diese Unannehmlichkeit bitten wir Sie vielmals um Entschuldigung und danken für Ihr Verständnis.</p><p>

Ihr Team von Amazon.de

</p></td></tr>
</tbody></table>
<b><a href="https://www.amazon.de/ref=cs_503_link/">Klicken Sie hier, um zurück zur Homepage Amazon.de</a></b>
</p></center>



</body></html>"""


def page(*fragments):
    """Wrap fragments in a minimal document, as the engines hand it over."""
    return "<html><body>%s</body></html>" % "".join(fragments)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("price and currency parsing")
    prices = product_parser._prices_in
    ok = True

    ok &= check("'EUR 34.40' (ISO code, NBSP) -> 34.40 EUR",
                prices("EUR 34.40") == ([34.40], "EUR"))
    # A detail page renders the code hard against the number, a listing tile
    # separates them. Both must parse; the earlier pattern required a space
    # and read the detail page's price as no price at all.
    ok &= check("'EUR19.61' (no separator at all) -> 19.61 EUR",
                prices("EUR19.61") == ([19.61], "EUR"))
    ok &= check("'179,99 €' (comma decimal, NBSP, trailing symbol) -> 179.99 EUR",
                prices("179,99 €") == ([179.99], "EUR"))
    ok &= check("'$1,234.56' -> 1234.56 USD", prices("$1,234.56") == ([1234.56], "USD"))
    ok &= check("'1.234,56 €' (EU convention) -> 1234.56 EUR",
                prices("1.234,56 €") == ([1234.56], "EUR"))

    # Space grouping, in all four space characters a rendered page uses. A
    # plain-space-only pattern read "1 234 €" as 234 — an order of magnitude
    # out, silently.
    for name, space in (("plain space", " "), ("NBSP", " "),
                        ("narrow NBSP", " "), ("thin space", " ")):
        ok &= check("space grouping with %s: '1%s234 €' -> 1234.0" % (name, space),
                    prices("1%s234 €" % space) == ([1234.0], "EUR"))

    # A single separator followed by exactly three digits is a thousands
    # grouping: no currency here has a three-digit subunit.
    ok &= check("'$1,234' -> 1234.0, not 1.234", prices("$1,234") == ([1234.0], "USD"))
    ok &= check("'$1,23' -> 1.23 (two trailing digits is a decimal)",
                prices("$1,23") == ([1.23], "USD"))

    # The ISO-code form is matched against an allowlist. A bare [A-Z]{3}
    # would turn a size chart into a price.
    ok &= check("'XXL 100' is not a price (XXL is not a currency)",
                prices("XXL 100") == ([], None))
    ok &= check("'USB 3' is not a price", prices("USB 3") == ([], None))
    ok &= check("'100 CHF' (trailing ISO code) -> 100.0 CHF",
                prices("100 CHF") == ([100.0], "CHF"))

    # Currency tiers. A written code and a compound symbol name themselves; a
    # bare local symbol is a guess the marketplace can narrow.
    ok &= check("'HK$1,234' -> HKD, not USD (compound symbol beats bare $)",
                prices("HK$1,234") == ([1234.0], "HKD"))
    ok &= check("bare 'kr' with no marketplace known -> amount, currency None",
                prices("199 kr") == ([199.0], None))
    ok &= check("bare 'kr' on amazon.se -> SEK",
                prices("199 kr", "SEK") == ([199.0], "SEK"))
    ok &= check("bare '$' with amazon.com known -> USD",
                prices("$5.00", "USD") == ([5.0], "USD"))
    ok &= check("no money in the text -> ([], None), never a defaulted USD",
                prices("Free returns") == ([], None))
    return ok


# ---------------------------------------------------------------------------
# Listing tiles
# ---------------------------------------------------------------------------
def test_search_tiles():
    group("search tiles (real capture)")
    ok = True
    rows = parse_products(page(FIX_TILE_SPONSORED_COUPON),
                          "https://www.amazon.com/s?k=wireless+headphones")
    ok &= check("one tile in, one row out", len(rows) == 1)
    r = rows[0]
    ok &= check("sku is the ASIN from data-asin", r.sku == "B0HGFFLDYL")
    # The tile's own link is a /sspa/click tracker with the product path
    # percent-encoded inside it, so the row's url is rebuilt from the ASIN:
    # stable between runs, and correct for sponsored tiles too.
    ok &= check("url is the canonical /dp/{ASIN}, not the sspa click tracker",
                r.url == "https://www.amazon.com/dp/B0HGFFLDYL"
                and "sspa" not in r.url)
    ok &= check("price read from .a-price .a-offscreen (85.15), not the "
                "self-concatenated .a-price text (8515)", r.price == 85.15)
    ok &= check("currency EUR (a cross-border visitor is served the ISO code)",
                r.currency == "EUR")
    ok &= check("original_price is the struck-through list price", r.original_price == 170.33)
    ok &= check("discount computed from the two prices, not read from a flash",
                r.discount_pct == 50.0)
    ok &= check("rating 4.6 from the icon's accessible text", r.rating == 4.6)
    # 465 is what a digit sweep over "4.6 out of 5 stars, 28 ratings"
    # produces. It shipped in live runs before this check existed.
    ok &= check("review_count 28 (the number BEFORE 'ratings'), not 465 "
                "(rating+scale+count concatenated)", r.review_count == 28)
    ok &= check("sponsored flagged, not dropped", r.sponsored is True)
    # The label is rendered twice (screen-reader + visible), and the amount
    # carries an NBSP that _clean_text normalises to a plain space — so the
    # column holds one readable value rather than "Save EUR 42.15 EUR 42.15
    # off coupon" with a character no consumer expects.
    ok &= check("coupon label de-duplicated and NBSP-normalised to "
                "'Save EUR 42.15'", r.coupon == "Save EUR 42.15")
    ok &= check("price_source records which node was read", r.price_source == "offscreen")
    ok &= check("position from data-index", r.position == 3)
    ok &= check("page defaults to 1 when the URL says nothing", r.page == 1)
    ok &= check("category defaults to the search term", r.category == "wireless headphones")
    ok &= check("source is the marketplace host", r.source == "amazon.com")
    ok &= check("brand is null on a listing tile (Amazon renders no brand "
                "field there) rather than guessed from the title",
                r.brand is None)
    ok &= check("image url captured", (r.image_url or "").startswith("https://m.media-amazon.com/"))

    # The withheld-price tile. Null here is the site's answer, not a failure.
    rows = parse_products(page(FIX_TILE_BADGE_COM),
                          "https://www.amazon.com/s?k=wireless+headphones&page=2")
    r = rows[0]
    ok &= check("a tile Amazon renders without a price gives price None, and "
                "the row is still kept", r.price is None and r.sku == "B0C3HCD34R")
    ok &= check("no price means no currency and no price_source either — not "
                "a defaulted USD", r.currency is None and r.price_source is None)
    ok &= check("badge 'Overall Pick' read", r.badge == "Overall Pick")
    ok &= check("not sponsored: False, not None — the page was checked",
                r.sponsored is False)
    ok &= check("page read from ?page=2, so position is unambiguous across pages",
                r.page == 2 and r.position == 4)
    ok &= check("review_count 73731 (matches this ASIN's own detail page)",
                r.review_count == 73731)
    return ok


def test_localised_tiles():
    group("localised tiles (amazon.de, amazon.co.jp)")
    ok = True
    r = parse_products(page(FIX_TILE_DE_SPLIT_BADGE),
                       "https://www.amazon.de/s?k=kopfhoerer")[0]
    ok &= check("de: '15,10 €' parsed as 15.10 EUR", r.price == 15.10 and r.currency == "EUR")
    ok &= check("de: list price 19,00 € -> 19.0, discount 20.5%",
                r.original_price == 19.0 and r.discount_pct == 20.5)
    # Amazon styles a badge by splitting its label across spans. Reading one
    # span gives "Amazons".
    ok &= check("de: badge joined across spans -> 'Amazons Tipp', not 'Amazons'",
                r.badge == "Amazons Tipp")
    ok &= check("de: rating 4.6 from 'von 5 Sternen' wording", r.rating == 4.6)
    ok &= check("de: source amazon.de and url on that host",
                r.source == "amazon.de" and r.url.startswith("https://www.amazon.de/dp/"))
    ok &= check("de: zero-width characters stripped from the title",
                "​" not in (r.title or "") and r.title == "Apple EarPods (USB-C)")

    r = parse_products(page(FIX_TILE_JP_RATING),
                       "https://www.amazon.co.jp/s?k=headphones")[0]
    # The scale comes FIRST in Japanese. An "N out of 5" pattern matches
    # nothing, and every rating on this marketplace was null before.
    ok &= check("jp: rating 4.2 from '5つ星のうち 4.2' (scale stated first)",
                r.rating == 4.2)
    ok &= check("jp: review_count 339 from '339 レーティング', not 542 "
                "(scale+rating concatenated)", r.review_count == 339)
    ok &= check("jp: price served in EUR to a European exit — currency follows "
                "the delivery country, NOT the domain",
                r.currency == "EUR" and r.price == 33.61)
    ok &= check("jp: source amazon.co.jp", r.source == "amazon.co.jp")
    return ok


def test_bestseller_cards():
    group("best-seller grid (real capture)")
    ok = True
    rows = parse_products(page(FIX_BESTSELLER_CARD),
                          "https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/")
    ok &= check("one card in, one row out", len(rows) == 1)
    r = rows[0]
    ok &= check("sku from the card's inner data-asin", r.sku == "B08JHCVHTY")
    # The class is `_cDEzb_p13n-sc-price_3mJ9Z` — a build hash. Matching the
    # whole name would break on Amazon's next deploy.
    ok &= check("price read through a substring class match, despite the "
                "build-hashed class name", r.price == 10.31)
    ok &= check("position is the PUBLISHED rank (#1), read not counted",
                r.position == 1)
    ok &= check("review_count 279961, not 445279961 (the digit sweep that "
                "shipped garbage in a live run)", r.review_count == 279961)
    ok &= check("rating 4.4", r.rating == 4.4)
    ok &= check("category from the department slug", r.category == "electronics")
    ok &= check("a grid shows one price, so original_price stays null",
                r.original_price is None)
    return ok


def test_url_fallback_and_tile_scope():
    group("URL-pattern fallback and tile scoping")
    ok = True
    # Neither primary anchor present: the parser must still return rows
    # rather than report an empty category.
    fallback_html = """
    <div class="results">
      <div class="card">
        <a href="/some-product/dp/B000000001/ref=sr_1_1">First product</a>
        <span class="a-price"><span class="a-offscreen">EUR 10.00</span></span>
      </div>
      <div class="card">
        <a href="/other-product/dp/B000000002/ref=sr_1_2">Second product</a>
        <span class="a-price"><span class="a-offscreen">EUR 99.00</span></span>
      </div>
    </div>"""
    rows = parse_products(page(fallback_html), "https://www.amazon.com/s?k=x")
    ok &= check("fallback path finds both products by URL pattern", len(rows) == 2)
    by_sku = {r.sku: r for r in rows}
    # One level too wide and every tile reports its neighbours' prices — the
    # "junk-link data theft" failure this scoping exists to prevent.
    ok &= check("each row gets its OWN tile's price, not its neighbour's",
                by_sku["B000000001"].price == 10.0
                and by_sku["B000000002"].price == 99.0)
    ok &= check("fallback rows say where the price came from", 
                all(r.price_source == "split" for r in rows))

    # A sponsored click-tracker hides the real path inside a query parameter.
    spa = ("/sspa/click?ie=UTF8&spc=abc&url=%2FCancelling-Headphones%2Fdp%2F"
           "B0C3HCD34R%2Fref%3Dsr_1_3&qualifier=1")
    ok &= check("ASIN recovered from a percent-encoded sspa click tracker",
                product_parser._asin_from_href(spa) == "B0C3HCD34R")
    ok &= check("ASIN recovered from a plain /dp/ href",
                product_parser._asin_from_href("/x/dp/B000000003/ref=y") == "B000000003")
    ok &= check("a href with no ASIN gives None",
                product_parser._asin_from_href("/gp/help/customer/display.html") is None)

    # data-asin="" appears on spacer rows inside the results list.
    ok &= check("an empty data-asin is not an ASIN",
                product_parser._valid_asin("") is None
                and product_parser._valid_asin(None) is None)
    ok &= check("a 9-character id is not an ASIN",
                product_parser._valid_asin("B00000000") is None)
    return ok


# ---------------------------------------------------------------------------
# Detail page and reviews
# ---------------------------------------------------------------------------
DETAIL_PAGE = None  # built in main(), from the fragments below


def test_product_detail():
    group("product detail page (real capture)")
    ok = True
    html = page(FIX_DETAIL_TITLE, FIX_DETAIL_BYLINE, FIX_DETAIL_PRICE_DESKTOP,
                FIX_DETAIL_PRICE_CORE, FIX_DETAIL_ACR, FIX_DETAIL_AVAILABILITY,
                FIX_DETAIL_BREADCRUMBS, FIX_DETAIL_BULLETS)
    rows = parse_product_detail(html, "https://www.amazon.com/dp/B07K5214NZ")
    ok &= check("a detail page yields exactly one row", len(rows) == 1)
    r = rows[0]
    ok &= check("sku from the URL", r.sku == "B07K5214NZ")
    # The desktop price container is PRESENT AND EMPTY on this capture while
    # the next one holds the number, and the list price lives in the same
    # container as the price. Both are why the price used to come back as
    # 20.64 — the struck-through figure.
    ok &= check("price is 19.61 (what is charged), not 20.64 (struck through)",
                r.price == 19.61)
    ok &= check("original_price is the struck-through 20.64", r.original_price == 20.64)
    ok &= check("discount 5.0% computed from the pair", r.discount_pct == 5.0)
    ok &= check("price_source says 'detail'", r.price_source == "detail")
    ok &= check("brand unwrapped from 'Visit the ZIHNIC Store'", r.brand == "ZIHNIC")
    # Read from the whole document, this matched a carousel item's aria-label
    # and reported ITS count (13,930) for this product.
    ok &= check("review_count 87349 from this product's own block, not a "
                "carousel item's", r.review_count == 87349)
    ok &= check("rating 4.4", r.rating == 4.4)
    ok &= check("category from the site's own breadcrumb",
                (r.category or "").startswith("Electronics > Headphones"))
    ok &= check("availability text kept verbatim", r.availability == "In Stock")
    ok &= check("bullets captured as a list", len(r.bullets or []) == 5)
    ok &= check("listing-only columns stay null on a detail row",
                r.position is None and r.sponsored is None and r.page is None)

    # in_stock is decided from MARKUP, not from words: #availability reads
    # "In Stock" / "Auf Lager" / "在庫あり" depending on the marketplace.
    ok &= check("in_stock is None when neither control is present — 'we could "
                "not tell' is not 'no'", r.in_stock is None)
    with_cart = parse_product_detail(
        page(FIX_DETAIL_TITLE, '<input id="add-to-cart-button">'),
        "https://www.amazon.com/dp/B07K5214NZ")
    ok &= check("in_stock True from an add-to-cart control, language-neutrally",
                with_cart and with_cart[0].in_stock is True)
    out_of_stock = parse_product_detail(
        page(FIX_DETAIL_TITLE, '<div id="outOfStock">Derzeit nicht verfügbar</div>'),
        "https://www.amazon.com/dp/B07K5214NZ")
    ok &= check("in_stock False from the out-of-stock block",
                out_of_stock and out_of_stock[0].in_stock is False)

    # A page that is not a detail page must give nothing rather than a row of
    # nulls that looks like a product.
    ok &= check("no #productTitle -> no row (a warning, not a fake product)",
                parse_product_detail(page("<div>nothing here</div>"),
                                     "https://www.amazon.com/dp/B07K5214NZ") == [])
    return ok


def test_reviews():
    group("reviews rendered on a detail page (real capture)")
    ok = True
    rows = parse_reviews(page(FIX_REVIEW_CONTAINER),
                         "https://www.amazon.com/dp/B07K5214NZ")
    ok &= check("one container in, one Review out", len(rows) == 1)
    r = rows[0]
    ok &= check("review_id from data-reviewid", r.review_id == "R000EXAMPLEREVIEW1")
    ok &= check("rating 5.0 from the review's own star node", r.rating == 5.0)
    ok &= check("author read", r.author == "Anonymised Reviewer")
    ok &= check("verified purchase flagged", r.verified_purchase is True)
    ok &= check("helpful votes parsed from '4 people found this helpful'",
                r.helpful_votes == 4)
    ok &= check("variant captured", r.variant == "Color: Rose")
    ok &= check("date kept verbatim, not parsed to a date",
                (r.review_date or "").startswith("Reviewed in the United States on"))
    # The body node's own text begins with an accessibility affordance.
    ok &= check("accessibility filler stripped from the body",
                "double tap" not in (r.body or "")
                and (r.body or "").startswith("Comfortable over long sessions"))
    ok &= check("title has no rating text prefixed",
                r.title == "Comfortable and sounds good")
    ok &= check("url is the per-review permalink, not the product page",
                r.url == "https://www.amazon.com/gp/customer-reviews/R000EXAMPLEREVIEW1")
    ok &= check("sku is the ASIN that was REQUESTED", r.sku == "B07K5214NZ")
    ok &= check("variant_asin null when the review is on the same ASIN",
                r.variant_asin is None)

    # A review attached to a colour variant: the row must still be joinable
    # to what the caller asked for.
    variant_html = FIX_REVIEW_CONTAINER.replace('data-asin="B07K5214NZ"',
                                                'data-asin="B0CQXMXJC5"', 1)
    v = parse_reviews(page(variant_html), "https://www.amazon.com/dp/B07K5214NZ")[0]
    ok &= check("a variant's review keys on the requested ASIN, with the "
                "variant recorded separately",
                v.sku == "B07K5214NZ" and v.variant_asin == "B0CQXMXJC5")

    # "One person found this helpful" has no digit in it at all.
    worded = FIX_REVIEW_CONTAINER.replace("4 people found this helpful",
                                          "One person found this helpful")
    w = parse_reviews(page(worded), "https://www.amazon.com/dp/B07K5214NZ")[0]
    ok &= check("'One person found this helpful' -> 1, not None",
                w.helpful_votes == 1)
    return ok


# ---------------------------------------------------------------------------
# URLs, marketplaces, page state
# ---------------------------------------------------------------------------
def test_urls():
    group("pagination, categories and page kinds")
    ok = True
    ok &= check("search paginates with ?page=N",
                page_url("https://www.amazon.com/s?k=x", 3)
                == "https://www.amazon.com/s?k=x&page=3")
    # The two listing kinds use DIFFERENT parameters. Using one for the other
    # silently re-fetches page 1.
    ok &= check("a best-seller grid paginates with ?pg=N, not ?page=N",
                page_url("https://www.amazon.com/zgbs/electronics/", 3)
                == "https://www.amazon.com/zgbs/electronics/?pg=3")
    ok &= check("existing filters are preserved",
                "rh=n%3A123" in page_url("https://www.amazon.com/s?k=x&rh=n%3A123", 2))
    ok &= check("an existing page parameter is REPLACED, not appended twice",
                page_url("https://www.amazon.com/s?k=x&page=7", 2).count("page=") == 1)
    ok &= check("an existing pg parameter is replaced on a grid",
                page_url("https://www.amazon.com/zgbs/e/?pg=1", 4).count("pg=") == 1)

    ok &= check("page number read back from a search URL",
                page_number_from_url("https://www.amazon.com/s?k=x&page=5") == 5)
    ok &= check("page number read back from a grid URL",
                page_number_from_url("https://www.amazon.com/zgbs/e/?pg=2") == 2)
    ok &= check("no page parameter means page 1",
                page_number_from_url("https://www.amazon.com/s?k=x") == 1)

    for url, kind in (("https://www.amazon.com/s?k=x", "search"),
                      ("https://www.amazon.com/Best-Sellers/zgbs/electronics/", "bestsellers"),
                      ("https://www.amazon.com/gp/bestsellers/electronics/", "bestsellers"),
                      ("https://www.amazon.com/dp/B07K5214NZ", "detail"),
                      ("https://www.amazon.com/gp/product/B07K5214NZ", "detail"),
                      ("https://www.amazon.com/", "other")):
        ok &= check("listing_kind(%s) == %s" % (url[24:] or "/", kind),
                    listing_kind(url) == kind)

    ok &= check("category from a search URL is the query term",
                category_from_url("https://www.amazon.com/s?k=wireless+headphones")
                == "wireless headphones")
    ok &= check("category from a grid URL is the department slug",
                category_from_url("https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/")
                == "electronics")
    ok &= check("a node id in the path is not a category",
                category_from_url("https://www.amazon.co.jp/zgbs/electronics/2151981051/")
                == "electronics")
    # Returning the ASIN made every detail row's category its own id.
    ok &= check("a /dp/ URL has no category (the ASIN is not one)",
                category_from_url("https://www.amazon.com/dp/B07K5214NZ") is None)
    return ok


def test_marketplaces():
    group("marketplaces")
    ok = True
    ok &= check("21 marketplaces registered", len(MARKETPLACES) == 21)
    ok &= check("www. stripped so one marketplace is one value",
                marketplace_host("https://www.amazon.co.uk/s?k=x") == "amazon.co.uk")
    ok &= check("smile. stripped too",
                marketplace_host("https://smile.amazon.com/s?k=x") == "amazon.com")
    ok &= check("marketplace currency available for symbol disambiguation",
                marketplace_info("https://www.amazon.se/s?k=x")["currency"] == "SEK")
    # Amazon opens marketplaces faster than a table gets updated.
    ok &= check("an unknown amazon host still parses, just without the "
                "currency hint",
                marketplace_host("https://www.amazon.co.za/s?k=x") == "amazon.co.za"
                and marketplace_info("https://www.amazon.co.za/s?k=x") == {})
    ok &= check("every registry entry has country, currency and name",
                all({"country", "currency", "name"} <= set(v)
                    for v in MARKETPLACES.values()))
    return ok


def test_page_state():
    group("page-state triage (four answers, four different responses)")
    ok = True
    # The AWS WAF interstitial clears ITSELF in a browser. Reporting it as a
    # block would report a block that does not exist; sending it to a solver
    # would bill for a challenge no solver can answer.
    ok &= check("AWS WAF challenge page detected as waf_challenge",
                detect_page_state(FIX_WAF_CHALLENGE_PAGE) == "waf_challenge")
    ok &= check("the WAF challenge is NOT reported as a block",
                detect_bot_challenge(FIX_WAF_CHALLENGE_PAGE) is None)
    ok &= check("Amazon's 503 page detected as throttled",
                detect_page_state(FIX_THROTTLE_PAGE_DE) == "throttled")
    ok &= check("a bare 503 status with no marker is still throttled",
                detect_page_state("<html>nothing</html>", status=503) == "throttled")
    ok &= check("the image captcha page detected as captcha",
                detect_page_state(CAPTCHA_PAGE) == "captcha")
    ok &= check("the image captcha IS reported as a block (exit 3 territory)",
                detect_bot_challenge(CAPTCHA_PAGE) == "amazon-captcha")
    # Decided from the URL: the real signin page contains "/ap/signin" zero
    # times, while every ordinary Amazon page links to it from the header.
    ok &= check("a signin redirect is read from the URL",
                detect_page_state("<html>irrelevant</html>",
                                  url="https://www.amazon.com/ap/signin?openid.x=1")
                == "signin")
    ok &= check("a healthy search page is 'ok'",
                detect_page_state(page(FIX_TILE_SPONSORED_COUPON),
                                  url="https://www.amazon.com/s?k=x") == "ok")
    # This is the regression: a live run reported "needs sign-in" for every
    # best-seller grid, because the header's signin link was being matched.
    ok &= check("a page merely LINKING to /ap/signin is not a signin page",
                detect_page_state('<html><a href="/ap/signin">Sign in</a>'
                                  '<div id="p13n-asin-index-0"></div></html>',
                                  url="https://www.amazon.com/zgbs/electronics/")
                == "ok")
    return ok


# ---------------------------------------------------------------------------
# The one fixture that is NOT a capture
# ---------------------------------------------------------------------------
# Amazon's image captcha, written from its documented markup rather than
# captured: no run during development was served one. 47 navigations across
# five marketplaces produced AWS WAF challenges (which clear themselves) and
# 503 throttles, and no image captcha at all. Said out loud here because the
# rest of this file's fixtures are real, and a reader is entitled to know
# which one is not — the solve path it exercises is therefore unproven
# against the live site, and TROUBLESHOOTING.md says so too.
CAPTCHA_PAGE = """<html><head><title>Amazon.com</title></head><body>
<form method="get" action="/errors/validateCaptcha" name="">
  <input type="hidden" name="amzn" value="AbCdEf+/123=">
  <input type="hidden" name="amzn-r" value="/s?k=wireless+headphones">
  <div class="a-box-inner">
    <h4>Enter the characters you see below</h4>
    <p>Sorry, we just need to make sure you're not a robot.</p>
    <img src="https://images-na.ssl-images-amazon.com/captcha/xkfytqvr/Captcha_qhlmauzbfe.jpg">
    <input type="text" name="field-keywords" id="captchacharacters">
  </div>
  <button type="submit">Continue shopping</button>
</form></body></html>"""


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("output contract")
    ok = True
    # The first sixteen columns are the family's, in the family's order, so a
    # consumer written against another repo in this family still reads them.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source"]
    names = [f.name for f in fields(Product)]
    ok &= check("Product keeps the family's 16 columns first, in order",
                names[:16] == family_prefix)
    ok &= check("Amazon's own columns come after them",
                names[16:] == ["page", "position", "sponsored", "badge", "coupon",
                               "seller", "availability", "bullets", "images",
                               "variations"])
    # A column that is null on every row of every run is worse than a missing
    # one; Amazon rendered no Prime marker on any captured tile.
    ok &= check("there is no 'prime' column", "prime" not in names)
    ok &= check("Review is its own schema, keyed on sku like the others",
                [f.name for f in fields(Review)][:5]
                == ["source", "scraped_at", "url", "sku", "review_id"])
    ok &= check("currency has no default", Product().currency is None)
    ok &= check("modes map to row classes",
                ROW_CLASS_BY_MODE == {"listing": Product, "product": Product,
                                      "reviews": Review})
    ok &= check("exit codes are the family's: 3 blocked, 4 empty, 6 partial",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("single_page_mode counts as a complete run",
                "single_page_mode" in COMPLETE_STOP_REASONS)
    return ok


def test_writers():
    group("writers, dedupe and exit codes")
    ok = True
    tmp = tempfile.mkdtemp()

    # A run that finds nothing must not replace last night's good output.
    out = os.path.join(tmp, "empty")
    rc = save([], out, "both")
    ok &= check("0 rows: nothing written, exit 4",
                rc == EXIT_NO_PRODUCTS
                and not os.path.exists(out + ".json")
                and not os.path.exists(out + ".csv"))
    rc = save([], out, "both", allow_empty=True)
    ok &= check("--allow-empty writes the files, and the code is still 4",
                rc == EXIT_NO_PRODUCTS and os.path.exists(out + ".json"))
    # A zero-byte CSV makes a consumer fail on read instead of reading a
    # valid table with no rows.
    header = open(out + ".csv", encoding="utf-8").read().strip()
    ok &= check("an empty CSV still carries its header",
                header.startswith("source,scraped_at,url,sku,title"))

    reviews_out = os.path.join(tmp, "empty_reviews")
    write_csv([], reviews_out + ".csv", row_cls=Review)
    ok &= check("an empty reviews CSV carries REVIEWS columns, not product ones",
                open(reviews_out + ".csv", encoding="utf-8").read().startswith(
                    "source,scraped_at,url,sku,review_id"))

    # CSV cannot hold a list; repr() of one is neither readable nor parseable.
    listed = os.path.join(tmp, "listed")
    save([Product(sku="A", bullets=["one", "two"])], listed, "csv")
    row = open(listed + ".csv", encoding="utf-8").read().splitlines()[1]
    ok &= check("a list column is joined with '%s' in CSV, not repr()ed"
                % LIST_CSV_SEPARATOR.strip(),
                "one | two" in row and "['one'" not in row)

    seen = set()
    dupes = [Product(sku="A"), Product(sku="A"), Product(sku="B"), Product(sku=None)]
    kept = dedupe_by_sku(dupes, seen)
    ok &= check("dedupe drops a repeated sku and KEEPS a row with no sku "
                "(nothing to key on is not a duplicate)", len(kept) == 3)
    seen = set()
    revs = [Review(sku="A", review_id="r1"), Review(sku="A", review_id="r1"),
            Review(sku="A", review_id="r2")]
    ok &= check("reviews dedupe on review_id, so a dozen reviews of one ASIN "
                "all survive", len(dedupe_by_key(revs, seen, "review_id")) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u2", 5, mode="reviews",
                    source="amazon.de")
    ok &= check("the sidecar records mode and source — neither is implied by "
                "the repo", meta["mode"] == "reviews" and meta["source"] == "amazon.de")
    ok &= check("the sidecar names WHICH pages failed, not just how many",
                "pages_failed" in meta)

    # status/exit mapping, shared so the three engines cannot drift
    cases = [
        (dict(blocked=True, stop_reason="blocked_amazon-captcha", rows=[]), EXIT_BLOCKED, None),
        (dict(blocked=False, stop_reason="completed", rows=[]), EXIT_NO_PRODUCTS, None),
        (dict(blocked=False, stop_reason="completed", rows=[Product(sku="A")]), 0, "complete"),
        (dict(blocked=False, stop_reason="no_new_products", rows=[Product(sku="A")]), 0, "complete"),
        (dict(blocked=False, stop_reason="single_page_mode", rows=[Product(sku="A")]), 0, "complete"),
        (dict(blocked=False, stop_reason="page_load_timeout", rows=[Product(sku="A")]), EXIT_PARTIAL, "partial"),
    ]
    for i, (kw, expected_rc, expected_status) in enumerate(cases):
        prefix = os.path.join(tmp, "run%d" % i)
        rc = finish_run(kw["rows"], prefix, "json", False, blocked=kw["blocked"],
                        stop_reason=kw["stop_reason"], pages_requested=1,
                        pages_completed=1, start_url="u", final_url="u")
        got_status = None
        if os.path.exists(prefix + ".meta.json"):
            got_status = json.load(open(prefix + ".meta.json"))["status"]
        ok &= check("finish_run(%s, %d row) -> exit %d, status %s"
                    % (kw["stop_reason"], len(kw["rows"]), expected_rc, expected_status),
                    rc == expected_rc and got_status == expected_status)
    # A "failed" sidecar beside the previous run's still-good output would
    # contradict it, and diff_runs would refuse data that is fine.
    ok &= check("a failed run writes NO sidecar",
                not os.path.exists(os.path.join(tmp, "run0.meta.json")))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    old = [{"sku": "A", "title": "a", "price": 25.80, "original_price": 34.40,
            "discount_pct": 25.0, "currency": "EUR", "in_stock": None,
            "price_source": "offscreen"},
           {"sku": "B", "title": "b", "price": 100.0, "currency": "EUR",
            "price_source": "offscreen"},
           {"sku": "C", "title": "c", "price": 5.0, "currency": "EUR",
            "price_source": "offscreen"}]
    new = [{"sku": "A", "title": "a", "price": 25.81, "original_price": 34.42,
            "discount_pct": 25.0, "currency": "EUR", "in_stock": None,
            "price_source": "offscreen"},
           {"sku": "B", "title": "b", "price": 89.0, "currency": "EUR",
            "price_source": "offscreen"},
           {"sku": "D", "title": "d", "price": 7.0, "currency": "EUR",
            "price_source": "offscreen"}]

    r = diff_products(old, new)
    ok &= check("added/removed keyed on sku",
                [p["sku"] for p in r["added"]] == ["D"]
                and [p["sku"] for p in r["removed"]] == ["C"])
    ok &= check("with the default tolerance of 0, every cent is reported",
                len(r["changed"]) == 2 and not r["within_tolerance"])

    # Two runs of the same command ten minutes apart disagreed on 10 of 43
    # ASINs, all by 0.039-0.058%: Amazon converts the price for a
    # cross-border visitor and the exchange rate ticks.
    r = diff_products(old, new, price_tolerance_pct=0.1)
    ok &= check("a 0.04% move inside --price-tolerance-pct is an FX tick, not "
                "a change", [c["sku"] for c in r["within_tolerance"]] == ["A"])
    ok &= check("an 11% cut is still a change at that tolerance",
                [c["sku"] for c in r["changed"]] == ["B"])

    # A currency change of any size is a real change.
    r = diff_products(
        [{"sku": "A", "price": 5.0, "currency": "EUR", "price_source": "offscreen"}],
        [{"sku": "A", "price": 5.0, "currency": "USD", "price_source": "offscreen"}],
        price_tolerance_pct=99.0)
    ok &= check("a currency change is never swallowed by the tolerance",
                len(r["changed"]) == 1)

    # A differing price_source means our two snapshots read different nodes.
    r = diff_products(
        [{"sku": "A", "price": 5.0, "currency": "EUR", "price_source": "offscreen"}],
        [{"sku": "A", "price": 6.0, "currency": "EUR", "price_source": "split"}])
    ok &= check("a price change with a price_source change is reported as "
                "source_changed, not changed",
                len(r["source_changed"]) == 1 and not r["changed"])
    return ok


def test_captcha():
    group("captcha detection")
    ok = True
    c = detect_amazon_captcha(CAPTCHA_PAGE, "https://www.amazon.com/s?k=x")
    ok &= check("Amazon's image captcha detected", c is not None)
    ok &= check("the captcha image URL is captured",
                c.image_url.endswith("Captcha_qhlmauzbfe.jpg"))
    # Both hidden fields must be echoed back: dropping amzn-r lands the
    # browser on the home page having spent a solve.
    ok &= check("both hidden form fields captured",
                c.amzn == "AbCdEf+/123=" and c.amzn_r == "/s?k=wireless+headphones")
    submit = amazon_captcha_submit_url(c, "HELLO")
    ok &= check("the solution is submitted as a GET with all three parameters",
                "field-keywords=HELLO" in submit and "amzn=" in submit
                and "amzn-r=" in submit
                and submit.startswith("https://www.amazon.com/errors/validateCaptcha"))
    # "/errors/validateCaptcha" appears in Amazon's own error-handling
    # JavaScript on ordinary pages, so a marker alone must not fire — under
    # --solve-captcha always that would buy a solve per page.
    ok &= check("a marker with no captcha image is NOT a captcha",
                detect_amazon_captcha(
                    '<html><script>var u="/errors/validateCaptcha";</script></html>',
                    "https://www.amazon.com/s?k=x") is None)
    ok &= check("a healthy listing page is not a captcha",
                detect_amazon_captcha(page(FIX_TILE_SPONSORED_COUPON),
                                      "https://www.amazon.com/s?k=x") is None)
    # reCAPTCHA detection stays in place: Amazon uses it on account flows,
    # and the family's rule is that detection stays broad.
    ok &= check("the reCAPTCHA v3 detector still fires on an inline "
                "grecaptcha.execute call",
                detect_recaptcha_v3(
                    "<html><script>grecaptcha.execute("
                    "'6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI', "
                    "{action: 'verify'});</script></html>",
                    "https://www.amazon.com/ap/signin") is not None)
    # A pinned LIMITATION rather than a half-guard: a bare g-recaptcha
    # container with the config supplied in JavaScript is invisible to the
    # static detector, which is precisely why the runtime detector exists and
    # why both are run and reconciled. Asserting the current behaviour makes a
    # future change to it a decision rather than a surprise.
    ok &= check("a bare g-recaptcha div with no sitekey in the HTML is NOT "
                "detected statically — that is what the runtime detector is for",
                detect_recaptcha_v3(
                    '<html><div id="c" class="g-recaptcha"></div></html>',
                    "https://www.amazon.com/ap/signin") is None)
    ok &= check("detect_recaptcha_in_page is present for the runtime path",
                callable(detect_recaptcha_in_page))
    ok &= check("reconcile_detections still returns None for a clean page",
                reconcile_detections(None, None) is None)
    return ok


# ---------------------------------------------------------------------------
# The shared policy
# ---------------------------------------------------------------------------
class FakeDriver:
    """A scriptable stand-in for a browser, for page_flow's algorithms.

    `counts` is what count() returns on each successive call; the last value
    repeats. Records what it was asked to do so the tests can assert the
    ORDER of operations, which is where two real bugs lived.
    """

    def __init__(self, counts, heights=None, html_sequence=None):
        self.counts = list(counts)
        self.heights = list(heights or [1000])
        self.html_sequence = list(html_sequence or [])
        self.calls = []

    def _next(self, seq):
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else 0)

    def count(self, selector):
        self.calls.append(("count", selector))
        return self._next(self.counts)

    def page_height(self):
        return self._next(self.heights)

    def scroll_to_bottom(self):
        self.calls.append(("scroll_to_bottom",))

    def scroll_into_view(self, selector):
        self.calls.append(("scroll_into_view", selector))

    def sleep(self, ms):
        self.calls.append(("sleep", ms))

    def content(self):
        return self._next(self.html_sequence) if self.html_sequence else "<html></html>"

    def current_url(self):
        return "https://www.amazon.com/s?k=x"


def test_page_flow():
    group("page_flow policy")
    ok = True
    ok &= check("a listing needs several anchors before it counts as loaded",
                page_flow.min_matches("listing") == page_flow.MIN_CARD_MATCHES
                and page_flow.MIN_CARD_MATCHES > 1)
    # A detail page has exactly one #productTitle: requiring more than one
    # would time out on every successful fetch.
    ok &= check("a detail page needs only one anchor (the threshold follows "
                "the mode, or it silently inverts)",
                page_flow.min_matches("product") == 0
                and page_flow.min_matches("reviews") == 0)
    ok &= check("the listing readiness selector is the PRIMARY anchor, not "
                "a[href*='/dp/'] (143 such links for 22 tiles)",
                "s-search-result" in page_flow.READY_SELECTOR_LISTING
                and "p13n-asin-index" in page_flow.READY_SELECTOR_LISTING
                and "/dp/" not in page_flow.READY_SELECTOR_LISTING)
    ok &= check("link[rel=next] leads the pagination selector even though "
                "Amazon serves none — the durable form comes first",
                page_flow.NEXT_PAGE_SELECTOR.startswith("link[rel='next']"))
    # Search results are server-rendered: a captured page held the same 16
    # tiles before and after scrolling to the bottom.
    ok &= check("search results are not scrolled (they do not grow)",
                page_flow.needs_scrolling("listing", "https://www.amazon.com/s?k=x") is False)
    ok &= check("best-seller grids ARE scrolled (30 -> 50 cards, measured)",
                page_flow.needs_scrolling("listing", "https://www.amazon.com/zgbs/e/") is True)
    ok &= check("detail pages are scrolled", 
                page_flow.needs_scrolling("product", "https://www.amazon.com/dp/B0000000AA") is True)
    ok &= check("a detail page gets a shorter content timeout than a listing",
                page_flow.content_timeout_ms("reviews") < page_flow.content_timeout_ms("listing"))
    # Reviews fail because of a served variant, not a transient fault, so
    # they get their own session budget rather than eating --retries.
    ok &= check("reviews get their own session budget (>=5), independent of "
                "--retries", page_flow.session_attempts("reviews", 1) >= 5
                and page_flow.session_attempts("listing", 1) == 1)
    ok &= check("scrolling is not retried for reviews — measured not to be "
                "what fills the empty variant",
                page_flow.hydrate_attempts("reviews") == 1
                and page_flow.hydrate_attempts("listing") == 3)

    # The count must hold still for several rounds: one quiet round is not
    # the end of the content, and believing it cost 20 of 50 cards.
    d = FakeDriver(counts=[30, 30, 30, 50, 50, 50, 50], heights=[1, 1, 1, 2, 2, 2, 2])
    got = page_flow.scroll_until_stable(d.count, d.page_height, d.scroll_to_bottom,
                                        d.sleep, "sel", stable_rounds=3)
    ok &= check("scroll_until_stable waits out a plateau and returns the "
                "final count (50, not 30)", got == 50)
    ok &= check("it scrolls to the document end rather than wheeling a fixed "
                "distance", ("scroll_to_bottom",) in d.calls)
    d = FakeDriver(counts=[7], heights=[1])
    ok &= check("a page that never grows stops after stable_rounds",
                page_flow.scroll_until_stable(d.count, d.page_height,
                                              d.scroll_to_bottom, d.sleep, "sel") == 7)
    d = FakeDriver(counts=list(range(1, 200)), heights=list(range(1, 200)))
    ok &= check("a page that grows forever is bounded by max_rounds",
                page_flow.scroll_until_stable(d.count, d.page_height,
                                              d.scroll_to_bottom, d.sleep, "sel",
                                              max_rounds=4) is not None)

    # The review widget hydrates from an observer on its own element, so the
    # anchor is scrolled into view before the bottom-ward pass.
    d = FakeDriver(counts=[0, 0, 13, 13, 13, 13])
    page_flow.hydrate(d.count, d.page_height, d.scroll_to_bottom, d.sleep,
                      d.scroll_into_view, "reviews", "sel", 0, attempts=2)
    scrolled_anchor = [c for c in d.calls if c[0] == "scroll_into_view"]
    ok &= check("hydrate scrolls the mode's own anchor into view first",
                scrolled_anchor
                and "reviewsMedley" in scrolled_anchor[0][1])

    # The WAF challenge must be waited out, not reported.
    d = FakeDriver(counts=[0], html_sequence=[FIX_WAF_CHALLENGE_PAGE,
                                              FIX_WAF_CHALLENGE_PAGE,
                                              page(FIX_TILE_SPONSORED_COUPON)])
    ok &= check("wait_out_waf_challenge returns True once the real page arrives",
                page_flow.wait_out_waf_challenge(d.content, d.current_url, d.sleep) is True)
    d = FakeDriver(counts=[0], html_sequence=[FIX_WAF_CHALLENGE_PAGE])
    ok &= check("a challenge that never clears gives up (bounded) and says so",
                page_flow.wait_out_waf_challenge(d.content, d.current_url, d.sleep) is False)
    return ok


# ---------------------------------------------------------------------------
# Configuration and credentials
# ---------------------------------------------------------------------------
class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_env_config():
    group("configuration")
    ok = True
    ok &= check("ENV_KEYS names the Amazon variables",
                set(env_config.ENV_KEYS) == {"TWOCAPTCHA_KEY", "AMAZON_CDP_ENDPOINT",
                                             "AMAZON_PROXY", "AMAZON_URL"})
    # A variable mapped onto a flag with a non-empty default would be
    # silently inert: a setting that looks configurable and is not.
    ok &= check("no variable maps onto --out, which has a non-empty default",
                "out" not in env_config.ENV_KEYS.values())

    # .env.example must document exactly what the code reads, in both
    # directions — it drifts otherwise.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    with open(example, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # An explicit flag always wins over the environment.
    saved = os.environ.get("AMAZON_URL")
    os.environ["AMAZON_URL"] = "https://www.amazon.com/s?k=from-env"
    try:
        args = _Args(url="https://www.amazon.com/s?k=from-flag", twocaptcha_key=None,
                     cdp_endpoint=None, proxy=None)
        env_config.apply(args)
        ok &= check("an explicit flag beats an environment variable",
                    args.url == "https://www.amazon.com/s?k=from-flag")
        args = _Args(url=None, twocaptcha_key=None, cdp_endpoint=None, proxy=None)
        env_config.apply(args)
        ok &= check("an unset flag is filled from the environment",
                    args.url == "https://www.amazon.com/s?k=from-env")
    finally:
        if saved is None:
            del os.environ["AMAZON_URL"]
        else:
            os.environ["AMAZON_URL"] = saved
    return ok


def test_proxy_pool():
    group("proxies and credentials")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    # Host and port are the point of a rotation log and are not the secret.
    ok &= check("mask() hides credentials and KEEPS host and port",
                mask(url) == "http://***:***@eu.proxy.2captcha.com:2334")
    ok &= check("the password never appears in a masked URL", "secret" not in mask(url))
    # --proxy-server= becomes part of the browser's argv, readable by
    # anything that can run `ps`.
    pw = to_playwright(url)
    ok &= check("Playwright gets credentials in their own fields, not in server",
                pw["server"] == "http://eu.proxy.2captcha.com:2334"
                and pw["username"] == "user" and pw["password"] == "secret"
                and "secret" not in pw["server"])
    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials keeps the address argv-safe",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))
    ok &= check("a proxy with no credentials yields None for them",
                split_credentials("http://host:1234")[1] is None)

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-page")
    first = pool.current
    pool.advance("test")
    ok &= check("advance() moves to a different exit", pool.current != first)
    ok &= check("per-page rotation is reported by rotates_per_page()",
                pool.rotates_per_page() is True)
    ok &= check("per-run rotation is not",
                ProxyPool(["http://a:1"], rotate="per-run").rotates_per_page() is False)
    ok &= check("a comment line in a proxy file is skipped",
                parse_proxy_line("# a comment") is None)
    return ok


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


def test_engines(skips):
    group("engines (all three must agree)")
    ok = True
    loaded = {}
    for name in ENGINES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Visible, and CI's engine-smoke job fails on it: "skipped,
            # engine absent" reads identically to a real import error.
            skips.append("%s (%s)" % (name, e))
            print("  SKIP  %s could not be imported: %s" % (name, e))

    if not loaded:
        return ok

    # The public entry point, not only the helpers underneath it: a signature
    # once drifted from its callers while every check exercised the privates.
    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    callable(getattr(mod, "scrape", None))
                    and callable(getattr(mod, "parse_args", None)))
        ok &= check("%s builds its rows through the shared finish_run()" % name,
                    "finish_run" in inspect.getsource(mod.scrape))
        ok &= check("%s takes its page policy from page_flow, not its own copy"
                    % name, "page_flow" in inspect.getsource(mod))
        source = inspect.getsource(mod)
        # The AWS WAF interstitial must never reach the captcha solver.
        ok &= check("%s waits out the WAF challenge instead of solving it" % name,
                    "wait_out_waf_challenge" in source)
        ok &= check("%s retries Amazon's 503 throttle page" % name,
                    "throttled" in source)

    # THE GUARD FOR THE GUARD. Everything above only means something if
    # importing an engine module genuinely requires its driver library: the
    # suite reports a skip when the import fails, and CI's engine-smoke job
    # fails on any reported skip. This engine's pyppeteer import once sat
    # inside the launch path instead, so the module imported cleanly with no
    # pyppeteer installed, the group never skipped, and CI happily ran against
    # a stub version (pyppeteer 0.0.25, resolved from an unpinned install)
    # without noticing. Checked on the SOURCE rather than by importing,
    # because by the time this runs the library is already loaded.
    DRIVER_MODULES = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, mod in loaded.items():
        driver = DRIVER_MODULES[name]
        tree = ast.parse(inspect.getsource(mod))
        top_level = any(
            (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == driver)
            or (isinstance(node, ast.Import)
                and any(a.name.split(".")[0] == driver for a in node.names))
            for node in tree.body)
        ok &= check("%s imports %s at module level, so a missing driver is a "
                    "reported skip rather than a silently vacuous check"
                    % (name, driver), top_level)

    # Flag parity: the family's contract is that the three CLIs take the same
    # flags, so a script can switch engines without rewriting its arguments.
    flag_sets = {}
    for name, mod in loaded.items():
        # parse_args() reads sys.argv, so the flags are read out of the
        # source rather than by building the parser.
        flag_sets[name] = set(re.findall(r'add_argument\("(--[a-z-]+)"',
                                         inspect.getsource(mod.parse_args)))
    if len(flag_sets) > 1:
        shared = set.intersection(*flag_sets.values())
        family_flags = {"--url", "--mode", "--category", "--pages", "--format",
                        "--out", "--delay", "--retries", "--retry-delay",
                        "--concurrency", "--proxy", "--proxy-file",
                        "--proxy-rotate", "--proxy-shuffle",
                        "--proxy-block-retries", "--twocaptcha-key",
                        "--captcha-api", "--solve-captcha", "--min-score",
                        "--cdp-endpoint", "--allow-empty", "--dump-html",
                        "--headless", "--headful"}
        missing = family_flags - shared
        ok &= check("every engine takes the family's flags (missing: %s)"
                    % (sorted(missing) or "none"), not missing)

    # A removed feature stays removed. Cheap, and it catches an editor
    # reintroducing either.
    for name, mod in loaded.items():
        source = inspect.getsource(mod)
        for gone in ("--antidetect", "ANTIDETECT_LOCAL_API"):
            ok &= check("%s does not reintroduce %s" % (name, gone),
                        gone not in source)
    return ok


# ---------------------------------------------------------------------------
# Wording and shipped samples
# ---------------------------------------------------------------------------
# Enforced because the naming predates this repo and drifts back easily. The
# right name for the product is the Scraping Browser API; gate.2prx.com is
# not a host; the antidetect flag was removed because its endpoint was a
# placeholder.
BANNED_PHRASES = ("cloud browser", "antidetect browser", "Antidetect Browser",
                  "gate.2prx.com", "--antidetect", "ANTIDETECT_LOCAL_API",
                  "2parser")

SHIPPED_TEXT_FILES = ("README.md", "TROUBLESHOOTING.md", "CONTRIBUTING.md",
                      "SECURITY.md", "CHANGELOG.md", ".env.example",
                      "CLAUDE.md")


# Session material and personal data must not come back with the next capture.
# The fixtures here are real page dumps, and a real page dump carries the
# session that fetched it plus, on a review, a real person's name and words.
# Both were committed once and scrubbed; these patterns keep that from being a
# one-off cleanup. Written as patterns rather than literals so a NEW capture's
# values are caught too, which a list of the old values would not do.
CAPTURE_LEAK_PATTERNS = (
    (r'"sessionId":"(?!000-0000000-0000000)[\d-]{10,}"', "real Amazon session ids"),
    (r'value="(?!REDACTED)[A-Za-z0-9+/=]{40,}"[^>]*name="anti-csrftoken',
     "real CSRF tokens"),
    (r'"csrfToken":"(?!REDACTED)[^"]{20,}"', "real CSRF tokens"),
    (r'amzn1\.account\.(?!EXAMPLEACCOUNTIDEXAMPLE)[A-Z0-9]{10,}',
     "real customer account ids"),
)


def test_no_capture_leaks():
    group("no session material or personal data in the fixtures")
    ok = True
    text = open(os.path.join(REPO_ROOT, "smoke_test.py"),
                encoding="utf-8").read()
    # Skip this file's own pattern definitions, which necessarily contain the
    # shapes they match.
    text = text.split("CAPTURE_LEAK_PATTERNS = (", 1)[0] + \
        text.split("def test_no_capture_leaks", 1)[-1]
    for pattern, what in CAPTURE_LEAK_PATTERNS:
        hits = re.findall(pattern, text)
        ok &= check("fixtures carry no %s (%d hit(s))" % (what, len(hits)),
                    not hits)
    # The anonymised review must stay anonymised.
    ok &= check("the review fixture's author is the placeholder, not a person",
                'class="a-profile-name">Anonymised Reviewer<' in FIX_REVIEW_CONTAINER)
    return ok


def test_wording():
    group("wording")
    ok = True
    checked = 0
    for name in os.listdir(REPO_ROOT):
        if not (name.endswith(".py") or name in SHIPPED_TEXT_FILES):
            continue
        if name == "smoke_test.py":
            continue  # this file names the banned phrases in order to ban them
        path = os.path.join(REPO_ROOT, name)
        if not os.path.isfile(path):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        checked += 1
        for phrase in BANNED_PHRASES:
            if phrase in text:
                ok &= check("%s must not contain %r" % (name, phrase), False)
    ok &= check("banned-phrase scan ran over %d shipped files" % checked, checked > 5)

    # Positive check: the README must name the product correctly at least once.
    readme = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    ok &= check("README names the Scraping Browser API",
                "Scraping Browser API" in readme)
    ok &= check("README does not integrate a competitor solver",
                not re.search(r"anti-?captcha|capmonster|deathbycaptcha",
                              readme, re.IGNORECASE))
    return ok


def test_sample_output():
    group("shipped sample output")
    ok = True
    sample_json = os.path.join(REPO_ROOT, "sample_output.json")
    sample_csv = os.path.join(REPO_ROOT, "sample_output.csv")
    if not (os.path.exists(sample_json) and os.path.exists(sample_csv)):
        ok &= check("sample_output.json and sample_output.csv are shipped", False)
        return ok

    rows = json.load(open(sample_json, encoding="utf-8"))
    ok &= check("sample_output.json is a non-empty list of rows",
                isinstance(rows, list) and rows)
    expected = [f.name for f in fields(Product)]
    ok &= check("sample_output.json columns match the Product schema exactly",
                all(list(r.keys()) == expected for r in rows))
    header = open(sample_csv, encoding="utf-8").read().splitlines()[0]
    ok &= check("sample_output.csv header matches the Product schema",
                header.split(",") == expected)
    # A sample cut from a real run, not typed by hand.
    text = json.dumps(rows)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    ok &= check("every sample row has a real 10-character ASIN",
                all(re.fullmatch(r"[A-Z0-9]{10}", r.get("sku") or "") for r in rows))
    ok &= check("the sample says which marketplace it came from",
                all((r.get("source") or "").startswith("amazon.") for r in rows))
    return ok


# ---------------------------------------------------------------------------
def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_price_parsing()
    ok &= test_search_tiles()
    ok &= test_localised_tiles()
    ok &= test_bestseller_cards()
    ok &= test_url_fallback_and_tile_scope()
    ok &= test_product_detail()
    ok &= test_reviews()
    ok &= test_urls()
    ok &= test_marketplaces()
    ok &= test_page_state()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_page_flow()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_sample_output()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
