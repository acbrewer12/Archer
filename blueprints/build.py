"""
blueprints/build.py — Build tracker routes extracted from archer.py.
"""

from flask import Blueprint, jsonify, request

from archer_state import _limiter, csrf_required

bp = Blueprint('build', __name__)


def _a():
    import archer
    return archer


# ── BUILD SPECS ───────────────────────────────────────────

@bp.route('/build/update', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def build_update_route():
    a = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Tier 1 required'}), 403
    data = request.get_json() or {}
    bool_keys = {'cold_air_intake', 'long_tube_headers', 'full_exhaust', 'intake_manifold',
                 'throttle_body_upgrade', 'cam_swap', 'heads_upgrade', 'wideband_o2',
                 'electric_fan', 'underdrive_pulley', 'custom_tune'}
    int_keys  = {'cam_level', 'heads_level'}
    for k, v in data.items():
        if k in bool_keys:
            a.build_specs[k] = bool(v)
        elif k in int_keys:
            a.build_specs[k] = int(v)
        elif k in a.build_specs:
            a.build_specs[k] = v
    a.save_state()
    return jsonify({'ok': True, 'build_specs': dict(a.build_specs), 'power': a.estimate_power_from_parts()})


# ── BUILD PART SEARCH ─────────────────────────────────────

@bp.route('/build/part/search')
@_limiter.limit('20 per minute')
def build_part_search():
    a    = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Tier 1 required'}), 403
    name = request.args.get('name', '')
    pn   = request.args.get('pn', '')
    results = a.web_search_parts(name, pn)
    return jsonify({'results': results, 'query_name': name, 'query_pn': pn})


# ── BUILD PART ADD ────────────────────────────────────────

@bp.route('/build/part/add', methods=['POST'])
@_limiter.limit('10 per minute')
@csrf_required
def build_part_add():
    import uuid
    from datetime import datetime
    a    = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Tier 1 required'}), 403
    data = request.get_json() or {}
    part = {
        'id':          str(uuid.uuid4())[:8],
        'name':        data.get('name', 'Unknown Part'),
        'part_number': data.get('part_number', ''),
        'category':    data.get('category', 'other'),
        'hp_gain':     float(data.get('hp_gain', 0)),
        'tq_gain':     float(data.get('tq_gain', 0)),
        'description': data.get('description', ''),
        'status':      data.get('status', 'ordered'),
        'cost':        float(data.get('cost', 0)),
        'date_added':  datetime.now().strftime('%B %d %Y'),
        'notes':       data.get('notes', ''),
    }
    a.build_tracker['parts'].append(part)
    a._recalc_build_spent()
    a.save_state()
    return jsonify({'ok': True, 'part': part, 'power': a.estimate_power_from_parts(),
                    'parts': list(a.build_tracker['parts'])})


# ── BUILD PART UPDATE ─────────────────────────────────────

@bp.route('/build/part/update', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def build_part_update():
    a    = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Tier 1 required'}), 403
    data = request.get_json() or {}
    pid  = data.get('id')
    part = next((p for p in a.build_tracker['parts'] if p.get('id') == pid), None)
    if not part:
        return jsonify({'ok': False, 'error': 'Part not found'})
    for k in ('status', 'hp_gain', 'tq_gain', 'cost', 'notes', 'name', 'part_number'):
        if k in data:
            part[k] = float(data[k]) if k in ('hp_gain', 'tq_gain', 'cost') else data[k]
    a._recalc_build_spent()
    a.save_state()
    return jsonify({'ok': True, 'part': part, 'power': a.estimate_power_from_parts(),
                    'parts': list(a.build_tracker['parts'])})


# ── BUILD PART REMOVE ─────────────────────────────────────

@bp.route('/build/part/remove', methods=['POST'])
@_limiter.limit('10 per minute')
@csrf_required
def build_part_remove():
    a   = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Tier 1 required'}), 403
    pid = (request.get_json() or {}).get('id')
    a.build_tracker['parts'] = [p for p in a.build_tracker['parts'] if p.get('id') != pid]
    a._recalc_build_spent()
    a.save_state()
    return jsonify({'ok': True, 'power': a.estimate_power_from_parts(),
                    'parts': list(a.build_tracker['parts'])})
