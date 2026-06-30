"""
BeamNG Drive → Archer telemetry bridge.

Setup in BeamNG:
  Options → Other → OutGauge
    Host: 127.0.0.1   Port: 4444   Delay: 1

Run alongside archer.py:
  python beamng_bridge.py
"""

import os
import socket
import struct
import time
import threading
import requests

LISTEN_HOST   = '127.0.0.1'   # loopback only — BeamNG and bridge run on same host
LISTEN_PORT   = 4444
ARCHER_URL    = 'http://127.0.0.1:7860/beamng_data'
STATUS_URL    = 'http://127.0.0.1:7860/beamng_status'
BEAMNG_TOKEN  = os.environ.get('BEAMNG_TOKEN', '')
POST_INTERVAL = 0.1    # seconds between POSTs
TIMEOUT_SEC   = 3.0    # seconds before marking disconnected

# Set True if running E85 — BeamNG fuel level ≠ ethanol content
E85_MODE = False

# OutGauge packet — 92 bytes (no optional ID)
# Ref: https://www.lfs.net/programmer/outgauge
OG_FMT    = '<I4sHBBfffffffIIfff16s16s'
OG_SIZE   = struct.calcsize(OG_FMT)   # 92
OG_FMT_ID = OG_FMT + 'i'             # 96 bytes with trailing ID

state = {
    'connected': False,
    'last_rx':   0.0,
    'packets':   0,
    'car':       '',
}

# Latest parsed telemetry — written by UDP thread, read by POST thread
latest = {}
latest_lock = threading.Lock()

# Launch detection state
_launch = {
    'armed':      False,
    'prev_speed': 0.0,
    'prev_rpm':   0.0,
}


def parse_outgauge(data: bytes) -> dict | None:
    try:
        if len(data) == OG_SIZE:
            fields = struct.unpack(OG_FMT, data)
        elif len(data) == OG_SIZE + 4:
            fields = struct.unpack(OG_FMT_ID, data)
        else:
            return None
    except struct.error:
        return None

    (time_ms, car_b, flags, gear, plid,
     speed_ms, rpm, turbo_bar, eng_temp_c,
     fuel, oil_psi_bar, oil_temp_c,
     dash_lights, show_lights,
     throttle, brake, clutch,
     disp1, disp2, *rest) = fields

    # Unit conversions
    speed_mph = speed_ms * 2.23694

    # G-force (OutGauge provides acceleration in m/s² in rest field bytes)
    # rest[0..2] = accX, accY, accZ when OutGauge flag 0x4 is set
    g_lat  = 0.0
    g_long = 0.0
    g_vert = 1.0
    if len(rest) >= 3:
        try:
            g_lat  = round(rest[0] / 9.81, 2)   # lateral G (left/right)
            g_long = round(rest[1] / 9.81, 2)   # longitudinal G (accel/brake)
            g_vert = round(rest[2] / 9.81, 2)   # vertical G
        except (TypeError, IndexError):
            pass

    # Boost: Eaton TVS2300 (LSA) is roots-style positive displacement —
    # boost is linear with RPM, not exponential like a turbo.
    # Formula: boost = max(0, (rpm - 1500) * 0.003 * (throttle/100)), capped at 14 PSI
    if turbo_bar > 0.05:
        # BeamNG is reporting actual boost — use it directly
        boost_psi = max(0.0, (turbo_bar - 1.0) * 14.5038) if turbo_bar > 1.0 else turbo_bar * 14.5038
    else:
        # Estimate from RPM + throttle (roots/Eaton TVS linear model)
        boost_psi = min(14.0, max(0.0, (rpm - 1500) * 0.003 * throttle))

    oil_temp_f = oil_temp_c * 9 / 5 + 32
    eng_temp_f = eng_temp_c * 9 / 5 + 32

    # Ethanol content — E85_MODE flag since BeamNG fuel level ≠ ethanol %
    ethanol_pct = 85 if E85_MODE else 0

    # BeamNG gear: 0=neutral, 1=1st, 2=2nd ... reverse varies by car (often 10+)
    if gear == 0:
        gear_display = 'N'
    elif gear >= 10:
        gear_display = 'R'
    else:
        gear_display = gear

    # Debug raw turbo every 50 packets
    if state['packets'] % 50 == 0:
        print(f'[BEAMNG] raw turbo_bar={turbo_bar:.4f}  boost_psi={boost_psi:.2f}  '
              f'gear={gear_display}  G=({g_lat:.2f},{g_long:.2f},{g_vert:.2f})')

    return {
        'source':       'beamng',
        'car':          car_b.rstrip(b'\x00').decode('ascii', errors='ignore'),
        'rpm':          round(rpm),
        'speed':        round(speed_mph, 1),
        'boost':        round(boost_psi, 1),
        'oil_temp':     round(oil_temp_f, 1),
        'coolant_temp': round(eng_temp_f, 1),
        'throttle':     round(throttle * 100),
        'brake':        round(brake * 100),
        'gear':         gear_display,
        'fuel':         round(fuel * 100, 1),
        'ethanol':      ethanol_pct,
        'g_lat':        g_lat,
        'g_long':       g_long,
        'g_vert':       g_vert,
        'brake_pressure': round(brake * 100),
    }


