#!/usr/bin/env python3
"""
create_demo_gif.py — generate docs/screenshots/demo.gif

Starts the Archer Flask server in emulator mode, navigates through the
Tier 1 cockpit with Playwright, captures frames, and assembles a GIF
using Pillow.  Writes the GIF to docs/screenshots/demo.gif.

Usage:
    python create_demo_gif.py
"""

import os
import sys
import time
import signal
import subprocess
import threading

os.environ.setdefault('USE_EMULATOR', 'true')
os.environ.setdefault('PORT', '17861')
os.environ.setdefault('ARCHER_SECRET', 'demo_secret_gif_generator')
os.environ.setdefault('ARCHER_OWNER_PIN', '0000')

SERVER_PORT = int(os.environ['PORT'])
BASE        = f'http://127.0.0.1:{SERVER_PORT}'

# ── Ensure Pillow is available ────────────────────────────────────
try:
    from PIL import Image
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'Pillow', '-q'])
    from PIL import Image

# ── Ensure Playwright is available ───────────────────────────────
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'playwright', '-q'])
    from playwright.sync_api import sync_playwright


def _start_server():
    """Launch archer.py in a subprocess; returns (proc, ready_event)."""
    ready = threading.Event()

    def _tail(proc):
        for line in iter(proc.stdout.readline, b''):
            text = line.decode('utf-8', errors='replace').strip()
            if text:
                print(f'  [server] {text}')
            if 'Running on' in text or 'Serving Flask' in text or '7861' in text:
                ready.set()
        ready.set()   # set even if we never saw the banner

    proc = subprocess.Popen(
        [sys.executable, 'archer.py'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    t = threading.Thread(target=_tail, args=(proc,), daemon=True)
    t.start()

    # Wait up to 15s for server to start
    ready.wait(timeout=15)
    time.sleep(1.5)   # give Flask one more beat to bind
    return proc


def _get_bt_token(page):
    """Fetch a boot token so we can navigate straight to /tier1."""
    try:
        resp = page.request.get(f'{BASE}/boot/status?reveal=999')
        data = resp.json()
        return data.get('bt') or data.get('token') or ''
    except Exception:
        return ''


def _capture_frames(page, bt_token):
    """Navigate the cockpit and collect screenshot bytes at each step."""
    frames = []

    def _snap(delay=0):
        if delay:
            time.sleep(delay)
        png = page.screenshot(type='png')
        frames.append(png)

    url = f'{BASE}/tier1'
    if bt_token:
        url += f'?_bt={bt_token}'

    # Block Google Fonts to prevent network hangs
    page.route('https://fonts.googleapis.com/**', lambda r: r.abort())
    page.route('https://fonts.gstatic.com/**',    lambda r: r.abort())

    page.goto(url, wait_until='domcontentloaded', timeout=12000)
    time.sleep(1.5)

    # Boot / splash screen
    _snap()
    time.sleep(0.5)
    _snap()
    time.sleep(0.5)
    _snap()

    # Click DRIVE tab (should already be active, but click to be sure)
    try:
        page.click('text=DRIVE', timeout=3000)
    except Exception:
        pass
    time.sleep(0.6)
    _snap()
    time.sleep(0.8)
    _snap()

    # Click HEALTH tab
    try:
        page.click('text=HEALTH', timeout=3000)
    except Exception:
        pass
    time.sleep(0.6)
    _snap()
    time.sleep(0.6)
    _snap()

    # Click LIVE tab
    try:
        page.click('text=LIVE', timeout=3000)
    except Exception:
        pass
    time.sleep(0.6)
    _snap()

    # Click PERF tab
    try:
        page.click('text=PERF', timeout=3000)
    except Exception:
        pass
    time.sleep(0.6)
    _snap()

    # Back to DRIVE
    try:
        page.click('text=DRIVE', timeout=3000)
    except Exception:
        pass
    time.sleep(0.8)
    _snap()
    time.sleep(0.5)
    _snap()

    return frames


def _png_to_pil(png_bytes):
    import io
    return Image.open(io.BytesIO(png_bytes)).convert('RGBA')


def _build_gif(frames_bytes, output_path, width=390):
    """Convert list of PNG bytes → animated GIF."""
    pil_frames = []
    for png in frames_bytes:
        img = _png_to_pil(png)
        # Resize to consistent width
        h = int(img.height * width / img.width)
        img = img.resize((width, h), Image.LANCZOS)
        # Convert to P (palette) mode for GIF compatibility
        img_rgb = img.convert('RGB')
        img_p   = img_rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        pil_frames.append(img_p)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    pil_frames[0].save(
        output_path,
        format='GIF',
        save_all=True,
        append_images=pil_frames[1:],
        duration=600,      # ms per frame
        loop=0,            # loop forever
        optimize=False,
    )
    size_kb = os.path.getsize(output_path) // 1024
    print(f'  Wrote {output_path} ({len(pil_frames)} frames, {size_kb} KB)')


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'docs', 'screenshots', 'demo.gif')
    print('[create_demo_gif] Starting Archer server in emulator mode...')
    proc = _start_server()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path='/opt/pw-browsers/chromium',
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
            )
            page = browser.new_page(viewport={'width': 390, 'height': 844})

            print('[create_demo_gif] Getting boot token...')
            bt = _get_bt_token(page)

            print('[create_demo_gif] Capturing cockpit frames...')
            frames = _capture_frames(page, bt)
            browser.close()

        print(f'[create_demo_gif] Captured {len(frames)} frames — building GIF...')
        _build_gif(frames, out)
        print('[create_demo_gif] Done.')
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)


if __name__ == '__main__':
    main()
