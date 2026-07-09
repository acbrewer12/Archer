"""
Archer price checker — real headless browser version.

Unlike the Google Apps Script version (which only sees raw server HTML),
this uses Playwright to launch an actual Chromium browser, let every
site's JavaScript fully execute, then read the price out of the
REAL rendered page. This is what actually solves JS-rendered pricing —
Apps Script structurally cannot do this, no matter how the code is
written, because it has no way to run a browser engine at all.

Also detects listings that have been sold or removed, using three
independent signals (a 404/410 status, a redirect away from the
specific listing page, or sold/unavailable text actually on the
rendered page) and writes the result to a "Status" column — separate
from Price, so a sold listing's last real price stays visible as
history instead of getting overwritten or blanked.

Same dynamic design as the Apps Script version: scans every sheet in
the spreadsheet for "Price" / "Status" / "Listing URL" / "Last Checked"
columns by header text, and checks whatever URL is in any matching
row. Add a new part or truck by pasting its URL into the sheet —
nothing here needs to change.

ONE-TIME SETUP (the only part that needs you, not code):
1. Go to https://console.cloud.google.com/ → create a project (any name).
2. APIs & Services → Enable APIs → enable "Google Sheets API".
3. APIs & Services → Credentials → Create Credentials → Service Account.
   Give it any name, skip the optional permission steps, click Done.
4. Click the service account you just made → Keys tab → Add Key →
   Create New Key → JSON. This downloads a .json file — keep it safe,
   it's a real credential.
5. Open your Google Sheet → Share → paste in the service account's
   email address (looks like xxxx@xxxx.iam.gserviceaccount.com,
   found inside that JSON file) → give it Editor access.
6. In your GitHub repo: Settings → Secrets and variables → Actions →
   New repository secret.
     - Name: GOOGLE_SERVICE_ACCOUNT_JSON   Value: paste the ENTIRE
       contents of the .json file you downloaded.
     - Name: SPREADSHEET_ID   Value: the long ID in your sheet's URL,
       e.g. docs.google.com/spreadsheets/d/THIS_PART_HERE/edit
That's the whole setup. The workflow file handles the rest.
"""

import json
import os
import re
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def connect_to_sheet():
    creds_dict = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ['SPREADSHEET_ID'])


def find_columns(sheet):
    """Scan the first 15 rows for a header row containing both
    'price' and 'listing url' (case-insensitive substring match, so
    'Listing URL (for re-checks)' still matches). Returns row/column
    numbers, or None if this sheet doesn't have that pattern."""
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


# Phrases that reliably signal a listing is gone, checked against the
# fully-rendered page text (lowercase). Kept fairly specific — bare
# "sold" alone is too easy to false-positive on ("500+ sold this year"
# type marketing copy), so most of these are multi-word phrases.
# Every phrase here specifically references "vehicle" or "listing" —
# deliberately excludes generic phrases like "currently unavailable" or
# "no longer available" on their own, since those show up constantly
# for reasons that have nothing to do with the car itself: a financing
# widget being down, a chat feature offline, a broken video embed.
# "Currently unavailable" alone caused a real false positive on a
# truck that was still actively for sale — every phrase below is
# tied specifically to the vehicle/listing itself, not a bare
# unavailability word that could belong to anything on the page.
SOLD_PHRASES = [
    'this vehicle has been sold', 'vehicle has sold', 'this vehicle is sold',
    'vehicle is no longer available', 'vehicle unavailable',
    'this vehicle is no longer', 'vehicle not found',
    'listing has ended', 'listing is no longer active', 'this listing has expired',
]


def check_sold(page, response, original_url):
    """Returns 'SOLD/REMOVED' with a reason, or None if the listing
    looks normal. Checks three independent signals since no single one
    is reliable alone — a redirect away from a specific vehicle page is
    strong on its own, sold-phrase text is strong on its own, and a 404
    is strong on its own; any one of them firing is enough to flag it."""

    # Signal 1: HTTP status
    if response.status in (404, 410):
        return f'SOLD/REMOVED — page returned {response.status}'

    # Signal 2: redirected to somewhere that's clearly NOT a specific
    # listing anymore — the homepage, a generic search/results page, or
    # an explicit error page. Deliberately narrow and specific rather
    # than a fuzzy "did the URL get shorter" guess — that kind of
    # heuristic flags completely normal things too (a site cleaning up
    # its own URL, dropping tracking params, adding a slug), which is
    # exactly what caused a real false positive on a truck that was
    # still actually for sale.
    final_url = page.url
    final_path = final_url.split('?')[0].rstrip('/')
    path_lower = final_path.lower()
    generic_redirect_markers = ['/search', '/results', '/inventory?', '/not-found', '/404', '/error']
    is_homepage = final_path in ('', 'https://' + final_path.split('/')[2]) if '//' in final_path else False
    if is_homepage or any(marker in path_lower for marker in generic_redirect_markers):
        return f'SOLD/REMOVED (probably) — redirected to {final_url}'

    # Signal 3: sold-indicator text actually on the rendered page
    try:
        text = page.locator('body').inner_text().lower()
        for phrase in SOLD_PHRASES:
            if phrase in text:
                return f'SOLD/REMOVED — page says "{phrase}"'
    except Exception:
        pass

    return None


