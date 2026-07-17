"""
Archer price checker — multi-engine Cloudflare-bypass version.

Per-listing waterfall — each engine tried only if the previous one was blocked
or found no price. Seven fundamentally different bypass strategies:

  0. curl-cffi      — TLS + HTTP/2 fingerprint impersonation via libcurl. No
                       browser process. Bypasses CF checks that only inspect the
                       handshake. Also extracts JSON-LD from initial HTML.

  1. Patchright      — patched Chromium that removes automation traces at the
                       binary level. Handles most CF / PerimeterX / DataDome.

  2. nodriver        — pure Chrome DevTools Protocol client; zero Playwright /
                       Selenium code. Completely different detection profile from
                       Patchright; uses system Chrome (pre-installed on GitHub
                       Actions runners).

  3. Camoufox        — patched, anti-detect Firefox. Different engine entirely —
                       catches sites that block all Chromium variants.

  4. FlareSolverr    — dedicated CF Turnstile/Interstitial solver (Docker
                       sidecar). Returns cf_clearance cookies injected into a
                       fresh Patchright session for full JS rendering.

  5. ScraperAPI      — residential proxy API with a free tier (1000 credits /
                       month, no credit card). Only active when SCRAPERAPI_KEY
                       is set. Handles datacenter-IP-blocked sites.

  6. ZenRows         — residential proxy API (~$49/month). Only active when
                       ZENROWS_API_KEY is set. Final paid escalation path.

Same dynamic sheet design: any sheet with Price + Listing URL headers is
auto-detected and checked. Add parts or trucks by pasting a URL — nothing
here changes.
"""

import json
import os
import re
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qs

import gspread
from google.oauth2.service_account import Credentials

# ── Optional engine imports — each degrades gracefully if not installed ────────

try:
    from curl_cffi import requests as _cffi
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

try:
    from patchright.sync_api import sync_playwright as _pw
    _HAS_PATCHRIGHT = True
except ImportError:
    _HAS_PATCHRIGHT = False

try:
    from camoufox.sync_api import Camoufox
    _HAS_CAMOUFOX = True
except ImportError:
    _HAS_CAMOUFOX = False

try:
    import nodriver as _nodriver
    import asyncio as _asyncio
    _HAS_NODRIVER = True
except ImportError:
    _HAS_NODRIVER = False

try:
    import requests as _req
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ── Constants ──────────────────────────────────────────────────────────────────

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
FLARESOLVERR_URL = os.environ.get('FLARESOLVERR_URL', 'http://localhost:8191/v1')
SCRAPERAPI_KEY   = os.environ.get('SCRAPERAPI_KEY', '')
ZENROWS_API_KEY  = os.environ.get('ZENROWS_API_KEY', '')

# Multi-word phrases tied specifically to the vehicle/listing being gone.
# Deliberately excludes generic single words like "sold" or "unavailable"
# that appear on active listings all the time (e.g. "500+ sold this week").
SOLD_PHRASES = [
    'this vehicle has been sold', 'vehicle has sold', 'this vehicle is sold',
    'vehicle is no longer available', 'vehicle unavailable',
    'this vehicle is no longer', 'vehicle not found',
    'listing has ended', 'listing is no longer active', 'this listing has expired',
]

# Known bot-protection challenge page titles (lowercase, exact)
_BLOCK_TITLES = {'just a moment...', 'just a moment', 'attention required!', 'one more step'}

# Price regex patterns — tried in order of specificity
_PRICE_PATTERNS = [
    r'\$([\d]{2,3},\d{3})\.\d{2}',   # $10,999.00
    r'\$([\d]{2,3},\d{3})(?!\d)',     # $10,999
    r'\$([\d]{1,4}\.\d{2})(?!\d)',    # $75.00  (small parts)
    r'\$([\d]{4,6})(?!\d)',           # $10999  (no comma)
]

# Redirect path fragments that indicate a listing was removed
_SOLD_REDIRECT_MARKERS = ['/search', '/results', '/inventory?', '/not-found', '/404', '/error']

# Safety cap: never follow more than this many pagination pages for a single catalog URL
MAX_CATALOG_PAGES = 20

# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    price:   Optional[float]
    sold:    Optional[str]   # reason string if listing gone, None if available
    debug:   str             # snippet for the "not found" cell note
    engine:  str             # which engine produced this result
    blocked: bool = False    # True = bot-protection fired (worth escalating to next engine)


# ── Google Sheets ──────────────────────────────────────────────────────────────

def connect_to_sheet():
    creds_dict = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ['SPREADSHEET_ID'])


def find_columns(sheet):
    """Scan the first 15 rows for a header row containing Price + Listing URL.
    Returns column map or None if this sheet has no such headers."""
    rows = sheet.get_values('A1:O15')
    for r_idx, row in enumerate(rows, start=1):
        price_col = url_col = checked_col = status_col = None
        for c_idx, val in enumerate(row, start=1):
            v = val.lower()
            if 'price' in v and 'total' not in v:
                price_col = c_idx
            if 'url' in v:
                url_col = c_idx
            if 'last checked' in v:
                checked_col = c_idx
            if v.strip() == 'status':
                status_col = c_idx
        if price_col and url_col:
            return {'header_row': r_idx, 'price_col': price_col,
                    'url_col': url_col, 'checked_col': checked_col,
                    'status_col': status_col}
    return None


# ── Shared page helpers (work with any Playwright-compatible engine) ───────────

def _is_blocked(page) -> bool:
    """True when a bot-protection challenge page is showing instead of real content."""
    try:
        return page.title().strip().lower() in _BLOCK_TITLES
    except Exception:
        return False


def _dismiss_popups(page):
    """Click cookie banners and promo overlays that can obscure price content."""
    for text in ['Accept All', 'Accept Cookies', 'Accept', 'I Agree', 'Agree',
                 'Got it', 'OK', 'Close', 'No Thanks', 'Continue', 'Dismiss']:
        try:
            btn = page.get_by_role('button', name=text, exact=False).first
            if btn.count() > 0 and btn.is_visible(timeout=800):
                btn.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            continue
    for sel in ['[aria-label="Close"]', '[aria-label="close"]', 'button.close', '.modal-close']:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=800):
                btn.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            continue


def _human_scroll(page):
    """Scroll in short increments with random timing — triggers lazy-load widgets
    and looks less like an automated sweep to behavior-analysis systems."""
    try:
        for delta in [300, 400, 350]:
            page.mouse.wheel(0, delta)
            page.wait_for_timeout(random.randint(280, 650))
    except Exception:
        pass


