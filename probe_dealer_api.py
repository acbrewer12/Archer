"""
Dealer API probe — finds the underlying inventory API for a dealer site
so prices can be fetched without going through Cloudflare.

Most dealer sites are built on one of a handful of DMS/website platforms
(DealerInspire/CDK, Dealer.com/Cox, DealerSocket, Tekion, etc.) that all
expose inventory via APIs that are NOT behind the same Cloudflare rules as
the consumer-facing page. This script:

  1. Fetches the page HTML via curl-cffi (TLS impersonation, bypasses lighter
     Cloudflare checks) and parses it for platform signals and inline data.
  2. Probes ~20 known API endpoint patterns across all common dealer platforms.
  3. Tries VIN-based inventory API endpoints for DealerInspire WordPress REST,
     CDK / Dealer.com feeds, DealerSocket, Tekion, and generic REST patterns.
  4. Prints a ranked list of endpoints that responded with data so you can
     add the working one to check_prices.py.

Usage:
  python probe_dealer_api.py <listing-url> [vin]

  vin is optional — extracted from the URL automatically if the URL ends with
  the VIN (most DealerInspire and CDK sites do).

Example:
  python probe_dealer_api.py \
    'https://www.jamesonealchryslerdodgejeep.com/inventory/used-2004-gmc-sierra-2500hd-slt-4wd-4d-crew-cab-1gthk23u64f251261/'
"""

import json
import re
import sys
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_req
except ImportError:
    raise SystemExit('curl-cffi required: pip install curl-cffi')

IMPERSONATE = 'chrome124'
TIMEOUT     = 20

# ── Helpers ───────────────────────────────────────────────────────────────────

def get(url: str) -> tuple:
    """Returns (status_code, content_type, body_text). Never raises."""
    try:
        with cffi_req.Session(impersonate=IMPERSONATE) as s:
            r = s.get(url, timeout=TIMEOUT, allow_redirects=True,
                      headers={'Accept': 'application/json, text/html, */*',
                               'Accept-Language': 'en-US,en;q=0.9'})
            return r.status_code, r.headers.get('content-type', ''), r.text
    except Exception as e:
        return 0, '', str(e)


def looks_like_data(body: str, ct: str) -> bool:
    """True when the response looks like real inventory data, not an error page."""
    if not body or len(body) < 40:
        return False
    ct_lower = ct.lower()
    if 'json' in ct_lower:
        try:
            json.loads(body)
            return True
        except Exception:
            pass
    if 'xml' in ct_lower and '<?xml' in body[:20]:
        return True
    if 'html' in ct_lower and 'just a moment' in body[:2000].lower():
        return False
    return False