def dismiss_popups(page):
    """Click through common cookie-consent banners and promo popups.
    A lot of dealer sites show one of these on first load, and some
    genuinely block the rest of the page from finishing its render
    until it's dismissed — a real, common obstacle, not a rare edge
    case. Tries a range of common button text/patterns; harmless if
    none of them exist on a given page."""
    patterns = [
        'Accept All', 'Accept Cookies', 'Accept', 'I Agree', 'Agree',
        'Got it', 'OK', 'Close', 'No Thanks', 'Continue', 'Dismiss',
    ]
    for text in patterns:
        try:
            btn = page.get_by_role('button', name=text, exact=False).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            continue  # button with this text doesn't exist on this page — expected most of the time

    # generic "X" close button on a modal, by common aria-label patterns
    for selector in ['[aria-label="Close"]', '[aria-label="close"]', 'button.close', '.modal-close']:
        try:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            continue


def get_all_texts(page):
    """Returns the visible text of the main page PLUS every iframe on
    it. Third-party pricing widgets (TradePending and similar dealer
    tools) commonly render inside an iframe — a separate embedded
    document with its own content that the main page's body text
    cannot see at all. This is very likely why the James O'Neal page
    came back with literally zero '$' or 'price' text found even
    though the price is clearly visible on screen."""
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
            continue  # some frames are cross-origin and genuinely unreadable — expected, not an error
    return texts


def extract_price(page):
    """Try several strategies, most reliable first, same approach real
    price-tracking tools use since no single method works everywhere.
    Checks the main page AND all iframes for each strategy."""

    # Strategy 1: JSON-LD structured data (most reliable when present —
    # many listing sites embed this for search engine SEO)
    for frame in page.frames:
        try:
            scripts = frame.locator('script[type="application/ld+json"]').all_text_contents()
            for s in scripts:
                try:
                    data = json.loads(s)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        offers = item.get('offers', {}) if isinstance(item, dict) else {}
                        price = offers.get('price') if isinstance(offers, dict) else None
                        if price:
                            return float(str(price).replace(',', ''))
                except (json.JSONDecodeError, AttributeError, ValueError):
                    continue
        except Exception:
            continue

    # Strategy 2: common price meta tags
    for frame in page.frames:
        for selector in ['meta[itemprop="price"]', 'meta[property="product:price:amount"]']:
            try:
                el = frame.locator(selector).first
                if el.count() > 0:
                    content = el.get_attribute('content')
                    if content:
                        return float(content.replace(',', ''))
            except Exception:
                continue

    # Strategy 3: fall back to visible rendered text, checking the main
    # page AND every iframe — this is the part that only works because
    # Playwright actually executed the JS first. Try comma-formatted
    # first ($10,999), then cents ($10,999.00), then bare digits with
    # no comma ($10999) — small dealer sites in particular often skip
    # comma formatting that bigger platforms use.
    for text in get_all_texts(page):
        for pattern in [
            r'\$([\d]{2,3},\d{3})\.\d{2}',   # $10,999.00
            r'\$([\d]{2,3},\d{3})(?!\d)',    # $10,999
            r'\$([\d]{1,4}\.\d{2})(?!\d)',   # $75.00 (small parts)
            r'\$([\d]{4,6})(?!\d)',          # $10999, no comma at all
        ]:
            m = re.search(pattern, text)
            if m:
                return float(m.group(1).replace(',', ''))

    # Strategy 4: raw HTML source, not just visible rendered text. This
    # catches a real, different failure mode than the others — a price
    # that's technically present in the markup but wrapped in something
    # (a hidden element JS reveals later, an unusual CSS state) that
    # Playwright's visible-text reading skips over. Won't help when a
    # bot-challenge page blocked the real content from loading at all
    # (nothing to find in the HTML if the real page was never
    # delivered) — but that's a different problem than this catches.
    try:
        html = page.content()
        for pattern in [
            r'\$([\d]{2,3},\d{3})\.\d{2}',
            r'\$([\d]{2,3},\d{3})(?!\d)',
            r'\$([\d]{4,6})(?!\d)',
        ]:
            m = re.search(pattern, html)
            if m:
                return float(m.group(1).replace(',', ''))
    except Exception:
        pass

    return None