def _all_texts(page) -> list:
    """Body text from the main frame plus every iframe — needed because pricing
    widgets (TradePending, DealerSocket, etc.) often render inside iframes that
    are invisible to the main frame's inner_text()."""
    texts = []
    try:
        texts.append(page.locator('body').inner_text())
    except Exception:
        pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            texts.append(frame.locator('body').inner_text())
        except Exception:
            continue
    return texts


def _extract_price_from_page(page) -> Optional[float]:
    """Five-strategy extraction: subtotal label → JSON-LD → meta tags → rendered text → raw HTML."""

    # Strategy 0: cart/checkout subtotal label — search rendered text for
    # "subtotal" (or "sub total / sub-total") immediately before a price.
    # This runs before JSON-LD so a cart page returns the order subtotal,
    # not an individual line-item price.
    try:
        for text in _all_texts(page):
            m = re.search(
                r'sub[-\s]?total[^$\n]{0,40}\$([\d,]+(?:\.\d{2})?)',
                text, re.IGNORECASE)
            if m:
                return float(m.group(1).replace(',', ''))
    except Exception:
        pass

    # Strategy 1: JSON-LD structured data (most reliable; many listing sites
    # embed this specifically for search-engine indexing)
    for frame in page.frames:
        try:
            for s in frame.locator('script[type="application/ld+json"]').all_text_contents():
                try:
                    data = json.loads(s)
                    for item in (data if isinstance(data, list) else [data]):
                        if not isinstance(item, dict):
                            continue
                        offers = item.get('offers', {})
                        price = offers.get('price') if isinstance(offers, dict) else None
                        if price:
                            return float(str(price).replace(',', ''))
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue
        except Exception:
            continue

    # Strategy 2: itemprop / Open Graph price meta tags
    for frame in page.frames:
        for sel in ['meta[itemprop="price"]', 'meta[property="product:price:amount"]']:
            try:
                el = frame.locator(sel).first
                if el.count() > 0:
                    content = el.get_attribute('content')
                    if content:
                        return float(content.replace(',', ''))
            except Exception:
                continue

    # Strategy 3: visible rendered text — main frame + iframes.
    # This is the part that only works because the engine executed JS first.
    for text in _all_texts(page):
        for pat in _PRICE_PATTERNS:
            m = re.search(pat, text)
            if m:
                return float(m.group(1).replace(',', ''))

    # Strategy 4: raw HTML source. Catches prices in hidden / CSS-toggled
    # elements that inner_text() skips over.
    try:
        html = page.content()
        for pat in _PRICE_PATTERNS:
            m = re.search(pat, html)
            if m:
                return float(m.group(1).replace(',', ''))
    except Exception:
        pass

    return None


def _extract_price_from_html(html: str) -> Optional[float]:
    """Same patterns applied to a raw HTML string (for FlareSolverr's static response)."""
    for pat in _PRICE_PATTERNS:
        m = re.search(pat, html)
        if m:
            return float(m.group(1).replace(',', ''))
    return None


def _check_sold(page, response, url: str) -> Optional[str]:
    """Three independent sold signals. Any one firing is enough to flag the listing."""

    # Signal 1: HTTP 404 / 410
    if response and response.status in (404, 410):
        return f'SOLD/REMOVED — page returned {response.status}'

    # Signal 2: redirect to a non-listing page (homepage, search results, error)
    final_url = page.url
    final_path = final_url.split('?')[0].rstrip('/')
    try:
        origin = 'https://' + final_url.split('/')[2].rstrip('/')
        is_homepage = final_path in ('', origin)
    except Exception:
        is_homepage = False
    if is_homepage or any(m in final_path.lower() for m in _SOLD_REDIRECT_MARKERS):
        return f'SOLD/REMOVED (probably) — redirected to {final_url}'

    # Signal 3: sold-indicator phrases in rendered text
    try:
        text = page.locator('body').inner_text().lower()
        for phrase in SOLD_PHRASES:
            if phrase in text:
                return f'SOLD/REMOVED — page says "{phrase}"'
    except Exception:
        pass

    return None


def _debug_snippet(page, engine: str) -> str:
    """Grab context around the first '$' or 'price' to help diagnose why
    extraction failed — checking iframes too."""
    title = ''
    try:
        title = page.title()[:40]
    except Exception:
        pass
    for text in _all_texts(page):
        idx = text.find('$')
        if idx == -1:
            idx = text.lower().find('price')
        if idx != -1:
            snippet = text[max(0, idx - 30):idx + 60].replace('\n', ' ').strip()
            return f'[{engine}:{title}] {snippet[:80]}'
    n_frames = max(0, len(page.frames) - 1)
    return f'[{engine}:{title}] no $/"price" in main page or {n_frames} iframe(s)'


def _page_to_result(page, response, url: str, engine: str) -> CheckResult:
    """Evaluate a loaded page: check for bot block, sold status, then extract price."""
    if _is_blocked(page):
        return CheckResult(price=None, sold=None,
                           debug=f'[{engine}] blocked by bot-protection challenge page',
                           engine=engine, blocked=True)
    sold = _check_sold(page, response, url)
    if sold:
        return CheckResult(price=None, sold=sold, debug='', engine=engine)
    price = _extract_price_from_page(page)
    if price is not None:
        return CheckResult(price=price, sold=None, debug='', engine=engine)
    return CheckResult(price=None, sold=None,
                       debug=_debug_snippet(page, engine),
                       engine=engine, blocked=False)


# ── Engine 0: curl-cffi (TLS + HTTP/2 fingerprint impersonation) ─────────────

