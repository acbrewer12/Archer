"""
blueprints/spotify.py — All Spotify routes extracted from archer.py.

State and helper functions live in archer.py and are accessed via late
import (_a()) to avoid circular dependencies — same pattern as other
blueprints.  CSRF protection comes from archer_state.csrf_required, which
is the single authoritative source shared with archer.py.
"""
import json
import urllib.parse

from flask import Blueprint, jsonify, request

from archer_state import _limiter, csrf_required

bp = Blueprint('spotify', __name__)


def _a():
    import archer as _archer
    return _archer


@bp.route('/spotify/dj', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_dj_toggle():
    a = _a()
    dj = a.dj_state
    dj['enabled'] = not dj['enabled']
    if dj['enabled']:
        dj['last_track_id'] = None
        a.speak("DJ mode on. I've got the intro.")
    else:
        a.speak("DJ mode off.")
    return jsonify({'enabled': dj['enabled']})


@bp.route('/spotify/disconnect')
def spotify_disconnect():
    a = _a()
    a.spotify_tokens['access_token']  = None
    a.spotify_tokens['refresh_token'] = None
    a.spotify_tokens['expires_at']    = 0
    return jsonify({'ok': True})


@bp.route('/spotify/login')
def spotify_login():
    a = _a()
    redirect_uri = a.SPOTIFY_REDIRECT_URI or f'{request.scheme}://{request.host}/spotify/callback'
    params = urllib.parse.urlencode({
        'client_id':     a.SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri':  redirect_uri,
        'scope':         a.SPOTIFY_SCOPES,
        'show_dialog':   'true',
    })
    return json.dumps({'redirect': f'https://accounts.spotify.com/authorize?{params}'}), 200, {'Content-Type': 'application/json'}


@bp.route('/spotify/callback')
def spotify_callback():
    import time, base64, urllib.request as _ureq, uuid as _uuid
    a = _a()
    code  = request.args.get('code')
    error = request.args.get('error')
    if error or not code:
        return f'<h2 style="font-family:monospace;color:#cc0000;background:#000;padding:20px">Spotify auth failed: {error}</h2>'
    if a.spotify_tokens['access_token'] and time.time() < a.spotify_tokens['expires_at']:
        _bt2 = str(_uuid.uuid4())
        a.system_health['boot_tokens'][_bt2] = time.time() + 15
        return (
            f'<html><head><style>body{{background:#000;color:#00cc44;font-family:monospace;'
            f'display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px}}'
            f'</style></head><body><div style="font-size:32px">&#10003;</div>'
            f'<div style="font-size:18px;letter-spacing:3px">ALREADY CONNECTED</div>'
            f'<script>setTimeout(()=>{{window.location.href=\'/display?spotify=ok&_bt={_bt2}\'}},1000)</script>'
            f'</body></html>'
        )
    try:
        creds = base64.b64encode(f"{a.SPOTIFY_CLIENT_ID}:{a.SPOTIFY_CLIENT_SECRET}".encode()).decode()
        redirect_uri = a.SPOTIFY_REDIRECT_URI or f'{request.scheme}://{request.host}/spotify/callback'
        data = urllib.parse.urlencode({
            'grant_type':   'authorization_code',
            'code':          code,
            'redirect_uri':  redirect_uri,
        }).encode()
        req = _ureq.Request(
            'https://accounts.spotify.com/api/token', data=data,
            headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with _ureq.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            a.spotify_tokens['access_token']  = resp['access_token']
            a.spotify_tokens['refresh_token'] = resp.get('refresh_token')
            a.spotify_tokens['expires_at']    = time.time() + resp.get('expires_in', 3600) - 60
            print(f'[SPOTIFY] Authenticated. Scopes: {resp.get("scope")}')
            _bt = str(_uuid.uuid4())
            a.system_health['boot_tokens'][_bt] = time.time() + 15
            return (
                f'<html><head><style>body{{background:#000;color:#00cc44;font-family:monospace;'
                f'display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px}}'
                f'</style></head><body><div style="font-size:32px">&#10003;</div>'
                f'<div style="font-size:18px;letter-spacing:3px">SPOTIFY CONNECTED</div>'
                f'<div style="font-size:12px;color:#444">You can close this tab</div>'
                f'<script>setTimeout(()=>{{window.location.href=\'/display?spotify=ok&_bt={_bt}\'}},1500)</script>'
                f'</body></html>'
            )
    except Exception as e:
        print(f'[SPOTIFY] Token exchange failed: {e}')
        return f'<h2 style="font-family:monospace;color:#cc0000;background:#000;padding:20px">Token exchange failed: {e}</h2>'


@bp.route('/spotify/status')
def spotify_status():
    a = _a()
    if not a.spotify_tokens['access_token']:
        return jsonify({'connected': False})
    data = a.spotify_api('GET', 'me/player')
    if not data:
        return jsonify({'connected': True, 'playing': False, 'track': None})
    item     = data.get('item', {})
    artists  = ', '.join(x['name'] for x in item.get('artists', []))
    album    = item.get('album', {})
    track_id = item.get('id')
    art_url  = a._get_art_cached(track_id, album.get('images', []))
    progress = data.get('progress_ms', 0)
    duration = item.get('duration_ms', 1) or 1
    return jsonify({
        'connected':      True,
        'playing':        data.get('is_playing', False),
        'track':          item.get('name', ''),
        'track_id':       track_id,
        'artist':         artists,
        'album':          album.get('name', ''),
        'art':            art_url,
        'progress':       progress,
        'progress_pct':   round((progress / duration) * 100, 1),
        'duration':       duration,
        'volume':         data.get('device', {}).get('volume_percent', 50),
        'device':         data.get('device', {}).get('name', ''),
        'dj_enabled':     a.dj_state['enabled'],
        'dj_intensity':   a._dj_intensity_level(),
    })


@bp.route('/spotify/play', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_play():
    _a().spotify_api('PUT', 'me/player/play')
    return jsonify({'ok': True})


@bp.route('/spotify/pause', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_pause():
    _a().spotify_api('PUT', 'me/player/pause')
    return jsonify({'ok': True})


@bp.route('/spotify/next', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_next():
    _a().spotify_api('POST', 'me/player/next')
    return jsonify({'ok': True})


@bp.route('/spotify/prev', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_prev():
    _a().spotify_api('POST', 'me/player/previous')
    return jsonify({'ok': True})


@bp.route('/spotify/volume', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_volume():
    vol = int((request.json or {}).get('volume', 50))
    vol = max(0, min(100, vol))
    _a().spotify_api('PUT', f'me/player/volume?volume_percent={vol}')
    return jsonify({'ok': True})


@bp.route('/spotify/seek', methods=['POST'])
@_limiter.limit('60 per minute')
@csrf_required
def spotify_seek():
    pos_ms = max(0, int((request.json or {}).get('position_ms', 0)))
    _a().spotify_api('PUT', f'me/player/seek?position_ms={pos_ms}')
    return jsonify({'ok': True})


@bp.route('/spotify/playlists')
def spotify_playlists():
    a = _a()
    intensity_filter = request.args.get('intensity')
    search_q         = (request.args.get('q') or '').lower().strip()
    try:
        data = a.spotify_api('GET', 'me/playlists?limit=50')
        if not data:
            return jsonify({'playlists': [], 'suggested': None, 'intensity': a._dj_intensity_level()})
        playlists = []
        for p in data.get('items', []):
            try:
                tracks_obj    = p.get('tracks')
                tracks_total  = tracks_obj.get('total') if isinstance(tracks_obj, dict) else None
                name_lower    = p['name'].lower()
                detected_intensity = None
                for lvl, keywords in a.DJ_PLAYLIST_KEYWORDS.items():
                    if any(kw in name_lower for kw in keywords):
                        detected_intensity = lvl
                        break
                if intensity_filter and detected_intensity != intensity_filter:
                    continue
                if search_q and search_q not in name_lower:
                    continue
                playlists.append({
                    'id':        p['id'],
                    'name':      p['name'],
                    'tracks':    tracks_total,
                    'art':       p['images'][0]['url'] if p.get('images') else '',
                    'intensity': detected_intensity,
                })
            except Exception:
                continue
        current_intensity = a._dj_intensity_level()
        suggested = next((pl for pl in playlists if pl.get('intensity') == current_intensity), None)
        return jsonify({
            'playlists':         playlists,
            'total':             len(playlists),
            'intensity':         current_intensity,
            'suggested':         suggested,
            'dj_intensity_mode': a.dj_state.get('intensity_mode', 'auto'),
        })
    except Exception as e:
        print(f'[SPOTIFY] Playlists error: {e}')
        return jsonify({'error': str(e), 'playlists': []}), 500


@bp.route('/spotify/dj/intensity', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def spotify_dj_intensity():
    a = _a()
    mode = (request.json or {}).get('mode', 'auto')
    if mode not in ('auto', 'calm', 'moderate', 'aggressive'):
        return jsonify({'error': 'Invalid mode'}), 400
    a.dj_state['intensity_mode'] = mode
    return jsonify({'intensity_mode': mode, 'current': a._dj_intensity_level()})


@bp.route('/spotify/play_playlist', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def spotify_play_playlist():
    playlist_id = (request.json or {}).get('playlist_id', '')
    if playlist_id and isinstance(playlist_id, str) and len(playlist_id) < 64:
        _a().spotify_api('PUT', 'me/player/play', {'context_uri': f'spotify:playlist:{playlist_id}'})
    return jsonify({'ok': True})


@bp.route('/spotify/queue')
def spotify_queue():
    data = _a().spotify_api('GET', 'me/player/queue')
    if not data:
        return jsonify({'queue': []})
    queue_items = []
    for item in data.get('queue', [])[:8]:
        artists = ', '.join(x['name'] for x in item.get('artists', []))
        queue_items.append({'name': item.get('name', ''), 'artist': artists,
                            'duration': item.get('duration_ms', 0)})
    return jsonify({'queue': queue_items})
