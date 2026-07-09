"""
Test 3 of 3: FlareSolverr — not a browser library like the other two,
a dedicated proxy SERVICE purpose-built for exactly one job: solving
Cloudflare's challenge and handing back clean HTML + cookies. Runs as
its own container (started as a GitHub Actions "service" alongside
this job — see the workflow file); this script just sends it a
request over HTTP and reads back what it got.
"""
import re
import os
import requests

URL = 'https://www.jamesonealchryslerdodgejeep.com/inventory/used-2004-gmc-sierra-2500hd-slt-4wd-4d-crew-cab-1gthk23u64f251261/'
FLARESOLVERR_ENDPOINT = 'http://localhost:8191/v1'

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
    try:
        response = requests.post(FLARESOLVERR_ENDPOINT, json={
            'cmd': 'request.get',
            'url': URL,
            'maxTimeout': 60000,
        }, timeout=70)
        response.raise_for_status()
        result = response.json()

        if result.get('status') != 'ok':
            print(f"RESULT: FlareSolverr itself reported an error — {result.get('message')}")
            write_summary('FlareSolverr', False, f"FlareSolverr error: {result.get('message')}")
            return

        solution = result['solution']
        title = solution.get('title', '')
        html = solution.get('response', '')
        print(f"PAGE TITLE: {title}")

        if 'just a moment' in title.lower():
            print("RESULT: BLOCKED — still shows Cloudflare challenge page")
            write_summary('FlareSolverr', False, f'Blocked — title was "{title}"')
            return

        for pattern in PRICE_PATTERNS:
            m = re.search(pattern, html)
            if m:
                price = m.group(1).replace(',', '')
                print(f"RESULT: SUCCESS — found price ${price}")
                write_summary('FlareSolverr', True, f'Found ${price}, page title: "{title}"')
                return

        print("RESULT: PAGE LOADED but no price pattern matched in returned HTML")
        write_summary('FlareSolverr', False, f'Page loaded (title: "{title}") but no price found in HTML')
    except Exception as e:
        print(f"RESULT: ERROR — {e}")
        write_summary('FlareSolverr', False, f'Error: {e}')


if __name__ == '__main__':
    main()
