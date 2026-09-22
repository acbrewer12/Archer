"""
blueprints/vehicle.py — Vehicle control routes extracted from archer.py.

Covers: geofence, remote start/stop, Compustar, ambient lighting, Helix DSP,
trailer, parking mode, crash detection, rivalry, voice navigation, and heat soak.
All mutable state and business logic live in archer.py and are accessed via
late import (_a()) to avoid circular dependencies.
"""

import functools

from flask import Blueprint, jsonify, request

from archer_state import _limiter, csrf_required, decode_auth_jwt

bp = Blueprint('vehicle', __name__)


def _a():
    import archer as _ar
    return _ar


def _owner_auth(f):
    """Require tier 1 (Owner). Accepts a Bearer JWT or the archer_auth
    cookie for tier resolution — same pattern as blueprints/roku.py's
    _roku_auth, tightened to tier == 1.

    Without this, a Bearer-only client (the Wear OS app; it has no
    WebView session to share a cookie with) passed csrf_required() fine —
    that decorator already accepts a valid Bearer JWT to satisfy CSRF —
    but then hit get_request_tier(request), which only ever reads the
    cookie, resolved to an unauthenticated tier, and 403'd as "Owner only"
    regardless of what tier the Bearer token actually carried.

    Note: csrf_required() (the outer decorator) also tries to decode any
    Bearer header, to satisfy CSRF specifically. A genuinely invalid Bearer
    token with no session cookie either gets caught by csrf_required's own
    generic CSRF-failure 403 first — this decorator's more specific
    "invalid or expired token" 401 only fires for a valid Bearer that
    doesn't carry tier 1, or for the (unusual) case of a valid session
    cookie/CSRF token present alongside a bad Bearer header. Both paths
    fail closed either way."""
    @functools.wraps(f)
    def _w(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            try:
                payload = decode_auth_jwt(auth[7:])
            except Exception:
                return jsonify({'error': 'invalid or expired token'}), 401
            if payload.get('tier', 99) != 1:
                return jsonify({'error': 'Owner only'}), 403
            return f(*args, **kwargs)
        if _a().get_request_tier(request) > 1:
            return jsonify({'error': 'Owner only'}), 403
        return f(*args, **kwargs)
    return _w


# ── GEOFENCE ──────────────────────────────────────────────

@bp.route('/geofence/add', methods=['POST'])
@csrf_required
def geofence_add():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    data   = request.get_json() or {}
    name   = data.get('name', '').strip()
    lat    = data.get('lat')
    lon    = data.get('lon')
    radius = float(data.get('radius_miles', 0.5))
    if not name or lat is None or lon is None:
        return jsonify({'error': 'name, lat, and lon required'}), 400
    msg = a.add_geofence(name, float(lat), float(lon), radius)
    return jsonify({'ok': True, 'msg': msg, 'count': len(a.geofences)})


@bp.route('/geofence/list')
def geofence_list():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    return jsonify({'geofences': a.geofences, 'count': len(a.geofences), 'inside': list(a._geofence_inside)})


@bp.route('/geofence/remove', methods=['POST'])
@csrf_required
def geofence_remove():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    name = (request.get_json() or {}).get('name', '')
    before = len(a.geofences)
    a.geofences[:] = [f for f in a.geofences if f['name'] != name]
    a.save_state()
    removed = before - len(a.geofences)
    return jsonify({'ok': True, 'removed': removed, 'count': len(a.geofences)})


# ── REMOTE START / STOP ───────────────────────────────────

@bp.route('/remote/start', methods=['POST'])
@csrf_required
@_owner_auth
def remote_start_route():
    a = _a()
    msg = a.remote_start_engine()
    return jsonify({'ok': True, 'status': a.remote_start['status'], 'msg': msg or 'Starting.'})


@bp.route('/remote/stop', methods=['POST'])
@csrf_required
def remote_stop_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    msg = a.remote_stop_engine()
    return jsonify({'ok': True, 'status': a.remote_start['status'], 'msg': msg})


@bp.route('/remote/status')
def remote_status_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    return jsonify({
        'status':        a.remote_start['status'],
        'runtime_mins':  round(a.remote_start['runtime_mins'], 1),
        'auto_off_mins': a.remote_start['auto_off_mins'],
        'started_at':    a.remote_start['started_at'],
        'warm_temp':     a.remote_start['warm_temp'],
        'oil_temp':      a.truck_state.get('oil_temp'),
    })


# ── COMPUSTAR ─────────────────────────────────────────────

@bp.route('/compustar/arm', methods=['POST'])
@csrf_required
def compustar_arm_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    return jsonify({'ok': True, 'msg': a.arm_compustar(), 'armed': a.compustar['armed']})


@bp.route('/compustar/disarm', methods=['POST'])
@csrf_required
def compustar_disarm_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    return jsonify({'ok': True, 'msg': a.disarm_compustar(), 'armed': a.compustar['armed']})


@bp.route('/compustar/trigger', methods=['POST'])
@csrf_required
def compustar_trigger_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    ttype = (request.get_json() or {}).get('type', 'unknown')
    a.compustar_trigger(ttype)
    return jsonify({'ok': True, 'triggered': ttype, 'log_count': len(a.compustar['trigger_log'])})


@bp.route('/compustar/status')
def compustar_status_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    return jsonify({
        'armed':        a.compustar['armed'],
        'disarmed':     a.compustar['disarmed'],
        'shock_sens':   a.compustar['shock_sens'],
        'tilt_sens':    a.compustar['tilt_sens'],
        'panic_active': a.compustar['panic_active'],
        'last_trigger': a.compustar['last_trigger'],
        'trigger_count': len(a.compustar['trigger_log']),
    })


# ── AMBIENT LIGHTING ──────────────────────────────────────

@bp.route('/ambient/set', methods=['POST'])
@csrf_required
def ambient_set_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    data       = request.get_json() or {}
    zone       = data.get('zone', 'all')
    on         = bool(data.get('on', True))
    color      = data.get('color')
    brightness = data.get('brightness')
    msg = a.set_ambient(zone, on, color, brightness)
    return jsonify({'ok': True, 'msg': msg, 'state': a.ambient_lighting})


@bp.route('/ambient/mode', methods=['POST'])
@csrf_required
def ambient_mode_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    mode = (request.get_json() or {}).get('mode', 'off')
    msg  = a.ambient_mode(mode)
    return jsonify({'ok': True, 'msg': msg, 'mode': a.ambient_lighting['mode']})


@bp.route('/ambient/status')
def ambient_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    return jsonify({'zones': a.ambient_lighting['zones'], 'mode': a.ambient_lighting['mode'], 'master': a.ambient_lighting['master']})


# ── HELIX DSP ─────────────────────────────────────────────

@bp.route('/helix/preset', methods=['POST'])
@csrf_required
def helix_preset_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    preset = (request.get_json() or {}).get('preset', '')
    msg    = a.set_helix_preset(preset)
    return jsonify({'ok': True, 'msg': msg, 'preset': a.helix_dsp['preset']})


@bp.route('/helix/sub', methods=['POST'])
@csrf_required
def helix_sub_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    pct = (request.get_json() or {}).get('level', a.helix_dsp['sub_level'])
    msg = a.set_sub_level(pct)
    return jsonify({'ok': True, 'msg': msg, 'sub_level': a.helix_dsp['sub_level']})


@bp.route('/helix/status')
def helix_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    return jsonify({**a.helix_dsp, 'presets': a.HELIX_PRESETS})


# ── TRAILER ───────────────────────────────────────────────

@bp.route('/trailer/connect', methods=['POST'])
@csrf_required
def trailer_connect_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    data   = request.get_json() or {}
    ttype  = data.get('type', '')
    weight = data.get('weight', 0)
    msg    = a.connect_trailer(ttype, weight)
    return jsonify({'ok': True, 'msg': msg, 'trailer': a.trailer})


@bp.route('/trailer/disconnect', methods=['POST'])
@csrf_required
def trailer_disconnect_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    msg = a.disconnect_trailer()
    return jsonify({'ok': True, 'msg': msg, 'trailer': a.trailer})


@bp.route('/trailer/status')
def trailer_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    return jsonify(a.trailer)


# ── PARKING MODE ──────────────────────────────────────────

@bp.route('/parking/activate', methods=['POST'])
@csrf_required
def parking_activate_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    location = (request.get_json() or {}).get('location', '')
    msg      = a.activate_parking_mode(location)
    return jsonify({'ok': True, 'msg': msg, 'parking_mode': a.parking_mode})


@bp.route('/parking/deactivate', methods=['POST'])
@csrf_required
def parking_deactivate_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    msg = a.deactivate_parking_mode()
    return jsonify({'ok': True, 'msg': msg, 'parking_mode': a.parking_mode})


@bp.route('/parking/status')
def parking_status_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    return jsonify({**a.parking_mode, 'surveillance_armed': a.surveillance.get('armed', False)})


# ── CRASH DETECTION ───────────────────────────────────────

@bp.route('/crash/events')
def crash_events_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    return jsonify({
        'enabled':    a.crash_detection['enabled'],
        'threshold_g': a.crash_detection['threshold_g'],
        'last_event': a.crash_detection['last_event'],
        'events':     a.crash_detection['events'][-20:],
        'count':      len(a.crash_detection['events']),
    })


@bp.route('/crash/clear', methods=['POST'])
@csrf_required
def crash_clear_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    a.crash_detection['events']     = []
    a.crash_detection['last_event'] = None
    return jsonify({'ok': True})


# ── RIVALRY ───────────────────────────────────────────────

@bp.route('/rival/set', methods=['POST'])
@csrf_required
def rival_set_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    data = request.get_json() or {}
    msg  = a.set_rival(data.get('name', ''), data.get('et'), data.get('mph'))
    return jsonify({'ok': True, 'msg': msg, 'rivalry': a.rivalry})


@bp.route('/rival/status')
def rival_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    return jsonify(a.rivalry)


@bp.route('/rival/reset', methods=['POST'])
@csrf_required
def rival_reset_route():
    a = _a()
    if a.get_request_tier(request) > 1:
        return jsonify({'error': 'Owner only'}), 403
    a.rivalry.update({'rival': '', 'rival_et': None, 'rival_mph': None, 'active': False, 'wins': 0, 'losses': 0, 'sessions': []})
    a.save_state()
    return jsonify({'ok': True, 'rivalry': a.rivalry})


# ── VOICE NAVIGATION ──────────────────────────────────────

@bp.route('/navigate/start', methods=['POST'])
@csrf_required
def navigate_start_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    data      = request.get_json() or {}
    dest_name = data.get('dest_name', 'destination')
    dest_lat  = data.get('dest_lat')
    dest_lon  = data.get('dest_lon')
    steps     = data.get('steps', [])
    if dest_lat is None or dest_lon is None:
        return jsonify({'error': 'dest_lat and dest_lon required'}), 400
    msg = a.start_navigation(dest_name, dest_lat, dest_lon, steps)
    return jsonify({'ok': True, 'msg': msg, 'steps': len(steps)})


@bp.route('/navigate/stop', methods=['POST'])
@csrf_required
def navigate_stop_route():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Tier 1-2 only'}), 403
    msg = a.stop_navigation()
    return jsonify({'ok': True, 'msg': msg})


@bp.route('/navigate/status')
def navigate_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    idx   = a.nav_session['step_index']
    steps = a.nav_session['steps']
    return jsonify({
        'active':       a.nav_session['active'],
        'dest_name':    a.nav_session['dest_name'],
        'dest_lat':     a.nav_session['dest_lat'],
        'dest_lon':     a.nav_session['dest_lon'],
        'step_index':   idx,
        'total_steps':  len(steps),
        'current_step': steps[idx] if idx < len(steps) else None,
        'eta_mins':     a.nav_session['eta_mins'],
        'started_at':   a.nav_session['started_at'],
    })


# ── HEAT SOAK ─────────────────────────────────────────────

@bp.route('/heat_soak/status')
def heat_soak_status_route():
    a = _a()
    if a.get_request_tier(request) > 3:
        return jsonify({'error': 'Auth required'}), 403
    return jsonify({**a.heat_soak, 'boost_cap_active': a.heat_soak['heat_soak_risk'] == 'critical', 'boost_cap_psi': a._HEAT_SOAK_BOOST_CAP})
