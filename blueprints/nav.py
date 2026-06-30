from flask import Blueprint, jsonify, request
from archer_state import _limiter, csrf_required

bp = Blueprint('nav', __name__)

def _a():
    import archer
    return archer


@bp.route('/nav/save_place', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def nav_save_place():
    from flask import request as _req
    data    = _req.get_json()
    name    = data.get('name', '').strip().lower()
    lat     = data.get('lat')
    lon     = data.get('lon')
    address = data.get('address', '')
    if not name or lat is None or lon is None:
        return jsonify({'error': 'Need name, lat, lon'}), 400
    a = _a()
    a.nav_places[name] = {'lat': float(lat), 'lon': float(lon), 'address': address}
    a.save_state()
    print(f'[NAV] Saved place "{name}" → {lat},{lon}')
    return jsonify({'ok': True, 'name': name})


@bp.route('/nav/places')
def nav_list_places():
    a = _a()
    return jsonify({k: v for k, v in a.nav_places.items()})