def _try_curl_cffi(url: str) -> CheckResult:
    """Impersonates Chrome/Firefox at the TLS and HTTP/2 level using libcurl.
    No browser process — very fast. Bypasses Cloudflare protections that only
    inspect the TLS handshake rather than running a full JS challenge. Also
    extracts JSON-LD from the initial HTML, which many listing sites embed for
    SEO even when the rendered price itself is JS-driven.
    Returns blocked=False (not blocked=True) when the page loads but has no
    price in static HTML — the browser engines will take over for JS rendering."""
    if not _HAS_CURL_CFFI:
        return CheckResult(price=None, sold=None,
                           debug='[curl-cffi] not installed',
                           engine='curl-cffi', blocked=False)

    for browser_type in ('chrome124', 'chrome120', 'firefox122'):
        try:
            with _cffi.Session(impersonate=browser_type) as session:
                resp = session.get(url, timeout=30, allow_redirects=True,
                                   headers={'Accept-Language': 'en-US,en;q=0.9'})

            if resp.status_code in (404, 410):
                return CheckResult(price=None,
                                   sold=f'SOLD/REMOVED — page returned {resp.status_code}',
                                   debug='', engine='curl-cffi')

            html = resp.text

            # Still showing a challenge page — try next impersonation
            title_m = re.search(r'<title[^>]*>(.*?)</title>', html[:3000],
                                 re.IGNORECASE | re.DOTALL)
            title = (title_m.group(1).strip() if title_m else '').lower()
            if resp.status_code == 403 or title in _BLOCK_TITLES:
                continue

            # JSON-LD (often present in initial HTML even on JS-heavy sites)
            for ld in re.findall(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    html, re.DOTALL | re.IGNORECASE):
                try:
                    data = json.loads(ld)
                    for item in (data if isinstance(data, list) else [data]):
                        if not isinstance(item, dict):
                            continue
                        offers = item.get('offers', {})
                        p = offers.get('price') if isinstance(offers, dict) else None
                        if p:
                            return CheckResult(price=float(str(p).replace(',', '')),
                                               sold=None, debug='', engine='curl-cffi')
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue

            # Sold phrases in static HTML
            html_lower = html.lower()
            for phrase in SOLD_PHRASES:
                if phrase in html_lower:
                    return CheckResult(price=None,
                                       sold=f'SOLD/REMOVED — page says "{phrase}"',
                                       debug='', engine='curl-cffi')

            # Regex scan on raw HTML
            price = _extract_price_from_html(html)
            if price is not None:
                return CheckResult(price=price, sold=None, debug='', engine='curl-cffi')

            # Page loaded but price needs JS — fall through to browser engines
            return CheckResult(price=None, sold=None,
                               debug=f'[curl-cffi:{browser_type}] page loaded, price needs JS render',
                               engine='curl-cffi', blocked=False)

        except Exception:
            continue

    return CheckResult(price=None, sold=None,
                       debug='[curl-cffi] all impersonations blocked',
                       engine='curl-cffi', blocked=True)


# ── Engine 1: Patchright (patched Chromium) ───────────────────────────────────

def _try_patchright(url: str, browser) -> CheckResult:
    """Fresh browser context per URL so state never leaks between listings."""
    ctx = page = None
    try:
        ctx = browser.new_context(
            user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
            viewport={'width': random.randint(1200, 1440), 'height': random.randint(700, 900)},
        )
        page = ctx.new_page()
        resp = page.goto(url, wait_until='load', timeout=45000)
        page.wait_for_timeout(random.randint(1800, 3200))
        _dismiss_popups(page)
        _human_scroll(page)
        _dismiss_popups(page)
        return _page_to_result(page, resp, url, 'patchright')
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[patchright] error: {str(e)[:100]}',
                           engine='patchright', blocked=False)
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


# ── Engine 2: nodriver (pure CDP Chrome, no Playwright/Selenium wrapper) ──────

async def _nodriver_async(url: str) -> CheckResult:
    """Async core — called via asyncio.run() from the sync waterfall."""
    browser = None
    try:
        browser = await _nodriver.start(
            headless=True,
            browser_args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        page = await browser.get(url)
        await _asyncio.sleep(random.uniform(2.0, 3.5))

        title = (await page.evaluate('document.title') or '').strip().lower()
        if title in _BLOCK_TITLES:
            return CheckResult(price=None, sold=None,
                               debug='[nodriver] Cloudflare challenge page',
                               engine='nodriver', blocked=True)

        # Dismiss common popups
        for btn_text in ['Accept All', 'Accept Cookies', 'Accept', 'I Agree',
                         'Got it', 'OK', 'Close', 'No Thanks']:
            try:
                el = await page.find(btn_text, best_match=True, timeout=1)
                if el:
                    await el.click()
                    await _asyncio.sleep(0.4)
            except Exception:
                pass

        # Scroll to trigger lazy-load price widgets
        await page.evaluate('window.scrollBy(0, 900)')
        await _asyncio.sleep(1.2)

        html = await page.get_content()

        # JSON-LD
        for ld in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE):
            try:
                data = json.loads(ld)
                for item in (data if isinstance(data, list) else [data]):
                    if not isinstance(item, dict):
                        continue
                    offers = item.get('offers', {})
                    p = offers.get('price') if isinstance(offers, dict) else None
                    if p:
                        return CheckResult(price=float(str(p).replace(',', '')),
                                           sold=None, debug='', engine='nodriver')
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue

        # Sold phrases
        html_lower = html.lower()
        for phrase in SOLD_PHRASES:
            if phrase in html_lower:
                return CheckResult(price=None,
                                   sold=f'SOLD/REMOVED — page says "{phrase}"',
                                   debug='', engine='nodriver')

        price = _extract_price_from_html(html)
        if price is not None:
            return CheckResult(price=price, sold=None, debug='', engine='nodriver')

        return CheckResult(price=None, sold=None,
                           debug=f'[nodriver:{title[:40]}] page loaded, no price found',
                           engine='nodriver', blocked=False)
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[nodriver] error: {str(e)[:100]}',
                           engine='nodriver', blocked=False)
    finally:
        if browser:
            try:
                browser.stop()
            except Exception:
                pass


def _try_nodriver(url: str) -> CheckResult:
    """nodriver uses pure async CDP — no Playwright layer, completely different
    detection profile. Runs in the main thread's own event loop (separate from
    Playwright's background thread) so there's no asyncio conflict."""
    if not _HAS_NODRIVER:
        return CheckResult(price=None, sold=None,
                           debug='[nodriver] not installed',
                           engine='nodriver', blocked=False)
    # Create the coroutine before asyncio.run() so we can close() it in the
    # except branch — otherwise Python emits RuntimeWarning: coroutine was never
    # awaited when asyncio.run() raises before the coroutine starts (e.g. when
    # Patchright's background thread already holds the event-loop lock).
    coro = _nodriver_async(url)
    try:
        return _asyncio.run(coro)
    except Exception as e:
        try:
            coro.close()
        except Exception:
            pass
        return CheckResult(price=None, sold=None,
                           debug=f'[nodriver] asyncio error: {str(e)[:100]}',
                           engine='nodriver', blocked=False)


# ── Engine 2: Camoufox (patched Firefox) ─────────────────────────────────────

