"""
serial_auth.py — HMAC-authenticated serial protocol for Pi ↔ Arduino communication.

Every message is framed as:
    MSG:<payload>:<nonce>:<tag>\n

Where:
    payload  — comma-separated key=value pairs, e.g. "cmd=HELIX,preset=2"
    nonce    — 8 hex digits (32-bit counter or random, sender-incremented)
    tag      — first 16 hex chars of HMAC-SHA256(secret, payload|nonce)

The receiver validates the tag and rejects replayed nonces (monotonic counter window).
Both sides share the same HMAC secret derived from the HSM master key.
"""

import hmac as _hmac
import hashlib as _hashlib
import os as _os
import secrets as _secrets

# ── Key derivation ────────────────────────────────────────────────────────────
def _get_serial_secret() -> bytes:
    """Return the 32-byte HMAC key for serial authentication.

    Derived from the HSM master key (or ARCHER_SECRET env var) so the key is
    stable across restarts.  Never falls back to a random per-process value —
    that would break Pi/Arduino comms silently after a server restart.
    """
    try:
        from hsm import get_or_create_secret
        base = get_or_create_secret()  # stable machine-bound secret
        return _hmac.new(base.encode(), b'serial-arduino', _hashlib.sha256).digest()
    except Exception as _e:
        print(f'[SERIAL_AUTH] WARNING: HSM unavailable ({_e}), falling back to ARCHER_SECRET')
        raw = _os.environ.get('ARCHER_SECRET', '')
        if not raw:
            raise RuntimeError(
                'Cannot derive serial auth secret: ARCHER_SECRET not set and HSM unavailable.'
            )
        return _hmac.new(raw.encode(), b'serial-arduino', _hashlib.sha256).digest()

_SERIAL_SECRET: bytes = _get_serial_secret()

# ── Tag computation ───────────────────────────────────────────────────────────
def _compute_tag(payload: str, nonce: str) -> str:
    msg = f'{payload}|{nonce}'.encode()
    return _hmac.new(_SERIAL_SECRET, msg, _hashlib.sha256).hexdigest()[:16]

# ── Encoder ───────────────────────────────────────────────────────────────────
def encode_message(payload: str, nonce: str | None = None) -> str:
    """Return a framed, authenticated serial line (without trailing newline).

    payload  — arbitrary string (no colons or newlines)
    nonce    — 8 hex chars; auto-generated if not provided
    """
    if nonce is None:
        nonce = _secrets.token_hex(4)  # 8 hex chars
    if ':' in payload or '\n' in payload:
        raise ValueError('payload must not contain colons or newlines')
    tag = _compute_tag(payload, nonce)
    return f'MSG:{payload}:{nonce}:{tag}'

# ── Decoder / validator ───────────────────────────────────────────────────────
class ReplayGuard:
    """Reject messages whose nonce was already seen (simple sliding-window cache)."""
    def __init__(self, window: int = 256):
        self._seen: set = set()
        self._order: list = []
        self._window = window

    def check_and_record(self, nonce: str) -> bool:
        """Return True if nonce is fresh (not seen before), False if replay."""
        if nonce in self._seen:
            return False
        self._seen.add(nonce)
        self._order.append(nonce)
        if len(self._order) > self._window:
            evicted = self._order.pop(0)
            self._seen.discard(evicted)
        return True

_default_guard = ReplayGuard()

def decode_message(line: str, guard: ReplayGuard | None = None) -> dict:
    """Parse and authenticate a framed serial line.

    Returns {'payload': str, 'nonce': str} on success.
    Raises ValueError with a reason on any failure.
    """
    line = line.strip()
    if not line.startswith('MSG:'):
        raise ValueError('Not a MSG frame')
    parts = line[4:].split(':')
    if len(parts) != 3:
        raise ValueError(f'Expected 3 parts, got {len(parts)}')
    payload, nonce, received_tag = parts
    expected_tag = _compute_tag(payload, nonce)
    if not _hmac.compare_digest(received_tag, expected_tag):
        raise ValueError('Bad HMAC tag — message rejected')
    g = guard or _default_guard
    if not g.check_and_record(nonce):
        raise ValueError(f'Replay detected: nonce {nonce!r} already seen')
    return {'payload': payload, 'nonce': nonce}

# ── Convenience helpers ───────────────────────────────────────────────────────
def parse_payload(payload: str) -> dict:
    """Parse 'key=value,key=value' payload string into a dict."""
    result = {}
    for part in payload.split(','):
        if '=' in part:
            k, _, v = part.partition('=')
            result[k.strip()] = v.strip()
    return result

def make_payload(**kwargs) -> str:
    """Build a payload string from keyword arguments."""
    return ','.join(f'{k}={v}' for k, v in kwargs.items())