def extract_price(body: str) -> str:
    """Best price string found in the response, for display."""
    patterns = [
        r'"price"\s*:\s*"?([\d,\.]+)"?',
        r'"listPrice"\s*:\s*"?([\d,\.]+)"?',
        r'"sellingPrice"\s*:\s*"?([\d,\.]+)"?',
        r'\$([\d]{2,3},\d{3})',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return m.group(1)
    return '(price field not obvious — check body)'


def vin_from_url(url: str) -> str:
    """Extract VIN from the URL path if present (17 alphanumeric chars)."""
    path = urlparse(url).path
    m = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', path, re.IGNORECASE)
    return m.group(1).upper() if m else ''


def origin(url: str) -> str:
    p = urlparse(url)
    return f'{p.scheme}://{p.netloc}'


# ── Platform probe suites ─────────────────────────────────────────────────────

def endpoints_to_try(base: str, vin: str, listing_url: str) -> list:
    """Returns a list of (label, url) pairs to probe."""
    probes = []

    # ── DealerInspire (WordPress-based CDK sites) ──────────────────────────
    # Most common URL pattern for CDK/DealerInspire inventory sites.
    probes += [
        ('DealerInspire WP REST — by VIN',
         f'{base}/wp-json/di/v1/inventory/?vin={vin}'),
        ('DealerInspire WP REST — all inventory',
         f'{base}/wp-json/di/v1/inventory/?per_page=1'),
        ('DealerInspire WP REST — root',
         f'{base}/wp-json/di/v1/'),
        ('WordPress REST root',
         f'{base}/wp-json/wp/v2/'),
    ]

    # ── CDK Drive / Dealer.com (Cox Automotive) ───────────────────────────
    probes += [
        ('CDK inventory feed (JSON)',
         f'{base}/feeds/inventory.json'),
        ('CDK inventory feed (XML)',
         f'{base}/feeds/inventory.xml'),
        ('Cox/Dealer.com VIN lookup',
         f'{base}/api/inventory/{vin}'),
        ('Cox/Dealer.com inventory search',
         f'{base}/api/inventory?vin={vin}'),
        ('Dealer.com vehicle data',
         f'{base}/vehicle/{vin}/data'),
    ]

    # ── DealerSocket / Solera ─────────────────────────────────────────────
    probes += [
        ('DealerSocket inventory JSON',
         f'{base}/Inventory/GetInventory?vin={vin}'),
        ('DealerSocket inventory search',
         f'{base}/Inventory/Search?vin={vin}&format=json'),
    ]

    # ── Tekion ────────────────────────────────────────────────────────────
    probes += [
        ('Tekion vehicle detail',
         f'{base}/api/v1/vehicles/{vin}'),
        ('Tekion inventory',
         f'{base}/api/v1/inventory?vin={vin}'),
    ]

    # ── Generic / fallback patterns ───────────────────────────────────────
    probes += [
        ('Generic /inventory.json',
         f'{base}/inventory.json'),
        ('Generic /inventory/api',
         f'{base}/inventory/api?vin={vin}'),
        ('Sitemap (reveals platform)',
         f'{base}/sitemap.xml'),
        ('Sitemap index',
         f'{base}/sitemap_index.xml'),
        ('robots.txt (reveals paths)',
         f'{base}/robots.txt'),
    ]

    # ── JSON-LD / meta in the listing page itself ─────────────────────────
    probes += [
        ('Listing page (JSON-LD / meta scan)',
         listing_url),
    ]

    return probes


# ── Page source analysis ──────────────────────────────────────────────────────

def analyse_page_source(body: str, base: str) -> list:
    """
    Parse the page HTML for:
    - Platform signals (generator meta, script src patterns)
    - Any XHR/fetch URLs hardcoded in JS bundles
    - Inline JSON with price data
    Returns a list of strings to print.
    """
    findings = []

    # Generator / platform meta
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
                    body, re.IGNORECASE)
    if gen:
        findings.append(f'  Generator: {gen.group(1)}')

    # Script src patterns that reveal the platform
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', body, re.IGNORECASE)
    for src in scripts:
        for kw in ('dealerinspire', 'dealer.com', 'dealersocket', 'tekion',
                   'cdk', 'cobalt', 'sincro', 'foxdealer', 'autotrader',
                   'dealerfire', 'edealer', 'motoinsight', 'gubagoo'):
            if kw in src.lower():
                findings.append(f'  Platform signal in script src: {src[:100]}')
                break

    # Inline fetch/XHR API calls in JS
    api_patterns = re.findall(
        r'(?:fetch|axios\.get|\.ajax)\(["\']([^"\']{10,})["\']', body)
    for ap in api_patterns[:10]:
        if any(kw in ap.lower() for kw in ('inventory', 'vehicle', 'price', 'api')):
            findings.append(f'  Inline API call found: {ap[:120]}')

    # JSON-LD blocks
    for ld in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            body, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(ld)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                offers = item.get('offers', {})
                if isinstance(offers, dict) and offers.get('price'):
                    findings.append(f'  JSON-LD price found: ${offers["price"]} '
                                    f'(currency: {offers.get("priceCurrency","?")})')
        except Exception:
            pass

    # Inline price data in JS variables
    price_vars = re.findall(r'["\']?(?:price|listPrice|sellingPrice)["\']?\s*:\s*["\']?([\d,]{4,})',
                            body, re.IGNORECASE)
    for pv in price_vars[:5]:
        findings.append(f'  Inline price value: ${pv}')

    return findings


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    listing_url = sys.argv[1].rstrip('/')
    vin         = sys.argv[2] if len(sys.argv) > 2 else vin_from_url(listing_url)
    base        = origin(listing_url)

    print(f'\nProbing: {listing_url}')
    print(f'VIN:     {vin or "(not found in URL)"}')
    print(f'Base:    {base}\n')

    hits = []

    probes = endpoints_to_try(base, vin, listing_url)
    for label, url in probes:
        status, ct, body = get(url)
        marker = ''

        if status == 200 and looks_like_data(body, ct):
            price_hint = extract_price(body)
            marker = f'  ✅ HIT  [{status}] {ct[:40]}  price≈ {price_hint}'
            hits.append((label, url, body[:400]))
        elif status == 200:
            # 200 but looks like an error/challenge page
            title_m = re.search(r'<title[^>]*>(.*?)</title>', body[:2000],
                                 re.IGNORECASE | re.DOTALL)
            title = title_m.group(1).strip()[:60] if title_m else ''
            marker = f'  ⚠  200 but not data  title="{title}"'

            # Still analyse the listing page for platform clues
            if url == listing_url:
                findings = analyse_page_source(body, base)
                if findings:
                    print(f'  Analysing listing page source for platform clues:')
                    for f in findings:
                        print(f)
        elif status == 0:
            marker = f'  ✗  connection error / timeout'
        else:
            marker = f'  ✗  HTTP {status}'

        print(f'{label}')
        print(f'  {url}')
        print(marker)
        print()

    print('─' * 60)
    if hits:
        print(f'\n✅ {len(hits)} working API endpoint(s) found:\n')
        for label, url, snippet in hits:
            print(f'  {label}')
            print(f'  {url}')
            print(f'  Response snippet: {snippet[:200]}\n')
    else:
        print('\n❌ No unprotected API endpoints found.')
        print('   This site serves all inventory data through the Cloudflare-protected page.')
        print('   ZenRows (paid residential proxy) is the only reliable option for this site.\n')


if __name__ == '__main__':
    main()