def _try_camoufox(url: str) -> CheckResult:
    """Fresh Camoufox browser per URL — required by its context-manager API."""
    if not _HAS_CAMOUFOX:
        return CheckResult(price=None, sold=None,
                           debug='[camoufox] not installed',
                           engine='camoufox', blocked=False)
    page = None
    try:
        with Camoufox(headless=True) as browser:
            # no_viewport=True: Camoufox's Firefox CDP doesn't understand the
            # isMobile field Playwright sends with every viewport — skip it.
            page = browser.new_page(no_viewport=True)
            resp = page.goto(url, wait_until='load', timeout=45000)
            page.wait_for_timeout(random.randint(1800, 3200))
            _dismiss_popups(page)
            _human_scroll(page)
            _dismiss_popups(page)
            return _page_to_result(page, resp, url, 'camoufox')
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[camoufox] error: {str(e)[:100]}',
                           engine='camoufox', blocked=False)


# ── Engine 3: FlareSolverr + Patchright cookie injection ──────────────────────

_flaresolverr_ok: Optional[bool] = None


def _flaresolverr_available() -> bool:
    """Probe once at startup; cache result to avoid repeated failed connections."""
    global _flaresolverr_ok
    if _flaresolverr_ok is None:
        if not _HAS_REQUESTS:
            _flaresolverr_ok = False
        else:
            try:
                probe = _req.get(FLARESOLVERR_URL.replace('/v1', '/'), timeout=4)
                _flaresolverr_ok = probe.status_code == 200
            except Exception:
                _flaresolverr_ok = False
        status = 'available' if _flaresolverr_ok else 'not reachable'
        print(f'FlareSolverr: {status} at {FLARESOLVERR_URL}')
    return _flaresolverr_ok


def _try_flaresolverr(url: str, browser) -> CheckResult:
    """
    Step A: Ask FlareSolverr to solve the Cloudflare challenge and return cookies.
    Step B: Quick price check on FlareSolverr's own rendered HTML (fast path).
    Step C: Inject cf_clearance cookies into a fresh Patchright context and load
            the real page — Cloudflare sees a valid clearance cookie and passes us
            through to the fully JS-rendered content.
    """
    # Step A — call FlareSolverr service
    try:
        fs_resp = _req.post(FLARESOLVERR_URL, json={
            'cmd': 'request.get',
            'url': url,
            'maxTimeout': 60000,
        }, timeout=75)
        fs_resp.raise_for_status()
        result = fs_resp.json()
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[flaresolverr] service call failed: {str(e)[:100]}',
                           engine='flaresolverr', blocked=False)

    if result.get('status') != 'ok':
        return CheckResult(price=None, sold=None,
                           debug=f'[flaresolverr] solver error: {result.get("message","?")[:100]}',
                           engine='flaresolverr', blocked=False)

    solution   = result['solution']
    cookies    = solution.get('cookies', [])
    user_agent = solution.get('userAgent', '')
    raw_html   = solution.get('response', '')

    # Step B — quick price scan on FlareSolverr's own HTML response (avoids a
    # second browser launch if the static HTML already contains the price)
    static_price = _extract_price_from_html(raw_html)
    if static_price is not None:
        print(f'    → flaresolverr static HTML had price ${static_price}')
        return CheckResult(price=static_price, sold=None, debug='', engine='flaresolverr')

    # Step C — inject cookies into Patchright for a full interactive session
    ctx = page = None
    try:
        ctx = browser.new_context(
            user_agent=user_agent or ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                      'Chrome/124.0.0.0 Safari/537.36'),
            viewport={'width': 1280, 'height': 800},
        )
        # Translate FlareSolverr cookie schema → Playwright schema
        if cookies:
            ctx.add_cookies([{
                'name':     c.get('name', ''),
                'value':    c.get('value', ''),
                'domain':   c.get('domain', '').lstrip('.'),
                'path':     c.get('path', '/'),
                'secure':   c.get('secure', False),
                'httpOnly': c.get('httpOnly', False),
            } for c in cookies])

        page = ctx.new_page()
        resp = page.goto(url, wait_until='load', timeout=45000)
        page.wait_for_timeout(random.randint(2000, 3000))
        _dismiss_popups(page)
        _human_scroll(page)
        _dismiss_popups(page)
        return _page_to_result(page, resp, url, 'flaresolverr')
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[flaresolverr+patchright] page error: {str(e)[:100]}',
                           engine='flaresolverr', blocked=False)
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


# ── Engine 5: ScraperAPI (free tier, residential proxy) ───────────────────────

def _try_scraperapi(url: str) -> CheckResult:
    """ScraperAPI routes through residential IPs and handles bot-protection.
    Free tier: 1000 credits/month, no credit card required.
    render=true (JavaScript rendering) costs 10 credits per request, so the
    free tier covers ~100 JS-rendered price checks per month — enough for
    daily checking of ~3 stubborn listings. Set SCRAPERAPI_KEY in Secrets."""
    if not SCRAPERAPI_KEY or not _HAS_REQUESTS:
        return CheckResult(price=None, sold=None,
                           debug='[scraperapi] not configured (SCRAPERAPI_KEY secret)',
                           engine='scraperapi', blocked=False)
    try:
        resp = _req.get(
            'http://api.scraperapi.com',
            params={
                'api_key': SCRAPERAPI_KEY,
                'url':     url,
                'render':  'true',
            },
            timeout=90,
        )
        if resp.status_code in (404, 410):
            return CheckResult(price=None,
                               sold=f'SOLD/REMOVED — ScraperAPI got {resp.status_code}',
                               debug='', engine='scraperapi')
        if resp.status_code != 200:
            return CheckResult(price=None, sold=None,
                               debug=f'[scraperapi] HTTP {resp.status_code}: {resp.text[:80]}',
                               engine='scraperapi', blocked=False)

        html = resp.text

        # JSON-LD
        for ld in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE):
            try:
                data = json.loads(ld)
                for item in (data if isinstance(data, list) else [data]):
                    if not isinstance(item, dict):
                        continue
                    offers = item.get('offers', {})
                    p = offers.get('price') if isinstance(offers, dict) else None
                    if p:
                        return CheckResult(price=float(str(p).replace(',', '')),
                                           sold=None, debug='', engine='scraperapi')
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue

        # Sold phrases
        html_lower = html.lower()
        for phrase in SOLD_PHRASES:
            if phrase in html_lower:
                return CheckResult(price=None,
                                   sold=f'SOLD/REMOVED — page says "{phrase}"',
                                   debug='', engine='scraperapi')

        price = _extract_price_from_html(html)
        if price is not None:
            return CheckResult(price=price, sold=None, debug='', engine='scraperapi')

        return CheckResult(price=None, sold=None,
                           debug='[scraperapi] page loaded but no price in rendered HTML',
                           engine='scraperapi', blocked=False)
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[scraperapi] error: {str(e)[:100]}',
                           engine='scraperapi', blocked=False)


