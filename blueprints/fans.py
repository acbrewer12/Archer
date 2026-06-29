"""
blueprints/fans.py — Fan page, public register, and fans/ask routes.
All routes here are public (no tier auth required).
"""
import os
import json

from flask import Blueprint, jsonify, Response, request

from archer_state import _limiter, decode_auth_jwt, _ARCHER_SECRET

bp = Blueprint('fans', __name__)


@bp.route('/fans')
@bp.route('/fan')
def fan_page():
    """Public fan page — injects auth context so JS knows if user is signed in."""
    import hashlib as _hl
    user_info = None
    cookie_val = request.cookies.get('archer_auth', '')
    if cookie_val:
        # Try JWT first
        try:
            payload = decode_auth_jwt(cookie_val)
            user_info = {'tier': int(payload['tier']), 'name': payload.get('name', '')}
        except ValueError:
            pass
        # Legacy tier:name:hmac format
        if user_info is None:
            try:
                parts = cookie_val.split(':')
                if len(parts) == 3:
                    c_tier, c_name, c_token = parts
                    expected = _hl.sha256(f'{c_name}{c_tier}{_ARCHER_SECRET}'.encode()).hexdigest()[:32]
                    if c_token == expected:
                        user_info = {'tier': int(c_tier), 'name': c_name}
            except Exception:
                pass
    user_json = json.dumps(user_info) if user_info else 'null'
    if os.path.exists('archer_fan.html'):
        with open('archer_fan.html', 'r', encoding='utf-8') as f:
            html = f.read()
        html = html.replace('</head>', f'<script>window.ARCHER_USER={user_json};</script></head>', 1)
        return Response(html, mimetype='text/html')
    return Response(
        '<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">ARCHER FAN PAGE</body></html>',
        mimetype='text/html',
    )


@bp.route('/register')
def register_page():
    """Registration / sign-in page — linked from fan page."""
    from archer import get_client_mac, registration_page as _reg_page
    mac = get_client_mac(request)
    return _reg_page(mac)


@bp.route('/fans/ask', methods=['POST'])
@_limiter.limit('10 per minute; 60 per hour')
def fans_ask():
    """Public read-only fan Q&A — no commands executed, no TTS, no auth required."""
    try:
        data = request.get_json() or {}
        question = (data.get('question') or data.get('command') or '').strip()
        if not question:
            return jsonify({'response': 'Ask me something about Archer!'})
        from archer import ask_archer
        response = ask_archer(question)
        return jsonify({'response': response or "I'm not sure about that one."})
    except Exception:
        return jsonify({'response': 'Give me a second.'})
