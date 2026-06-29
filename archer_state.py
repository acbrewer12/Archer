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
_ARCHER_SECRET: str = os.environ.get('ARCHER_SECRET') or _secrets.token_hex(32)
if not os.environ.get('ARCHER_SECRET'):
    print('[SECURITY] WARNING: ARCHER_SECRET not set — using ephemeral random secret. '
          'Sessions will not survive restarts. Set ARCHER_SECRET in archer.env before driving.')

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

# ── SIM FLAGS ─────────────────────────────────────────────────────────────────
# Mutable dict so blueprints can toggle sim noise without global statements.
sim_flags = {'random_enabled': True}