# ── Engine 6: ZenRows (optional paid residential proxy) ───────────────────────

def _try_zenrows(url: str) -> CheckResult:
    """ZenRows routes through residential IPs and handles bot-protection at the
    infrastructure level — the only reliable escalation for sites that block
    all datacenter IPs regardless of browser fingerprinting. Paid service
    (~$49/month); one-time free trial credits on sign-up. Only active when
    ZENROWS_API_KEY is set in environment / GitHub Secrets."""
    if not ZENROWS_API_KEY or not _HAS_REQUESTS:
        return CheckResult(price=None, sold=None,
                           debug='[zenrows] not configured (set ZENROWS_API_KEY secret)',
                           engine='zenrows', blocked=False)
    try:
        resp = _req.get(
            'https://api.zenrows.com/v1/',
            params={
                'apikey':    ZENROWS_API_KEY,
                'url':       url,
                'js_render': 'true',
                'antibot':   'true',
                'wait':      '2000',
            },
            timeout=90,
        )
        if resp.status_code in (404, 410):
            return CheckResult(price=None,
                               sold=f'SOLD/REMOVED — ZenRows got {resp.status_code}',
                               debug='', engine='zenrows')
        if resp.status_code != 200:
            return CheckResult(price=None, sold=None,
                               debug=f'[zenrows] HTTP {resp.status_code}: {resp.text[:100]}',
                               engine='zenrows', blocked=False)

        html = resp.text

        # JSON-LD
        for ld in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE):
            try:
                data = json.loads(ld)
                for item in (data if isinstance(data, list) else [data]):
                    if not isinstance(item, dict):
                        continue
                    offers = item.get('offers', {})
                    p = offers.get('price') if isinstance(offers, dict) else None
                    if p:
                        return CheckResult(price=float(str(p).replace(',', '')),
                                           sold=None, debug='', engine='zenrows')
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue

        # Sold phrases
        html_lower = html.lower()
        for phrase in SOLD_PHRASES:
            if phrase in html_lower:
                return CheckResult(price=None,
                                   sold=f'SOLD/REMOVED — page says "{phrase}"',
                                   debug='', engine='zenrows')

        price = _extract_price_from_html(html)
        if price is not None:
            return CheckResult(price=price, sold=None, debug='', engine='zenrows')

        return CheckResult(price=None, sold=None,
                           debug='[zenrows] page loaded but no price found in rendered HTML',
                           engine='zenrows', blocked=False)
    except Exception as e:
        return CheckResult(price=None, sold=None,
                           debug=f'[zenrows] error: {str(e)[:100]}',
                           engine='zenrows', blocked=False)


# ── Waterfall ──────────────────────────────────────────────────────────────────

def check_listing(url: str, browser) -> CheckResult:
    """Try each engine in order; return as soon as price or sold status is found."""
    result = CheckResult(price=None, sold=None, debug='no engines available', engine='none')

    # Engine 0 — curl-cffi (TLS impersonation, no browser, fastest)
    if _HAS_CURL_CFFI:
        result = _try_curl_cffi(url)
        print(f'    curl-cffi    → price={result.price}  sold={bool(result.sold)}  blocked={result.blocked}')
        if result.price is not None or result.sold is not None:
            return result
        # Continue to browser engines even when not blocked — price may need JS

    # Engine 1 — Patchright (patched Chromium, no external service needed)
    if _HAS_PATCHRIGHT:
        result = _try_patchright(url, browser)
        print(f'    patchright   → price={result.price}  sold={bool(result.sold)}  blocked={result.blocked}')
        if result.price is not None or result.sold is not None:
            return result

    # Engine 2 — nodriver (pure CDP Chrome, zero Playwright/Selenium layer)
    if _HAS_NODRIVER:
        result = _try_nodriver(url)
        print(f'    nodriver     → price={result.price}  sold={bool(result.sold)}  blocked={result.blocked}')
        if result.price is not None or result.sold is not None:
            return result

    # Engine 3 — Camoufox (patched Firefox, completely different engine fingerprint)
    if _HAS_CAMOUFOX:
        result = _try_camoufox(url)
        print(f'    camoufox     → price={result.price}  sold={bool(result.sold)}  blocked={result.blocked}')
        if result.price is not None or result.sold is not None:
            return result

    # Engine 4 — FlareSolverr (dedicated Cloudflare solver + Patchright session)
    if _flaresolverr_available() and _HAS_PATCHRIGHT:
        result = _try_flaresolverr(url, browser)
        print(f'    flaresolverr → price={result.price}  sold={bool(result.sold)}  blocked={result.blocked}')
        if result.price is not None or result.sold is not None:
            return result

    # Engine 5 — ScraperAPI (residential proxy, free tier 1000 credits/month)
    if SCRAPERAPI_KEY:
        result = _try_scraperapi(url)
        print(f'    scraperapi   → price={result.price}  sold={bool(result.sold)}')
        if result.price is not None or result.sold is not None:
            return result

    # Engine 6 — ZenRows (residential proxy, paid, final escalation)
    if ZENROWS_API_KEY:
        result = _try_zenrows(url)
        print(f'    zenrows      → price={result.price}  sold={bool(result.sold)}')

    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def _is_catalog_url(url: str) -> bool:
    """Heuristic: True when the URL looks like a multi-listing category/search page
    rather than a single-item detail page.

    Single listings almost always have a VIN, a model year, or a long numeric ID
    in the path. Category pages have slugs like /used-drivetrains/lsa-drivetrains/
    with none of those signals."""
    parsed = urlparse(url)
    path   = parsed.path.rstrip('/')
    # VIN (17 alphanumeric, no I/O/Q) → single listing
    if re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', path, re.IGNORECASE):
        return False
    # Model year in path → single listing (e.g. /used-2004-gmc-sierra/)
    if re.search(r'(?:^|[-/])(?:19|20)\d{2}(?:[-/]|$)', path):
        return False
    # Long numeric product/listing ID at the end of the path
    if re.search(r'/\d{5,}/?$', path):
        return False
    # UUID / GUID (e.g. /Inventory/Details/4fa31a40-1bcd-4bf6-bad3-de37b417c8a5)
    if re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                 path, re.IGNORECASE):
        return False
    # Cart / checkout / order pages — always a single page, never a paginated catalog.
    # Check both path and query-param names (e.g. cartIdToken, checkoutToken).
    combined = (path + '&' + parsed.query).lower()
    if re.search(r'\b(cart|checkout|basket|uceditor|order)\b', combined):
        return False
    # Long hex session token in any query parameter value → session-scoped single page
    for val in parse_qs(parsed.query).values():
        if val and re.fullmatch(r'[0-9A-Fa-f]{32,}', val[0]):
            return False
    # Anything else: treat as potential catalog; if only 1 price is found on the
    # page the result is indistinguishable from a regular single-listing check.
    return True


