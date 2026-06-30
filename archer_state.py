"""
archer_state.py — Shared mutable state, rate limiter, and CSRF helpers.

Import from here in blueprints to avoid circular imports with archer.py.
All objects here are initialized without a Flask app reference;
archer.py calls _limiter.init_app(display_app) after creating the app.
"""
import os
import hmac as _hmac
import hashlib as _hashlib
import secrets as _secrets
import functools
import threading
import collections
import base64
import json as _json
import uuid as _uuid
import time as _time

from flask import jsonify

# ── RATE LIMITER (init_app pattern — no circular import) ─────────────────────
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri='memory://')
    _LIMITER_AVAILABLE = True
except ImportError:
    _LIMITER_AVAILABLE = False
    class _FakeLimiter:
        def limit(self, *a, **kw): return lambda f: f
        def shared_limit(self, *a, **kw): return lambda f: f
        def init_app(self, *a, **kw): pass
    _limiter = _FakeLimiter()

# ── SHARED SECRET ─────────────────────────────────────────────────────────────
# Single authoritative source so archer.py and all blueprints use the same value.
# Priority: ARCHER_SECRET env var → HSM master.key → fatal error (no random fallback).
_ARCHER_SECRET: str = os.environ.get('ARCHER_SECRET', '')
if not _ARCHER_SECRET:
    try:
        from hsm import get_or_create_secret as _hsm_secret
        _ARCHER_SECRET = _hsm_secret()
        os.environ['ARCHER_SECRET'] = _ARCHER_SECRET
    except Exception as _hsm_err:
        pass
if not _ARCHER_SECRET:
    import sys as _sys
    print('[SECURITY] FATAL: ARCHER_SECRET is not set and the HSM key could not be read or created. '
          'Set ARCHER_SECRET in archer.env or ensure /etc/archer/ is writable. '
          'Refusing to start with an unknown secret.')
    _sys.exit(1)

# ── CSRF (double-submit cookie) ───────────────────────────────────────────────
_csrf_secret = _ARCHER_SECRET.encode()

def _csrf_token_for(session_id: str) -> str:
    return _hmac.new(_csrf_secret, session_id.encode(), _hashlib.sha256).hexdigest()[:32]

def _validate_csrf(req) -> bool:
    """Return True if request carries a valid CSRF token."""
    sid = req.cookies.get('archer_sid', '')
    if not sid:
        print(f'[SECURITY] CSRF_NO_SESSION  {{"path": "{req.path}", "ip": "{req.remote_addr}"}}')
        return False
    expected = _csrf_token_for(sid)
    token    = req.headers.get('X-CSRF-Token', '')
    if not _hmac.compare_digest(token, expected):
        print(f'[SECURITY] CSRF_TOKEN_MISMATCH  {{"path": "{req.path}", "ip": "{req.remote_addr}"}}')
        return False
    return True

def csrf_required(f):
    """Decorator: reject requests missing a valid CSRF token."""
    @functools.wraps(f)
    def _wrapped(*args, **kwargs):
        from flask import request as _r
        if not _validate_csrf(_r):
            return jsonify({'error': 'CSRF validation failed'}), 403
        return f(*args, **kwargs)
    return _wrapped

# ── JWT (HS256, stdlib-only) ──────────────────────────────────────────────────

def _b64url_enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64url_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))

_JWT_HEADER = _b64url_enc(_json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())

def make_auth_jwt(tier: int, name: str, days: int = 30) -> str:
    """Create a signed HS256 JWT for the archer_auth cookie."""
    now = _time.time()
    payload = {
        'tier': int(tier),
        'name': str(name),
        'jti':  str(_uuid.uuid4()),
        'iat':  int(now),
        'exp':  int(now + 86400 * days),
    }
    body = _b64url_enc(_json.dumps(payload, separators=(',', ':')).encode())
    msg  = f'{_JWT_HEADER}.{body}'
    sig  = _b64url_enc(_hmac.new(_csrf_secret, msg.encode(), _hashlib.sha256).digest())
    return f'{msg}.{sig}'

def decode_auth_jwt(token: str) -> dict:
    """Verify and decode an HS256 archer_auth JWT. Raises ValueError on any failure."""
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError('Not a JWT')
    header, body, sig = parts
    msg = f'{header}.{body}'
    expected = _b64url_enc(_hmac.new(_csrf_secret, msg.encode(), _hashlib.sha256).digest())
    if not _hmac.compare_digest(sig, expected):
        raise ValueError('Bad JWT signature')
    try:
        payload = _json.loads(_b64url_dec(body))
    except Exception:
        raise ValueError('Bad JWT payload')
    if 'exp' in payload and _time.time() > payload['exp']:
        raise ValueError('JWT expired')
    return payload

# ── SIM FLAGS ─────────────────────────────────────────────────────────────────
# Mutable dict so blueprints can toggle sim noise without global statements.
sim_flags = {'random_enabled': True}