def capture_debug_snippet(page):
    """When every extraction strategy fails, grab real text from around
    the first '$' or the word 'price' on the page — checking iframes
    too — so the failure note in the sheet has actual content to
    diagnose from instead of just 'not found'."""
    title = ''
    try:
        title = page.title()[:40]
    except Exception:
        pass

    # "Just a moment..." is Cloudflare's own bot-challenge page title —
    # recognizing this specifically means future failures say exactly
    # what happened instead of a cryptic title someone has to recognize.
    if title.strip().lower() in ('just a moment...', 'just a moment', 'attention required!'):
        return (f'[{title}] BLOCKED BY BOT PROTECTION (Cloudflare) — the real page never '
                f'loaded, nothing to find. This site actively blocks automated browsers; '
                f'no amount of retrying the extraction logic fixes this specific site.')

    for text in get_all_texts(page):
        idx = text.find('$')
        if idx == -1:
            idx = text.lower().find('price')
        if idx != -1:
            snippet = text[max(0, idx - 30):idx + 60].replace('\n', ' ').strip()
            return f'[{title}] {snippet[:80]}'
    return f'[{title}] no $ or "price" text found in main page or any of {len(page.frames)-1} iframe(s) after popup-dismiss + scroll'


def main():
    spreadsheet = connect_to_sheet()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )

        for sheet in spreadsheet.worksheets():
            cols = find_columns(sheet)
            if not cols:
                continue  # this sheet has no Price/Listing URL columns — skip (e.g. Dashboard)

            all_values = sheet.get_all_values()
            for row_idx in range(cols['header_row'] + 1, len(all_values) + 1):
                row = all_values[row_idx - 1]
                url = row[cols['url_col'] - 1] if len(row) >= cols['url_col'] else ''
                if not url.startswith('http'):
                    continue  # no URL on this row

                print(f"Checking {sheet.title} row {row_idx}: {url}")
                page = context.new_page()
                try:
                    # 'networkidle' waits for zero network activity — many dealer
                    # sites never go fully quiet (chat widgets, trackers keep
                    # polling forever), so it can hang the full timeout even
                    # though the actual page content loaded fine early on.
                    # 'load' is a reliable, much faster signal; the short
                    # explicit wait after it gives JS time to finish rendering
                    # price data without depending on the network ever going silent.
                    response = page.goto(url, wait_until='load', timeout=45000)
                    page.wait_for_timeout(2500)
                    dismiss_popups(page)
                    # scroll down partway — some price/deal widgets only
                    # render once scrolled into view (lazy-loading for
                    # performance), this nudges them to load
                    try:
                        page.mouse.wheel(0, 800)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    dismiss_popups(page)  # some popups only appear after scroll/delay, not immediately on load
                    sold_status = check_sold(page, response, url)

                    if sold_status:
                        # Don't overwrite the price when something's sold — the last
                        # real price is still useful historical context — but do
                        # flag it clearly so it's impossible to miss.
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], sold_status)
                        if cols['checked_col']:
                            # Include the detail here too, not just a bare timestamp —
                            # every other path (found/not-found) puts the reason in
                            # this same cell, and splitting sold-detection's reason
                            # into a different column than every other case is
                            # exactly what caused real confusion diagnosing this.
                            sheet.update_cell(row_idx, cols['checked_col'], f'{now}  {sold_status}')
                        page.close()
                        continue

                    price = extract_price(page)

                    if price is not None:
                        old_price = row[cols['price_col'] - 1] if len(row) >= cols['price_col'] else ''
                        sheet.update_cell(row_idx, cols['price_col'], price)
                        note = now
                        if old_price and old_price != 'TBD':
                            try:
                                if float(str(old_price).replace('$', '').replace(',', '')) != price:
                                    note += f'  ⚠ CHANGED from ${old_price}'
                                else:
                                    note += '  — unchanged'
                            except ValueError:
                                note += '  — unchanged'
                        if cols['checked_col']:
                            sheet.update_cell(row_idx, cols['checked_col'], note)
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], 'Available')
                    else:
                        if cols['checked_col']:
                            snippet = capture_debug_snippet(page)
                            sheet.update_cell(row_idx, cols['checked_col'],
                                               f'{now}  price not found — saw near "$"/"price": "{snippet}"')
                except Exception as e:
                    if cols['checked_col']:
                        sheet.update_cell(row_idx, cols['checked_col'], f'{now}  Error: {str(e)[:100]}')
                finally:
                    page.close()

        browser.close()

    print("Done.")


if __name__ == '__main__':
    main()
