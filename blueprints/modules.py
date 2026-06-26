"""
blueprints/modules.py — Modules, simulator control, emulator, and gatekeeper routes.
All mutable state (truck_state, module_states, etc.) is accessed via late imports
from archer to avoid circular dependency.
"""
import os
from datetime import datetime

from flask import Blueprint, jsonify, Response, redirect, request

from archer_state import sim_flags

bp = Blueprint('modules', __name__)

# ── SIMULATOR SCENARIOS ───────────────────────────────────────────────────────
SIM_SCENARIOS = {
    'idle':       {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 195, 'coolant_temp': 190, 'battery_main': 13.8, 'throttle': 5,  'engine_load': 12, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 165},
    'warmup':     {'rpm': 850,  'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 100, 'coolant_temp': 70,  'battery_main': 14.2, 'throttle': 5,  'engine_load': 15, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 80,  'iat': 70, 'timing': 6.0, 'stft_b1': 6.0, 'stft_b2': 6.0},
    'warm_idle':  {'rpm': 650,  'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 195, 'coolant_temp': 195, 'battery_main': 13.8, 'throttle': 5,  'engine_load': 12, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 170},
    'cruise':     {'rpm': 2000, 'speed': 55, 'boost': 0,  'ethanol': 0, 'oil_temp': 200, 'coolant_temp': 195, 'battery_main': 13.8, 'throttle': 18, 'engine_load': 32, 'gear': 4,   'prndl': 'D', 'tcc_state': 'LOCKED',   'tft': 175, 'timing': 18.0, 'wheel_speed_fl': 55, 'wheel_speed_fr': 55, 'wheel_speed_rl': 55, 'wheel_speed_rr': 55},
    'highway':    {'rpm': 2400, 'speed': 80, 'boost': 0,  'ethanol': 0, 'oil_temp': 205, 'coolant_temp': 200, 'battery_main': 13.9, 'throttle': 22, 'engine_load': 38, 'gear': 4,   'prndl': 'D', 'tcc_state': 'LOCKED',   'tft': 178, 'timing': 20.0, 'wheel_speed_fl': 80, 'wheel_speed_fr': 80, 'wheel_speed_rl': 80, 'wheel_speed_rr': 80},
    'hard_pull':  {'rpm': 5500, 'speed': 80, 'boost': 0,  'ethanol': 0, 'oil_temp': 215, 'coolant_temp': 208, 'battery_main': 13.5, 'throttle': 100,'engine_load': 95, 'gear': 3,   'prndl': 'D', 'tcc_state': 'UNLOCKED', 'tft': 190, 'timing': 28.0, 'stft_b1': -2.0, 'stft_b2': -2.0},
    'wot':        {'rpm': 4500, 'speed': 90, 'boost': 0,  'ethanol': 0, 'oil_temp': 215, 'coolant_temp': 210, 'battery_main': 13.5, 'throttle': 100,'engine_load': 92, 'gear': 3,   'prndl': 'D', 'tcc_state': 'UNLOCKED', 'tft': 188},
    'launch':     {'rpm': 5200, 'speed': 15, 'boost': 0,  'ethanol': 0, 'oil_temp': 220, 'coolant_temp': 215, 'battery_main': 13.2, 'throttle': 100,'engine_load': 98, 'gear': 1,   'prndl': 'D', 'tcc_state': 'UNLOCKED', 'tft': 195},
    'stop':       {'rpm': 0,   'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 210, 'coolant_temp': 205, 'battery_main': 12.6, 'throttle': 0,  'engine_load': 0,  'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 185},
    'cooldown':   {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 230, 'coolant_temp': 220, 'battery_main': 13.8, 'throttle': 5,  'engine_load': 12, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 198},
    'warning':    {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 20, 'oil_temp': 235,'coolant_temp': 225, 'battery_main': 11.8, 'throttle': 5,  'engine_load': 12, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 200},
    'cold_start': {'rpm': 850,  'speed': 0,  'boost': 0,  'ethanol': 0, 'oil_temp': 70,  'coolant_temp': 70,  'battery_main': 14.2, 'throttle': 5,  'engine_load': 15, 'gear': 'P', 'prndl': 'P', 'tcc_state': 'UNLOCKED', 'tft': 70,  'iat': 70, 'timing': 6.0, 'stft_b1': 8.0, 'stft_b2': 8.0},
    'cruise_55':  {'rpm': 2000, 'speed': 55, 'boost': 0,  'ethanol': 0, 'oil_temp': 200, 'coolant_temp': 195, 'battery_main': 13.8, 'throttle': 18, 'engine_load': 32, 'gear': 4,   'prndl': 'D', 'tcc_state': 'LOCKED',   'tft': 175, 'wheel_speed_fl': 55, 'wheel_speed_fr': 55, 'wheel_speed_rl': 55, 'wheel_speed_rr': 55},
    'highway_80': {'rpm': 2400, 'speed': 80, 'boost': 0,  'ethanol': 0, 'oil_temp': 205, 'coolant_temp': 200, 'battery_main': 13.9, 'throttle': 22, 'engine_load': 38, 'gear': 4,   'prndl': 'D', 'tcc_state': 'LOCKED',   'tft': 178, 'wheel_speed_fl': 80, 'wheel_speed_fr': 80, 'wheel_speed_rl': 80, 'wheel_speed_rr': 80},
}


# ── PAGE ROUTES ───────────────────────────────────────────────────────────────

@bp.route('/dashboard')
def dashboard_page():
    if os.path.exists('archer_dashboard.html'):
        with open('archer_dashboard.html', 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html')
    return Response('<html><body style="background:#050508;color:#00e5ff;font-family:monospace;text-align:center;padding:40px">ARCHER DASHBOARD — archer_dashboard.html not found</body></html>', mimetype='text/html')


@bp.route('/mirror')
def mirror_page():
    if os.path.exists('archer_mirror.html'):
        with open('archer_mirror.html', 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html')
    return Response('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">MIRROR — archer_mirror.html not found</body></html>', mimetype='text/html')


@bp.route('/hud')
def hud_page():
    if os.path.exists('archer_hud.html'):
        with open('archer_hud.html', 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html')
    return Response('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">HUD — archer_hud.html not found</body></html>', mimetype='text/html')


@bp.route('/simulator')
def simulator_page():
    return redirect('/modules', code=301)


@bp.route('/modules')
def modules_page():
    if os.path.exists('archer_modules.html'):
        with open('archer_modules.html', 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html')
    return Response('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">MODULES — archer_modules.html not found</body></html>', mimetype='text/html')


# ── MODULE STATUS / CONTROL ───────────────────────────────────────────────────

@bp.route('/modules/status')
def modules_status():
    """Return all module states + all live truck_state values."""
    import archer as _a
    ct = _a.truck_state.get('coolant_temp', 70)
    if ct >= 215:
        engine_state = 'HOT'
    elif ct >= 165:
        engine_state = 'NORMAL'
    elif ct >= 120:
        engine_state = 'WARMING'
    else:
        engine_state = 'COLD START'

    return jsonify({
        'modules':       _a.module_states,
        'truck_state':   _a.truck_state,
        'active_faults': _a.active_faults,
        'engine_state':  engine_state,
        'obd_mode':      _a.obd2_display.get('mode', 'default'),
        'obd_connected': _a.obd2_display.get('connected', False),
        'use_emulator':  _a.USE_EMULATOR,
        'beamng':        _a.beamng_state.get('connected', False),
        'gatekeeper':    _a.gatekeeper_state,
    })


@bp.route('/modules/update', methods=['POST'])
def modules_update():
    """Update truck_state values from modules page controls."""
    import archer as _a
    data = request.get_json() or {}
    updated = {}
    for key, val in data.items():
        if key in _a.truck_state:
            try:
                current = _a.truck_state[key]
                if isinstance(current, bool):
                    _a.truck_state[key] = bool(val)
                elif isinstance(current, float):
                    _a.truck_state[key] = float(val)
                elif isinstance(current, int):
                    _a.truck_state[key] = int(float(val))
                else:
                    _a.truck_state[key] = val
                updated[key] = _a.truck_state[key]
            except (ValueError, TypeError):
                pass
    return jsonify({'ok': True, 'updated': updated})


@bp.route('/modules/fault/inject', methods=['POST'])
def modules_fault_inject():
    """Inject a DTC into the active faults list."""
    import archer as _a
    data = request.get_json() or {}
    code   = data.get('code', '').upper().strip()
    module = data.get('module', 'ECU')
    if not code:
        return jsonify({'error': 'code required'}), 400

    lookup   = _a.DTC_DATABASE.get(code)
    desc     = data.get('desc')     or (lookup[0] if lookup else f'Unknown DTC {code}')
    severity = data.get('severity') or (lookup[1] if lookup else 'medium')

    entry = {
        'code':        code,
        'desc':        desc,
        'module':      module,
        'severity':    severity,
        'injected_at': datetime.now().strftime('%H:%M:%S'),
    }
    _a.active_faults.append(entry)
    _a.add_fault(code, desc, severity)
    return jsonify({'ok': True, 'fault': entry, 'total': len(_a.active_faults)})


@bp.route('/modules/fault/clear', methods=['POST'])
def modules_fault_clear():
    """Clear active faults — all or single code."""
    import archer as _a
    data = request.get_json() or {}
    code = data.get('code')
    if code:
        before = len(_a.active_faults)
        _a.active_faults[:] = [f for f in _a.active_faults if f['code'] != code.upper()]
        _a.fault_codes[:]   = [f for f in _a.fault_codes   if f['code'] != code.upper()]
        return jsonify({'ok': True, 'removed': before - len(_a.active_faults)})
    _a.active_faults.clear()
    _a.fault_codes.clear()
    _a.save_state()
    return jsonify({'ok': True, 'cleared': 'all'})


@bp.route('/modules/drive_cycle', methods=['POST'])
def modules_drive_cycle():
    """Apply a named drive cycle scenario to truck_state."""
    import archer as _a
    data = request.get_json() or {}
    name = data.get('name', '').lower()
    if name not in SIM_SCENARIOS:
        return jsonify({'error': f'Unknown scenario: {name}', 'valid': list(SIM_SCENARIOS.keys())}), 400
    for key, val in SIM_SCENARIOS[name].items():
        if key in _a.truck_state:
            _a.truck_state[key] = val
    sim_flags['random_enabled'] = False
    return jsonify({'ok': True, 'scenario': name})


@bp.route('/modules/module/toggle', methods=['POST'])
def modules_module_toggle():
    """Toggle a module online/offline."""
    import archer as _a
    data = request.get_json() or {}
    mod = data.get('module', '').upper()
    if mod not in _a.module_states:
        return jsonify({'error': f'Unknown module: {mod}'}), 400
    _a.module_states[mod]['online'] = not _a.module_states[mod]['online']
    return jsonify({'ok': True, 'module': mod, 'online': _a.module_states[mod]['online']})


# ── EMULATOR / GATEKEEPER STATUS ──────────────────────────────────────────────

@bp.route('/emulator/status')
def emulator_status():
    """Return current OBD mode: EMULATED / REAL_OBD / DISCONNECTED."""
    import archer as _a
    if _a.obd2_display.get('connected') and _a.obd2_display.get('mode') == 'live':
        mode = 'REAL_OBD'
    elif _a.USE_EMULATOR:
        mode = 'EMULATED'
    else:
        mode = 'DISCONNECTED'
    return jsonify({
        'mode':          mode,
        'use_emulator':  _a.USE_EMULATOR,
        'obd_connected': _a.obd2_display.get('connected', False),
        'obd_mode':      _a.obd2_display.get('mode', 'default'),
        'beamng':        _a.beamng_state.get('connected', False),
    })


@bp.route('/gatekeeper_status')
def gatekeeper_status_route():
    """Return gatekeeper authentication state."""
    import time as _t
    import archer as _a
    state = dict(_a.gatekeeper_state)
    if state.get('session_start'):
        state['session_duration_s'] = round(_t.time() - state['session_start'], 0)
    else:
        state['session_duration_s'] = 0
    lockout = state.get('lockout_until')
    state['locked_out'] = bool(lockout and _t.time() < lockout)
    return jsonify(state)


# ── SIM CONTROL ───────────────────────────────────────────────────────────────

@bp.route('/sim/set', methods=['POST'])
def sim_set():
    """Set individual truck_state values from simulator sliders."""
    import archer as _a
    data = request.get_json() or {}
    allowed    = {'rpm', 'speed', 'boost', 'ethanol', 'oil_temp', 'coolant_temp', 'battery_main', 'battery_aux', 'exhaust'}
    noise_keys = {'oil_temp', 'coolant_temp', 'battery_main'}
    updated = {}
    for key, val in data.items():
        if key in allowed and key in _a.truck_state:
            try:
                _a.truck_state[key] = float(val) if '.' in str(val) else int(val)
                updated[key] = _a.truck_state[key]
            except (ValueError, TypeError):
                pass
    if updated.keys() & noise_keys:
        sim_flags['random_enabled'] = False
    return jsonify({'ok': True, 'updated': updated, 'sim_random': sim_flags['random_enabled']})


@bp.route('/sim/random', methods=['POST'])
def sim_random_toggle():
    """Explicitly enable or disable simulated random noise.
    Body: {"enabled": true} or {"enabled": false}"""
    data = request.get_json() or {}
    sim_flags['random_enabled'] = bool(data.get('enabled', True))
    return jsonify({'ok': True, 'sim_random_enabled': sim_flags['random_enabled']})


@bp.route('/sim/scenario', methods=['POST'])
def sim_scenario():
    """Apply a preset driving scenario to truck_state."""
    import archer as _a
    data = request.get_json() or {}
    name = data.get('name', '').lower()
    if name not in SIM_SCENARIOS:
        return jsonify({'error': f'Unknown scenario: {name}', 'valid': list(SIM_SCENARIOS.keys())}), 400
    for key, val in SIM_SCENARIOS[name].items():
        if key in _a.truck_state:
            _a.truck_state[key] = val
    return jsonify({'ok': True, 'scenario': name, 'state': {k: _a.truck_state[k] for k in SIM_SCENARIOS[name]}})


@bp.route('/sim/status')
def sim_status():
    """Return simulator / OBD status."""
    import archer as _a
    return jsonify({
        'sim_random_enabled': sim_flags['random_enabled'],
        'obd_connected':      _a.obd2_display['connected'],
        'obd_mode':           _a.obd2_display['mode'],
        'rpm':          _a.truck_state['rpm'],
        'speed':        _a.truck_state['speed'],
        'boost':        _a.truck_state['boost'],
        'ethanol':      _a.truck_state['ethanol'],
        'oil_temp':     _a.truck_state['oil_temp'],
        'coolant_temp': _a.truck_state['coolant_temp'],
        'battery_main': _a.truck_state['battery_main'],
    })
