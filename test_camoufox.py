"""
Test 2 of 3: Camoufox — a patched, anti-detect build of Firefox
purpose-built for fingerprint spoofing (canvas, WebGL, audio context,
font list — the deeper signals a Chromium stealth plugin can't reach
since it's patching a different browser engine entirely). Testing
whether a genuinely different browser engine, not just a patched
Chromium, gets past this site's protection.
"""
import re
import os
from camoufox.sync_api import Camoufox

URL = 'https://www.jamesonealchryslerdodgejeep.com/inventory/used-2004-gmc-sierra-2500hd-slt-4wd-4d-crew-cab-1gthk23u64f251261/'

PRICE_PATTERNS = [
    r'\$([\d]{2,3},\d{3})\.\d{2}',
    r'\$([\d]{2,3},\d{3})(?!\d)',
    r'\$([\d]{4,6})(?!\d)',
]


def write_summary(method, success, detail):
    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        with open(summary_file, 'a') as f:
            icon = '✅' if success else '❌'
            f.write(f'| {method} | {icon} | {detail} |\n')


def main():
    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        try:
            page.goto(URL, timeout=45000)
            page.wait_for_timeout(3000)
            title = page.title()
            print(f"PAGE TITLE: {title}")

            if 'just a moment' in title.lower():
                print("RESULT: BLOCKED — still shows Cloudflare challenge page")
                write_summary('Camoufox', False, f'Blocked — title was "{title}"')
                return

            text = page.locator('body').inner_text()
            for pattern in PRICE_PATTERNS:
                m = re.search(pattern, text)
                if m:
                    price = m.group(1).replace(',', '')
                    print(f"RESULT: SUCCESS — found price ${price}")
                    write_summary('Camoufox', True, f'Found ${price}, page title: "{title}"')
                    return

            print("RESULT: PAGE LOADED but no price pattern matched")
            write_summary('Camoufox', False, f'Page loaded (title: "{title}") but no price found in text')
        except Exception as e:
            print(f"RESULT: ERROR — {e}")
            write_summary('Camoufox', False, f'Error: {e}')


if __name__ == '__main__':
    main()
