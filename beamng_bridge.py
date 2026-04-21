"""
BeamNG Drive → Archer telemetry bridge.

Setup in BeamNG:
  Options → Other → OutGauge
    Host: 127.0.0.1   Port: 4444   Delay: 1

Run alongside archer.py:
  python beamng_bridge.py
"""

import socket
import struct
import time
import threading
import requests

LISTEN_HOST  = '0.0.0.0'
LISTEN_PORT  = 4444
ARCHER_URL   = 'http://127.0.0.1:7860/beamng_data'
POST_INTERVAL = 0.1   # seconds between POSTs
TIMEOUT_SEC  = 3.0    # seconds before marking disconnected

# OutGauge packet — 92 bytes (no optional ID)
# Ref: https://www.lfs.net/programmer/outgauge
OG_FMT    = '<I4sHBBfffffffIIfff16s16s'
OG_SIZE   = struct.calcsize(OG_FMT)   # 92
OG_FMT_ID = OG_FMT + 'i'              # 96 bytes with trailing ID

state = {
    'connected': False,
    'last_rx':   0.0,
    'packets':   0,
    'car':       '',
}

# Latest parsed telemetry — written by UDP thread, read by POST thread
latest = {}
latest_lock = threading.Lock()


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
     disp1, disp2, *_rest) = fields

    # Unit conversions
    speed_mph  = speed_ms  * 2.23694
    boost_psi  = max(0.0, turbo_bar * 14.5038)
    oil_temp_f = oil_temp_c * 9/5 + 32
    eng_temp_f = eng_temp_c * 9/5 + 32
    ethanol_pct = fuel * 100          # placeholder: fuel 0-1 → 0-100%

    # Gear: 0=reverse, 1=neutral, 2=1st, 3=2nd ...
    gear_display = 'R' if gear == 0 else 'N' if gear == 1 else gear - 1

    return {
        'source':      'beamng',
        'car':         car_b.rstrip(b'\x00').decode('ascii', errors='ignore'),
        'rpm':         round(rpm),
        'speed':       round(speed_mph, 1),
        'boost':       round(boost_psi, 1),
        'oil_temp':    round(oil_temp_f, 1),
        'coolant_temp':round(eng_temp_f, 1),
        'throttle':    round(throttle * 100),
        'brake':       round(brake * 100),
        'gear':        gear_display,
        'fuel':        round(fuel * 100, 1),
    }


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
        except socket.timeout:
            if state['connected'] and time.time() - state['last_rx'] > TIMEOUT_SEC:
                state['connected'] = False
                print('[BEAMNG] Disconnected — no data')
        except Exception as e:
            print(f'[BEAMNG] UDP error: {e}')


def post_worker():
    session = requests.Session()
    while True:
        time.sleep(POST_INTERVAL)
        if not state['connected']:
            continue
        with latest_lock:
            payload = dict(latest)
        if not payload:
            continue
        try:
            session.post(ARCHER_URL, json=payload, timeout=0.5)
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
            print(f'[BEAMNG] ● LIVE  car={state["car"]}  '
                  f'{spd:.0f} mph  {rpm:.0f} rpm  {bst:.1f} psi boost  '
                  f'pkts={state["packets"]}')
        else:
            print('[BEAMNG] ○ waiting for BeamNG OutGauge data on port 4444 …')


if __name__ == '__main__':
    print('═' * 54)
    print('  ARCHER ↔ BeamNG Drive Bridge')
    print('  In BeamNG: Options → Other → OutGauge')
    print('  Host: 127.0.0.1   Port: 4444   Delay: 1')
    print('═' * 54)

    threading.Thread(target=udp_listener,  daemon=True).start()
    threading.Thread(target=post_worker,   daemon=True).start()
    threading.Thread(target=status_printer, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[BEAMNG] Bridge stopped.')
