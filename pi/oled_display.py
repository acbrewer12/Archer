#!/usr/bin/env python3
"""
oled_display.py — 1.3" OLED fallback display (SSD1306 / SH1106 I2C)

Runs independently on the Raspberry Pi; shows speed, RPM, and alerts
even when the phone/browser is not available.  Reads Archer's shared
state from /tmp/archer_oled.json (written by archer.py every second).

Wiring (I2C, matches README schematic):
  OLED VCC → Pi pin 1  (3.3V)
  OLED GND → Pi pin 6  (GND)
  OLED SCL → Pi pin 5  (GPIO 3 / SCL1)
  OLED SDA → Pi pin 3  (GPIO 2 / SDA1)

Install dependencies:
  pip install luma.oled pillow

Install as systemd service:
  sudo cp pi/oled_display.py    /opt/archer/oled_display.py
  sudo cp pi/oled_display.service /etc/systemd/system/
  sudo systemctl enable --now oled_display
"""

import json
import os
import sys
import time
import logging
import signal

try:
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306, sh1106
    from luma.core.render import canvas
    from PIL import ImageFont
    LUMA_AVAILABLE = True
except ImportError:
    LUMA_AVAILABLE = False

STATE_FILE    = "/tmp/archer_oled.json"
REFRESH_HZ    = 4           # frames per second
OFFLINE_AFTER = 5.0         # seconds without fresh data → show OFFLINE
I2C_PORT      = 1           # /dev/i2c-1 (Pi GPIO 2/3)
I2C_ADDRESS   = 0x3C        # default SSD1306 address; SH1106 is also 0x3C
DRIVER        = os.environ.get("OLED_DRIVER", "ssd1306")  # or "sh1106"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [OLED] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── state reader ─────────────────────────────────────────────────────

def read_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── layout helpers ───────────────────────────────────────────────────

def _draw_normal(draw, state: dict, width: int, height: int):
    """Standard display: speed big, RPM below, warning stripe if needed."""
    speed   = state.get("speed", 0)
    rpm     = state.get("rpm", 0)
    warning = state.get("warning", False)
    coolant = state.get("coolant_temp", 0)
    mode    = state.get("obd_mode", "LIVE")

    # Row 1 — big speed
    draw.text((0, 0),   "MPH", fill="white")
    draw.text((28, 0),  str(int(speed)), fill="white")

    # Row 2 — RPM
    draw.text((0, 22),  "RPM", fill="white")
    draw.text((28, 22), f"{int(rpm):,}", fill="white")

    # Row 3 — coolant / OBD mode
    draw.text((0, 44),  f"CLT {int(coolant)}°F", fill="white")
    if mode == "EMULATED":
        draw.text((70, 44), "SIM", fill="white")

    # Warning stripe at bottom
    if warning:
        draw.rectangle([(0, height - 10), (width, height)], fill="white")
        draw.text((2, height - 10), "! CHECK ENGINE !", fill="black")


def _draw_offline(draw, width: int, height: int):
    draw.rectangle([(0, 0), (width, height)], outline="white")
    draw.text((10, 18), "ARCHER", fill="white")
    draw.text((10, 34), "OFFLINE", fill="white")


def _draw_stale(draw, width: int, height: int):
    """Shown when archer.py is running but sensor data has stopped updating."""
    draw.rectangle([(0, 0), (width, height)], outline="white")
    draw.text((4, 14), "! NO DATA !", fill="white")
    draw.text((4, 34), "SENSOR LOST", fill="white")


def _draw_boot(draw, width: int, height: int, dots: int):
    draw.text((6, 20), "ARCHER BOOTING" + "." * (dots % 4), fill="white")


# ── main loop ────────────────────────────────────────────────────────

def _init_device():
    if not LUMA_AVAILABLE:
        log.warning("luma.oled not installed — running in stub mode (no display output)")
        return None
    serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
    if DRIVER == "sh1106":
        device = sh1106(serial, rotate=0)
    else:
        device = ssd1306(serial, rotate=0)
    log.info(f"OLED {DRIVER.upper()} @ I2C:{I2C_ADDRESS:#04x} ready — {device.width}×{device.height}")
    return device


def main():
    device = _init_device()

    _running = True
    def _stop(sig, _frame):
        nonlocal _running
        log.info("Shutdown — clearing display")
        if device:
            device.cleanup()
        _running = False
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    log.info(f"OLED display loop starting ({REFRESH_HZ} fps, data from {STATE_FILE})")
    boot_frames   = 0
    last_ts       = 0.0
    interval      = 1.0 / REFRESH_HZ

    while _running:
        t_start = time.monotonic()
        state   = read_state()

        if state is None:
            # No state file yet — show boot animation briefly, then offline
            if boot_frames < REFRESH_HZ * 3:
                boot_frames += 1
                if device:
                    with canvas(device) as draw:
                        _draw_boot(draw, device.width, device.height, boot_frames)
            else:
                if device:
                    with canvas(device) as draw:
                        _draw_offline(draw, device.width, device.height)
        else:
            boot_frames = 0
            ts = state.get("ts", 0)
            age = time.time() - ts
            if device:
                with canvas(device) as draw:
                    if age > OFFLINE_AFTER:
                        _draw_offline(draw, device.width, device.height)
                    elif state.get("stale", False):
                        _draw_stale(draw, device.width, device.height)
                    else:
                        _draw_normal(draw, state, device.width, device.height)

        elapsed = time.monotonic() - t_start
        sleep_for = max(0, interval - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