def _extract_all_prices_from_html(html: str, min_price: float = 100.0) -> list:
    """Return all prices found in raw HTML that are at or above min_price.

    Prices are deduplicated by value across strategies so the same price
    embedded in multiple HTML elements isn't counted twice. On catalog pages
    this means two products at the same price appear as one entry — use
    _extract_prices_via_js() for accurate per-card counts instead.

    Extraction strategies (in order of reliability):
    1. JSON-LD structured data — ItemList/Product with offers.price
    2. data-price / data-regular-price / data-sale-price attributes
    3. "price": N JSON patterns in script blobs
    4. Standard $-prefixed price text patterns
    """
    found_set  = set()   # dedup by value across strategies
    found_list = []

    def _add(p: float):
        if p >= min_price and p not in found_set:
            found_set.add(p)
            found_list.append(p)

    # Strategy 1: JSON-LD (most structured; ItemList gives one entry per product)
    for ld in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(ld)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for node in [item] + item.get('itemListElement', []):
                    if not isinstance(node, dict):
                        continue
                    offers = node.get('offers', {})
                    price_val = offers.get('price') if isinstance(offers, dict) else None
                    if price_val:
                        try:
                            _add(float(str(price_val).replace(',', '')))
                        except (ValueError, TypeError):
                            pass
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue

    # Strategy 2: data-price attributes (WooCommerce, BigCommerce, dealer platforms)
    for m in re.finditer(r'data-(?:regular-|sale-)?price=["\']?([\d]+(?:\.\d{1,2})?)["\']?',
                         html, re.IGNORECASE):
        try:
            _add(float(m.group(1)))
        except ValueError:
            pass

    # Strategy 3: "price": N JSON patterns in inline script data
    for m in re.finditer(r'"price"\s*:\s*"?([\d]+(?:\.\d{1,2})?)"?', html):
        try:
            _add(float(m.group(1)))
        except ValueError:
            pass

    # Strategy 4: $-prefixed visible price text (catches anything the above missed)
    for pat in _PRICE_PATTERNS:
        for m in re.finditer(pat, html):
            try:
                _add(float(m.group(1).replace(',', '')))
            except ValueError:
                pass

    return sorted(found_list)


def _next_page_param_url(current_url: str) -> Optional[str]:
    """Fallback pagination: increment the ?page=N query parameter by 1.
    If no ?page= param exists, adds ?page=2 (page 1 → page 2 transition).
    Also handles WordPress /page/N/ path style.
    Returns None if the URL already looks like it's using a non-standard scheme."""
    parsed = urlparse(current_url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    if 'page' in params:
        try:
            n = int(params['page'][0])
            params['page'] = [str(n + 1)]
            q = urlencode({k: v[0] for k, v in params.items()})
            return urlunparse(parsed._replace(query=q))
        except (ValueError, IndexError):
            return None

    # WordPress /page/N/ path style
    m = re.search(r'/page/(\d+)/?$', parsed.path)
    if m:
        new_path = parsed.path[:m.start()] + f'/page/{int(m.group(1)) + 1}/'
        return urlunparse(parsed._replace(path=new_path))

    # No page param at all — first call goes from page 1 to page 2
    params['page'] = ['2']
    q = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=q))


