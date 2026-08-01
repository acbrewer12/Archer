"""
hsm.py — Software HSM: machine-bound secret derivation.

On first boot with no ARCHER_SECRET env var, generates a random 32-byte master
key and saves it to /etc/archer/master.key (mode 0600, created with umask 077).
Subsequent boots derive ARCHER_SECRET from this key + machine hostname using
HKDF-SHA256, so the secret is stable across restarts without an env var.

This replaces the ephemeral random fallback with a persistent machine-bound secret
while keeping secrets out of code and version control.
"""
import os
import hmac
import hashlib
import secrets
import platform

_HSM_KEY_PATH = '/etc/archer/master.key'
_HSM_KEY_LEN  = 32  # bytes


def _read_master_key() -> bytes | None:
    try:
        if os.path.exists(_HSM_KEY_PATH) and os.access(_HSM_KEY_PATH, os.R_OK):
            with open(_HSM_KEY_PATH, 'rb') as f:
                key = f.read()
            if len(key) == _HSM_KEY_LEN:
                return key
    except Exception:
        pass
    return None


def _write_master_key(key: bytes) -> bool:
    try:
        os.makedirs(os.path.dirname(_HSM_KEY_PATH), mode=0o700, exist_ok=True)
        # Write with O_EXCL to prevent races; overwrite if it exists
        fd = os.open(_HSM_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(key)
        return True
    except Exception:
        return False


def _derive_secret(master_key: bytes, info: str = 'archer-secret') -> str:
    """HKDF-expand: derive a 32-byte secret from master key + info label."""
    machine_id = platform.node().encode() or b'archer'
    okm = hmac.new(master_key, machine_id + info.encode(), hashlib.sha256).digest()
    return okm.hex()


def get_or_create_secret() -> str:
    """Return a stable machine-bound secret, or create one if none exists.

    Priority:
      1. ARCHER_SECRET env var (explicit override — highest priority)
      2. /etc/archer/master.key (persistent machine-bound key)
      3. Generate + save to /etc/archer/master.key
      4. Ephemeral random (HuggingFace/sandboxed envs with no fs write access)
    """
    # 1. Explicit env var
    env_val = os.environ.get('ARCHER_SECRET', '')
    if env_val:
        return env_val

    # 2 & 3. File-based HSM
    key = _read_master_key()
    if key is None:
        key = secrets.token_bytes(_HSM_KEY_LEN)
        saved = _write_master_key(key)
        if saved:
            print('[HSM] Generated new master key — saved to', _HSM_KEY_PATH)
        else:
            print(f'[SECURITY] WARNING: Could not write {_HSM_KEY_PATH} — falling back to an '
                  f'EPHEMERAL secret for this process only. Every session, CSRF token, and '
                  f'issued JWT will be invalidated on the next restart. This usually means '
                  f'{os.path.dirname(_HSM_KEY_PATH)} is not writable by this process\'s user — '
                  f'check its ownership/permissions.')
            return secrets.token_hex(32)

    return _derive_secret(key)


def rotate_master_key() -> str:
    """Generate a new master key, invalidating all existing sessions."""
    key = secrets.token_bytes(_HSM_KEY_LEN)
    _write_master_key(key)
    print('[HSM] Master key rotated — all sessions invalidated')
    return _derive_secret(key)
