#!/usr/bin/env python3
"""
key_manager.py — Generates and manages OBD auth keys for the Archer gatekeeper.

Usage:
  python key_manager.py generate_owner        # creates permanent owner key
  python key_manager.py generate_mechanic 24  # creates 24-hour mechanic key
  python key_manager.py generate_readonly      # creates monitoring-only key
  python key_manager.py list                  # shows all active keys
  python key_manager.py revoke <key_id>       # revokes a key

Keys stored in /etc/archer/keys/ (one JSON file per key).
Master key stored in /etc/archer/obd_auth.key (hex string).
"""

import json
import os
import secrets
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

KEYS_DIR   = Path("/etc/archer/keys")
MASTER_KEY = Path("/etc/archer/obd_auth.key")


def _ensure_dirs():
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_KEY.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    path.chmod(0o600)


def generate_owner_key() -> str:
    """
    Generate a permanent 32-byte owner key.  Writes the raw hex to
    /etc/archer/obd_auth.key and metadata to /etc/archer/keys/owner.json.
    Returns the key hex string.
    """
    _ensure_dirs()
    key_bytes = secrets.token_bytes(32)
    key_hex   = key_bytes.hex()

    # Write master key file (this is the shared secret used for HMAC)
    MASTER_KEY.write_text(key_hex + "\n")
    MASTER_KEY.chmod(0o600)

    meta = {
        "id":          "owner",
        "type":        "OWNER",
        "key_hex":     key_hex,
        "created":     _now_iso(),
        "expires":     None,
        "description": "Permanent owner key — full access",
        "permissions": ["read_all", "write_all", "admin"],
    }
    _write_json(KEYS_DIR / "owner.json", meta)

    print(f"[key_manager] Owner key written to {MASTER_KEY}")
    print(f"[key_manager] Metadata: {KEYS_DIR / 'owner.json'}")
    return key_hex


def generate_mechanic_key(expiry_hours: int = 24) -> str:
    """
    Generate a temporary mechanic key that expires after expiry_hours.
    Returns the key hex string.
    """
    _ensure_dirs()
    key_bytes  = secrets.token_bytes(32)
    key_hex    = key_bytes.hex()
    ts         = int(datetime.now(timezone.utc).timestamp())
    filename   = f"mechanic_{ts}.json"
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expiry_hours)).isoformat()

    meta = {
        "id":          f"mechanic_{ts}",
        "type":        "MECHANIC",
        "key_hex":     key_hex,
        "created":     _now_iso(),
        "expires":     expires_at,
        "description": f"Mechanic key — valid for {expiry_hours} hours",
        "permissions": ["read_all", "write_non_security"],
    }
    _write_json(KEYS_DIR / filename, meta)

    print(f"[key_manager] Mechanic key written to {KEYS_DIR / filename}")
    print(f"[key_manager] Expires: {expires_at}")
    return key_hex


def generate_readonly_key() -> str:
    """
    Generate a monitoring-only key with no write permissions.
    Returns the key hex string.
    """
    _ensure_dirs()
    key_bytes = secrets.token_bytes(32)
    key_hex   = key_bytes.hex()
    ts        = int(datetime.now(timezone.utc).timestamp())
    filename  = f"readonly_{ts}.json"

    meta = {
        "id":          f"readonly_{ts}",
        "type":        "READONLY",
        "key_hex":     key_hex,
        "created":     _now_iso(),
        "expires":     None,
        "description": "Read-only monitoring key",
        "permissions": ["read_all"],
    }
    _write_json(KEYS_DIR / filename, meta)

    print(f"[key_manager] Read-only key written to {KEYS_DIR / filename}")
    return key_hex


def list_keys():
    """Print all keys in /etc/archer/keys/, marking expired ones."""
    if not KEYS_DIR.exists():
        print("[key_manager] No keys directory found — run generate_owner first.")
        return

    key_files = sorted(KEYS_DIR.glob("*.json"))
    if not key_files:
        print("[key_manager] No keys found.")
        return

    now = datetime.now(timezone.utc)
    print(f"{'ID':<30} {'TYPE':<12} {'CREATED':<26} {'EXPIRES':<26} STATUS")
    print("-" * 110)
    for path in key_files:
        try:
            meta    = _load_json(path)
            key_id  = meta.get("id", path.stem)
            ktype   = meta.get("type", "UNKNOWN")
            created = meta.get("created", "?")
            expires = meta.get("expires")

            if expires is None:
                status  = "PERMANENT"
                exp_str = "never"
            else:
                exp_dt  = datetime.fromisoformat(expires)
                exp_str = expires
                status  = "EXPIRED" if now > exp_dt else "ACTIVE"

            print(f"{key_id:<30} {ktype:<12} {created:<26} {exp_str:<26} {status}")
        except Exception as e:
            print(f"  {path.name}: ERROR reading file — {e}")


def revoke_key(key_id: str):
    """Delete the key file for the given key_id."""
    if not KEYS_DIR.exists():
        print(f"[key_manager] Keys directory not found.")
        sys.exit(1)

    # Search by id field first, then by filename stem
    for path in KEYS_DIR.glob("*.json"):
        try:
            meta = _load_json(path)
        except Exception:
            continue
        if meta.get("id") == key_id or path.stem == key_id:
            path.unlink()
            print(f"[key_manager] Revoked key {key_id!r} ({path.name} deleted)")
            return

    print(f"[key_manager] Key {key_id!r} not found.")
    sys.exit(1)


def verify_key_type(key_bytes: bytes) -> dict:
    """
    Load all key files and match key_bytes against stored keys.

    Returns a dict:
      { "found": True, "id": ..., "type": ..., "permissions": [...], "expired": bool }
    or
      { "found": False }
    """
    if not KEYS_DIR.exists():
        return {"found": False}

    key_hex = key_bytes.hex()
    now     = datetime.now(timezone.utc)

    for path in KEYS_DIR.glob("*.json"):
        try:
            meta = _load_json(path)
        except Exception:
            continue

        if meta.get("key_hex", "").lower() != key_hex.lower():
            continue

        expires = meta.get("expires")
        expired = False
        if expires is not None:
            expired = now > datetime.fromisoformat(expires)

        return {
            "found":       True,
            "id":          meta.get("id"),
            "type":        meta.get("type"),
            "permissions": meta.get("permissions", []),
            "expired":     expired,
            "description": meta.get("description", ""),
        }

    return {"found": False}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _usage()

    cmd = args[0]

    if cmd == "generate_owner":
        generate_owner_key()

    elif cmd == "generate_mechanic":
        hours = int(args[1]) if len(args) > 1 else 24
        generate_mechanic_key(hours)

    elif cmd == "generate_readonly":
        generate_readonly_key()

    elif cmd == "list":
        list_keys()

    elif cmd == "revoke":
        if len(args) < 2:
            print("Usage: key_manager.py revoke <key_id>")
            sys.exit(1)
        revoke_key(args[1])

    else:
        print(f"Unknown command: {cmd!r}")
        _usage()