def _check_launch(parsed: dict):
    """Detect drag launch: speed 0→moving with throttle >80% and RPM >2500.
    POSTs to /launch_timer/start when detected."""
    spd = parsed.get('speed', 0)
    rpm = parsed.get('rpm', 0)
    thr = parsed.get('throttle', 0)
    prev_spd = _launch['prev_speed']

    if prev_spd < 2 and spd >= 2 and thr > 80 and rpm > 2500:
        try:
            requests.post('http://127.0.0.1:7860/drag/launch', json={
                'source': 'beamng_bridge', 'rpm': rpm, 'throttle': thr
            }, timeout=0.3)
            print(f'[BEAMNG] Launch detected — {rpm} RPM  {thr}% throttle')
        except Exception:
            pass

    _launch['prev_speed'] = spd
    _launch['prev_rpm']   = rpm


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    sock.settimeout(1.0)
    print(f'[BEAMNG] Listening on UDP {LISTEN_HOST}:{LISTEN_PORT}')

    while True:
        try:
            data, addr = sock.recvfrom(256)
            parsed = parse_outgauge(data)
            if parsed is None:
                continue
            with latest_lock:
                latest.clear()
                latest.update(parsed)
            state['last_rx'] = time.time()
            state['packets'] += 1
            state['car']     = parsed['car']
            if not state['connected']:
                state['connected'] = True
                print(f'[BEAMNG] Connected — car: {parsed["car"]}')
            _check_launch(parsed)
        except socket.timeout:
            if state['connected'] and time.time() - state['last_rx'] > TIMEOUT_SEC:
                state['connected'] = False
                print('[BEAMNG] Disconnected — no data')
        except Exception as e:
            print(f'[BEAMNG] UDP error: {e}')


def post_worker():
    session = requests.Session()
    headers = {'X-BeamNG-Token': BEAMNG_TOKEN} if BEAMNG_TOKEN else {}
    while True:
        time.sleep(POST_INTERVAL)
        if not state['connected']:
            continue
        with latest_lock:
            payload = dict(latest)
        if not payload:
            continue
        try:
            session.post(ARCHER_URL, json=payload, headers=headers, timeout=0.5)
        except Exception:
            pass


def status_printer():
    while True:
        time.sleep(5)
        if state['connected']:
            with latest_lock:
                spd = latest.get('speed', 0)
                rpm = latest.get('rpm', 0)
                bst = latest.get('boost', 0)
                g_l = latest.get('g_lat', 0)
                g_g = latest.get('g_long', 0)
            print(f'[BEAMNG] ● LIVE  car={state["car"]}  '
                  f'{spd:.0f} mph  {rpm:.0f} rpm  {bst:.1f} psi boost  '
                  f'G=({g_l:.2f}lat, {g_g:.2f}long)  pkts={state["packets"]}')
        else:
            print('[BEAMNG] ○ waiting for BeamNG OutGauge data on port 4444 …')


if __name__ == '__main__':
    print('=' * 54)
    print('  ARCHER <-> BeamNG Drive Bridge')
    print('  In BeamNG: Options -> Other -> OutGauge')
    print('  Host: 127.0.0.1   Port: 4444   Delay: 1')
    print(f'  E85_MODE: {E85_MODE}')
    print('=' * 54)

    threading.Thread(target=udp_listener,   daemon=True).start()
    threading.Thread(target=post_worker,    daemon=True).start()
    threading.Thread(target=status_printer, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[BEAMNG] Bridge stopped.')
