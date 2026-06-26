"""
archer_state.py — Shared mutable state and rate limiter.

Import from here in blueprints to avoid circular imports with archer.py.
All objects here are initialized without a Flask app reference;
archer.py calls _limiter.init_app(display_app) after creating the app.
"""
import threading
import collections

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

# ── SIM FLAGS ─────────────────────────────────────────────────────────────────
# Mutable dict so blueprints can toggle sim noise without global statements.
sim_flags = {'random_enabled': True}