def _find_next_page_url(html: str, current_url: str) -> Optional[str]:
    """Find the URL of the next pagination page, or None if on the last page.

    Handles rel=next (WordPress/WooCommerce standard), class=next patterns,
    aria-label=next, and common Next button text as a last resort."""
    def resolve(href: str) -> Optional[str]:
        full = urljoin(current_url, href.replace('&amp;', '&'))
        return full if full.rstrip('/') != current_url.rstrip('/') else None

    # rel="next" (most reliable — semantic HTML standard)
    for pat in [
        r'<(?:a|link)[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']',
        r'<(?:a|link)[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            r = resolve(m.group(1))
            if r:
                return r

    # class="next" / class="page-next" (WooCommerce, many frameworks)
    for pat in [
        r'<a[^>]+class=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*\bnext\b[^"\']*["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            r = resolve(m.group(1))
            if r:
                return r

    # aria-label containing "next"
    for pat in [
        r'<a[^>]+aria-label=["\'][^"\']*next[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*aria-label=["\'][^"\']*next[^"\']*["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            r = resolve(m.group(1))
            if r:
                return r

    # Visible "Next" / "›" / "»" text inside a link (last resort)
    m = re.search(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:Next(?:\s+Page)?|›|»)\s*</a>',
        html, re.IGNORECASE)
    if m:
        r = resolve(m.group(1))
        if r:
            return r

    return None


_CATALOG_PRICE_JS = """(function () {
    /* IIFE — return one price per product card, preserving duplicate prices.
       Strategy order (first that finds 2+ prices wins):
       1. itemprop="price" content attr  (BigCommerce/WooCommerce/Shopify schema.org)
       2. data-product-price attribute    (BigCommerce Stencil themes)
       3. data-price attribute            (WooCommerce / generic)
       4. Common price CSS selectors, one price per card ancestor */
    var prices = [];
    var MIN = 100;

    /* Strategy 1: Schema.org content attribute */
    document.querySelectorAll('[itemprop="price"][content]').forEach(function(el) {
        var p = parseFloat(el.getAttribute('content'));
        if (!isNaN(p) && p >= MIN) prices.push(p);
    });
    if (prices.length >= 2) return prices;

    /* Strategy 2: BigCommerce Stencil data-product-price */
    document.querySelectorAll('[data-product-price]').forEach(function(el) {
        var txt = el.textContent.replace(/[^0-9.,]/g, '');
        var p = parseFloat(txt.replace(/,/g, ''));
        if (!isNaN(p) && p >= MIN) prices.push(p);
    });
    if (prices.length >= 2) return prices;

    /* Strategy 3: Generic data-price attribute value */
    document.querySelectorAll('[data-price]').forEach(function(el) {
        var p = parseFloat(el.getAttribute('data-price'));
        if (!isNaN(p) && p >= MIN) prices.push(p);
    });
    if (prices.length >= 2) return prices;

    /* Strategy 4: Common price class selectors — one price per card ancestor */
    var seen = new WeakSet();
    var sels = ['.price--main', '.price-value', '.woocommerce-Price-amount',
                '.product-price', '.bc-product-card__price', '.price--withoutTax',
                '.price'];
    for (var si = 0; si < sels.length; si++) {
        var sel = sels[si];
        var els = Array.prototype.slice.call(document.querySelectorAll(sel));
        if (els.length < 2) continue;
        els.forEach(function(el) {
            var card = el;
            for (var i = 0; i < 8; i++) {
                if (!card.parentElement) break;
                card = card.parentElement;
                var tag = card.tagName.toLowerCase();
                var cls = (card.className || '').toLowerCase();
                if (tag === 'article' || tag === 'li' ||
                    cls.indexOf('product') !== -1 || cls.indexOf('card') !== -1 ||
                    cls.indexOf('item') !== -1 || cls.indexOf('listing') !== -1) break;
            }
            if (seen.has(card)) return;
            seen.add(card);
            var txt = el.textContent.replace(/[$,\\s]/g, '');
            var m = txt.match(/[0-9]+(?:\\.[0-9]{1,2})?/);
            if (m) {
                var p = parseFloat(m[0]);
                if (!isNaN(p) && p >= MIN) prices.push(p);
            }
        });
        if (prices.length >= 2) return prices;
    }
    return prices;
}())"""


def _extract_prices_via_js(page) -> list:
    """Run the catalog price JS in the rendered page and return all found prices.
    Uses DOM-level counting so two items at the same price both appear."""
    try:
        result = page.evaluate(_CATALOG_PRICE_JS)
        if isinstance(result, list) and result:
            print(f'    [js-extract] {len(result)} prices found via DOM')
            return sorted(float(p) for p in result if p)
        print(f'    [js-extract] empty result (type={type(result).__name__}), '
              f'falling back to HTML extraction')
    except Exception as e:
        print(f'    [js-extract] exception: {str(e)[:200]} — falling back to HTML extraction')
    return []


def _is_cf_challenge_html(html: str) -> bool:
    """True when the page looks like a Cloudflare challenge regardless of its title.

    A CF challenge page may show the site's own title (so title-only detection
    misses it), but always contains multiple CF-specific JS/HTML fingerprints."""
    signals = [
        'cf-browser-verification', 'cf-challenge-running', 'cf_chl_opt',
        'window._cf_', 'cf-please-wait', 'cf_chl_prog', 'jschl-answer',
        'cdn-cgi/challenge-platform', 'cf-im-under-attack',
    ]
    html_lower = html.lower()
    return sum(1 for s in signals if s in html_lower) >= 2


def scrape_catalog_pages(url: str, browser, max_pages: int = MAX_CATALOG_PAGES) -> tuple:
    """Fetch a multi-listing catalog/category URL and collect all prices across all
    paginated pages.

    Uses ONE persistent Patchright browser context for the entire scrape so that
    Cloudflare clearance cookies earned on page 1 carry through to pages 2-N.
    Opening a fresh context per page causes Cloudflare to re-challenge by page 5.

    Falls back to curl-cffi for sites where Patchright is blocked or unavailable.
    Pagination follows rel=next / class=next / ?page=N up to max_pages pages.

    Returns: (prices: list[float], engine: str, page_count: int)
    """
    all_prices  = []
    engine_used = 'none'
    current_url = url
    visited     = set()
    page_num    = 0

    # Open the single shared Patchright context before the pagination loop.
    ctx = cat_page = None
    if _HAS_PATCHRIGHT:
        try:
            ctx = browser.new_context(
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) '
                            'Chrome/124.0.0.0 Safari/537.36'),
                viewport={'width': 1280, 'height': 900},
            )
            cat_page = ctx.new_page()
            engine_used = 'patchright'
        except Exception:
            ctx = cat_page = None

    try:
        while current_url and page_num < max_pages:
            norm = current_url.rstrip('/')
            if norm in visited:
                break
            visited.add(norm)
            page_num += 1

            print(f'    catalog p{page_num}: {current_url}')
            html = None

            # Navigate within the shared context — cookies (cf_clearance etc.) persist
            if cat_page is not None:
                try:
                    cat_page.goto(current_url, wait_until='load', timeout=60000)
                    cat_page.wait_for_timeout(random.randint(2500, 4000))
                    _dismiss_popups(cat_page)
                    # Scroll in chunks to trigger lazy-loaded product tiles
                    for _ in range(6):
                        cat_page.mouse.wheel(0, 1200)
                        cat_page.wait_for_timeout(random.randint(400, 700))
                    cat_page.wait_for_timeout(1200)
                    _dismiss_popups(cat_page)
                    if _is_blocked(cat_page):
                        print(f'    → p{page_num}: Cloudflare title challenge, stopping')
                        break
                    html = cat_page.content()
                except Exception as e:
                    print(f'    → patchright error on p{page_num}: {str(e)[:80]}')
                    break

            # curl-cffi fallback (no persistent context, but works on static/lightly
            # protected catalog pages when Patchright is unavailable)
            if html is None and _HAS_CURL_CFFI:
                for impersonate in ('chrome124', 'chrome120', 'firefox122'):
                    try:
                        with _cffi.Session(impersonate=impersonate) as s:
                            resp = s.get(current_url, timeout=30, allow_redirects=True,
                                         headers={'Accept-Language': 'en-US,en;q=0.9'})
                        if resp.status_code != 200:
                            continue
                        candidate = resp.text
                        t = re.search(r'<title[^>]*>(.*?)</title>', candidate[:3000],
                                      re.IGNORECASE | re.DOTALL)
                        if t and t.group(1).strip().lower() in _BLOCK_TITLES:
                            continue
                        html = candidate
                        engine_used = 'curl-cffi'
                        break
                    except Exception:
                        continue

            if not html:
                print(f'    → could not fetch page {page_num}')
                break

            # Catch CF challenges that use the site's real title (title check alone
            # won't detect these — look for CF-specific HTML fingerprints instead)
            if _is_cf_challenge_html(html):
                print(f'    → p{page_num}: Cloudflare HTML challenge detected, stopping')
                break

            # JS-level extraction (via Patchright): queries the rendered DOM for one
            # price element per product card — preserves items that share a price.
            # Falls back to HTML scanning for curl-cffi fetched pages.
            if cat_page is not None:
                page_prices = _extract_prices_via_js(cat_page)
                if not page_prices:
                    page_prices = _extract_all_prices_from_html(html)
            else:
                page_prices = _extract_all_prices_from_html(html)

            if not page_prices:
                print(f'    → no prices on page {page_num}, stopping pagination')
                break

            all_prices.extend(page_prices)
            sample = ', '.join(f'${p:,.0f}' for p in page_prices[:5])
            extra  = f' +{len(page_prices) - 5} more' if len(page_prices) > 5 else ''
            print(f'    → {len(page_prices)} prices [{engine_used}]: {sample}{extra}')

            # Brief human-like pause before loading the next page
            if cat_page is not None:
                cat_page.wait_for_timeout(random.randint(800, 1500))

            next_url = _find_next_page_url(html, current_url)
            if not next_url:
                next_url = _next_page_param_url(current_url)
            if not next_url or next_url.rstrip('/') in visited:
                break
            current_url = next_url

    finally:
        if cat_page:
            try: cat_page.close()
            except Exception: pass
        if ctx:
            try: ctx.close()
            except Exception: pass

    return all_prices, engine_used, page_num


