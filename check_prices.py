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
    creds_dict = json.loads(os.environ['GOOGLE_SERVICE_JSON'])
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
SOLD_PHRASES = [
    'this vehicle has been sold', 'vehicle has sold', 'this vehicle is sold',
    'no longer available', 'vehicle unavailable', 'listing has ended',
    'listing is no longer active', 'this listing has expired',
    'off the market', 'currently unavailable', 'vehicle not found',
    'this vehicle is no longer', 'page not found', "sorry, we couldn't find",
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

    # Signal 2: redirected away from the specific listing to somewhere
    # generic (search results, homepage) — a strong signal the exact
    # vehicle/product page no longer exists, even if the site itself
    # returned a normal 200 status for the page it redirected to
    final_url = page.url
    if final_url.rstrip('/') != original_url.rstrip('/'):
        # only treat this as suspicious if the URL structure actually
        # changed shape, not just a trailing-slash or query-param tweak
        orig_path = original_url.split('?')[0].rstrip('/')
        final_path = final_url.split('?')[0].rstrip('/')
        if orig_path != final_path and len(final_path) < len(orig_path) * 0.7:
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


def extract_price(page):
    """Try several strategies, most reliable first, same approach real
    price-tracking tools use since no single method works everywhere."""

    # Strategy 1: JSON-LD structured data (most reliable when present —
    # many listing sites embed this for search engine SEO)
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_text_contents()
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
        pass

    # Strategy 2: common price meta tags
    for selector in ['meta[itemprop="price"]', 'meta[property="product:price:amount"]']:
        try:
            el = page.locator(selector).first
            if el.count() > 0:
                content = el.get_attribute('content')
                if content:
                    return float(content.replace(',', ''))
        except Exception:
            continue

    # Strategy 3: fall back to visible rendered text — this is the part
    # that only works because Playwright actually executed the JS first
    try:
        text = page.locator('body').inner_text()
        matches = re.findall(r'\$([\d]{2,3},\d{3})(?!\d)', text)
        if matches:
            return float(matches[0].replace(',', ''))
        matches = re.findall(r'\$([\d]{1,4}\.\d{2})(?!\d)', text)
        if matches:
            return float(matches[0])
    except Exception:
        pass

    return None


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
                    response = page.goto(url, wait_until='networkidle', timeout=30000)
                    sold_status = check_sold(page, response, url)

                    if sold_status:
                        # Don't overwrite the price when something's sold — the last
                        # real price is still useful historical context — but do
                        # flag it clearly so it's impossible to miss.
                        if cols['status_col']:
                            sheet.update_cell(row_idx, cols['status_col'], sold_status)
                        if cols['checked_col']:
                            sheet.update_cell(row_idx, cols['checked_col'], now)
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
                            sheet.update_cell(row_idx, cols['checked_col'],
                                               f'{now}  price not found — page layout may have changed')
                except Exception as e:
                    if cols['checked_col']:
                        sheet.update_cell(row_idx, cols['checked_col'], f'{now}  Error: {str(e)[:100]}')
                finally:
                    page.close()

        browser.close()

    print("Done.")


if __name__ == '__main__':
    main()
