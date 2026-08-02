"""
blueprints/roku.py — Read-only Roku channel endpoints.

Three routes consumed by the Archer Roku channel:
  GET /roku/status      → current vehicle status  (tier ≤ 3)
  GET /roku/drives      → list of past trips       (tier ≤ 3)
  GET /roku/telemetry   → stats for one trip       (tier ≤ 3)

Auth: Authorization: Bearer <archer_auth JWT> — same token the Android app
uses.  No CSRF token required; Roku can't set cookies.  Cookie-based tier
check is accepted as a fallback so the endpoints work in a browser too.
"""
import functools

from flask import Blueprint, jsonify, request

from archer_state import decode_auth_jwt

bp = Blueprint('roku', __name__)


def _a():
    import archer as _ar
    return _ar


def _roku_auth(f):
    """Require tier ≤ 3.  Accepts Bearer JWT or archer_auth cookie."""
    @functools.wraps(f)
    def _w(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            try:
                payload = decode_auth_jwt(auth[7:])
            except Exception:
                return jsonify({'error': 'invalid or expired token'}), 401
            if payload.get('tier', 99) > 3:
                return jsonify({'error': 'Tier 1-3 required'}), 403
            return f(*args, **kwargs)
        # Cookie / MAC-whitelist fallback (browser testing, same-network access)
        tier = _a().get_request_tier(request)
        if tier > 3:
            return jsonify({'error': 'Tier 1-3 required'}), 403
        return f(*args, **kwargs)
    return _w


# ── /roku/status ──────────────────────────────────────────────────────────────

@bp.route('/roku/status')
@_roku_auth
def roku_status():
    """Return a one-line status summary + optional alert string."""
    a    = _a()
    ts   = a.truck_state
    # NOTE: obd2_display['mode'] is only ever 'live'/'default' in archer.py —
    # never 'REAL_OBD'/'EMULATED'/'BEAMNG'/'DISCONNECTED', so checking it here
    # meant this always fell through to 'offline'. Use the same live-computed
    # expression get_display_data() uses for its 'obd_mode' field instead.
    beamng_connected = a.beamng_state.get('connected')
    mode = (
        'BEAMNG' if beamng_connected
        else (('EMULATED' if a.USE_EMULATOR else 'REAL_OBD') if ts['rpm'] > 0 else 'DISCONNECTED')
    )

    speed   = ts.get('speed', 0) or 0
    rpm     = ts.get('rpm', 0) or 0
    coolant = ts.get('coolant_temp', 0) or 0
    oil     = ts.get('oil_temp', 0) or 0
    battery = ts.get('battery_main', 0) or 0
    boost   = ts.get('boost', 0) or 0
    gear    = ts.get('prndl', '')

    if mode == 'REAL_OBD':
        status = 'online'
    elif mode in ('EMULATED', 'SIMULATED', 'BEAMNG'):
        status = 'demo'
    else:
        status = 'offline'

    if status == 'offline':
        message = 'Vehicle offline — OBD not connected'
    elif speed > 1:
        parts = [f'{speed:.0f} mph', f'{rpm:,.0f} RPM']
        if boost > 1:
            parts.append(f'{boost:.1f} psi boost')
        message = '  ·  '.join(parts)
    else:
        parts = [f'{rpm:,.0f} RPM', gear] if gear else [f'{rpm:,.0f} RPM']
        if coolant > 0:
            parts.append(f'{coolant:.0f}°F coolant')
        message = '  ·  '.join(p for p in parts if p)

    alert = None
    if coolant > 230:
        alert = f'HIGH COOLANT TEMP — {coolant:.0f}°F'
    elif oil > 280:
        alert = f'HIGH OIL TEMP — {oil:.0f}°F'
    elif 0 < battery < 11.5:
        alert = f'LOW BATTERY — {battery:.1f}V'
    elif ts.get('airbag_status', '') == 'FAULT':
        alert = 'AIRBAG SYSTEM FAULT'

    return jsonify({'status': status, 'message': message, 'alert': alert})


# ── /roku/drives ──────────────────────────────────────────────────────────────

@bp.route('/roku/drives')
@_roku_auth
def roku_drives():
    """Return the 50 most-recent trips as a JSON array."""
    log    = list(getattr(_a(), 'trip_log', []))
    recent = list(reversed(log[-50:]))
    drives = []
    for i, trip in enumerate(recent):
        date  = trip.get('date', '')
        t     = trip.get('time', '')
        dist  = trip.get('trip_distance', 0) or 0
        dur   = trip.get('duration', 0) or 0
        label = f'{date} {t}  ·  {dist:.0f} mi  ·  {int(dur)} min'
        drives.append({
            'id':            i,
            'label':         label,
            'date':          date,
            'time':          t,
            'duration_min':  round(dur, 1),
            'distance_mi':   round(dist, 2),
            'peak_rpm':      trip.get('peak_rpm', 0),
            'peak_boost':    round(trip.get('peak_boost', 0) or 0, 1),
            'peak_oil_temp': trip.get('peak_oil_temp', 0),
            'peak_coolant':  trip.get('peak_coolant_temp', 0),
            'mpg':           round(trip.get('trip_mpg', 0) or 0, 1),
            'fuel_gal':      round(trip.get('fuel_used_gal', 0) or 0, 3),
            'best_060':      trip.get('best_060'),
            'hard_events':   trip.get('hard_events', 0),
            'quality':       trip.get('quality', ''),
            'road':          trip.get('road', ''),
            'telemetryUrl':  f'/roku/telemetry?id={i}',
        })
    return jsonify(drives)


# ── /roku/telemetry ───────────────────────────────────────────────────────────

@bp.route('/roku/telemetry')
@_roku_auth
def roku_telemetry():
    """Return full stats for one trip by index (0 = most recent)."""
    log    = list(getattr(_a(), 'trip_log', []))
    recent = list(reversed(log[-50:]))
    try:
        idx = int(request.args.get('id', 0))
    except (ValueError, TypeError):
        idx = 0
    if not recent or idx >= len(recent):
        return jsonify({'error': 'trip not found'}), 404
    trip = recent[idx]
    return jsonify({
        'id':            idx,
        'date':          trip.get('date', ''),
        'time':          trip.get('time', ''),
        'duration_min':  round(trip.get('duration', 0) or 0, 1),
        'distance_mi':   round(trip.get('trip_distance', 0) or 0, 2),
        'peak_rpm':      trip.get('peak_rpm', 0),
        'peak_boost':    round(trip.get('peak_boost', 0) or 0, 1),
        'peak_oil_temp': trip.get('peak_oil_temp', 0),
        'peak_coolant':  trip.get('peak_coolant_temp', 0),
        'mpg':           round(trip.get('trip_mpg', 0) or 0, 1),
        'fuel_gal':      round(trip.get('fuel_used_gal', 0) or 0, 3),
        'best_060':      trip.get('best_060'),
        'hard_events':   trip.get('hard_events', 0),
        'quality':       trip.get('quality', ''),
        'road':          trip.get('road', ''),
        'ethanol_pct':   trip.get('ethanol', 0),
        'weather':       trip.get('weather', ''),
        'fault_codes':   trip.get('fault_codes', []),
        # points: reserved for future time-series recording; empty until then
        'points':        [],
    })
