"""
Test 1 of 3: Patchright — a patched, API-compatible fork of Playwright
that removes automation traces at a deeper level than a stealth plugin
(patches things a plugin can only mask at the JS layer, not the
browser binary's own behavior). Nearly identical code to the real
check_prices.py, isolated here just to test whether this alone gets
past this specific site's Cloudflare protection.
"""
import re
from patchright.sync_api import sync_playwright

URL = 'https://www.jamesonealchryslerdodgejeep.com/inventory/used-2022-ford-f-150-xl-4wd-4d-supercrew-1ftew1ep4nfa96736/'

PRICE_PATTERNS = [
    r'\$([\d]{2,3},\d{3})\.\d{2}',
    r'\$([\d]{2,3},\d{3})(?!\d)',
    r'\$([\d]{4,6})(?!\d)',
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )
        page = context.new_page()

        try:
            page.goto(URL, wait_until='load', timeout=45000)
            page.wait_for_timeout(3000)
            title = page.title()
            print(f"PAGE TITLE: {title}")

            if 'just a moment' in title.lower():
                print("RESULT: BLOCKED — still shows Cloudflare challenge page")
                write_summary('Patchright', False, f'Blocked — title was "{title}"')
                return

            text = page.locator('body').inner_text()
            for pattern in PRICE_PATTERNS:
                m = re.search(pattern, text)
                if m:
                    price = m.group(1).replace(',', '')
                    print(f"RESULT: SUCCESS — found price ${price}")
                    write_summary('Patchright', True, f'Found ${price}, page title: "{title}"')
                    return

            print("RESULT: PAGE LOADED but no price pattern matched")
            write_summary('Patchright', False, f'Page loaded (title: "{title}") but no price found in text')
        except Exception as e:
            print(f"RESULT: ERROR — {e}")
            write_summary('Patchright', False, f'Error: {e}')
        finally:
            browser.close()


def write_summary(method, success, detail):
    import os
    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        with open(summary_file, 'a') as f:
            icon = '✅' if success else '❌'
            f.write(f'| {method} | {icon} | {detail} |\n')


if __name__ == '__main__':
    main()