def _parse_urls(cell_value: str) -> list:
    """Return all http(s) URLs from a cell that may contain comma- or
    newline-separated values (e.g. two eBay listings for the same part so
    the checker can average their prices)."""
    return [u.strip() for u in re.split(r'[,\n]+', cell_value)
            if u.strip().startswith('http')]


def main():
    if not _HAS_PATCHRIGHT:
        raise SystemExit(
            'patchright is required: pip install patchright && patchright install chromium'
        )

    spreadsheet = connect_to_sheet()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    print(f'Engines: curl_cffi={_HAS_CURL_CFFI}  patchright={_HAS_PATCHRIGHT}  '
          f'nodriver={_HAS_NODRIVER}  camoufox={_HAS_CAMOUFOX}')
    print(f'FlareSolverr: {FLARESOLVERR_URL}')
    print(f'ScraperAPI: {"configured" if SCRAPERAPI_KEY else "not set (optional — scraperapi.com, free tier)"}')
    print(f'ZenRows:    {"configured" if ZENROWS_API_KEY else "not set (optional — paid)"}\n')

    with _pw() as p:
        browser = p.chromium.launch(headless=True)

        for sheet in spreadsheet.worksheets():
            cols = find_columns(sheet)
            if not cols:
                continue

            all_values = sheet.get_all_values()
            for row_idx in range(cols['header_row'] + 1, len(all_values) + 1):
                row = all_values[row_idx - 1]
                url_cell = row[cols['url_col'] - 1] if len(row) >= cols['url_col'] else ''
                urls = _parse_urls(url_cell)
                if not urls:
                    continue

                old_price_str = row[cols['price_col'] - 1] if len(row) >= cols['price_col'] else ''

                # collected: (price, engine) tuples — one per item found, whether
                # that item came from a single listing or a catalog page.
                collected          = []
                any_sold           = None
                any_debug          = []
                catalog_page_total = 0  # sum of pages followed across all catalog URLs

                for url in urls:
                    if _is_catalog_url(url):
                        print(f'\n{sheet.title} row {row_idx}: catalog → {url}')
                        prices, eng, pages = scrape_catalog_pages(url, browser)
                        catalog_page_total += pages
                        if prices:
                            collected.extend((p, eng) for p in prices)
                        else:
                            any_debug.append(f'catalog {url[:60]}: no prices found')
                    else:
                        print(f'\n{sheet.title} row {row_idx}: listing → {url}')
                        result = check_listing(url, browser)
                        if result.sold:
                            any_sold = result.sold
                            break
                        if result.price is not None:
                            collected.append((result.price, result.engine))
                        else:
                            any_debug.append(result.debug)

                # ── Write results ──────────────────────────────────────────────
                if any_sold:
                    if cols['status_col']:
                        sheet.update_cell(row_idx, cols['status_col'], any_sold)
                    if cols['checked_col']:
                        sheet.update_cell(row_idx, cols['checked_col'],
                                          f'{now}  {any_sold}')

                elif collected:
                    prices  = [p for p, _ in collected]
                    avg     = round(sum(prices) / len(prices), 2)
                    engines = list(dict.fromkeys(e for _, e in collected))  # unique, ordered

                    if len(collected) == 1 and catalog_page_total == 0:
                        # Single listing URL → keep original compact note
                        note = f'{now}  [{engines[0]}]'
                        if old_price_str and old_price_str not in ('TBD', ''):
                            try:
                                old = float(old_price_str.replace('$', '').replace(',', ''))
                                note += ('  ⚠ CHANGED from $' + old_price_str
                                         if old != avg else '  — unchanged')
                            except ValueError:
                                pass
                        sheet.update_cell(row_idx, cols['price_col'], avg)
                        if cols['checked_col']:
                            sheet.update_cell(row_idx, cols['checked_col'], note)
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], 'Available')

                    elif catalog_page_total > 0:
                        # At least one catalog URL — summarise by item+page count
                        note = (f'{now}  avg ${avg:,.0f}  '
                                f'({len(prices)} items / {catalog_page_total} page(s) '
                                f'via {", ".join(engines)})')
                        if old_price_str and old_price_str not in ('TBD', ''):
                            try:
                                old = float(old_price_str.replace('$', '').replace(',', ''))
                                note += (f'  ⚠ CHANGED from ${old:,.0f}'
                                         if old != avg else '  — unchanged')
                            except ValueError:
                                pass
                        sheet.update_cell(row_idx, cols['price_col'], avg)
                        if cols['checked_col']:
                            sheet.update_cell(row_idx, cols['checked_col'], note)
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], 'Available')

                    else:
                        # Multiple comma-separated listing URLs → per-URL breakdown
                        parts = ' · '.join(f'${p:,.0f} [{e}]' for p, e in collected)
                        note  = (f'{now}  avg ${avg:,.0f} '
                                 f'({len(prices)}/{len(urls)} URLs): {parts}')
                        if old_price_str and old_price_str not in ('TBD', ''):
                            try:
                                old = float(old_price_str.replace('$', '').replace(',', ''))
                                note += (f'  ⚠ CHANGED from ${old:,.0f}'
                                         if old != avg else '  — unchanged')
                            except ValueError:
                                pass
                        sheet.update_cell(row_idx, cols['price_col'], avg)
                        if cols['checked_col']:
                            sheet.update_cell(row_idx, cols['checked_col'], note)
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], 'Available')

                else:
                    debug = '; '.join(any_debug) or 'all engines tried'
                    if cols['checked_col']:
                        sheet.update_cell(
                            row_idx, cols['checked_col'],
                            f'{now}  no prices found — {debug[:120]}'
                        )

        browser.close()

    print('\nDone.')


if __name__ == '__main__':
    main()
