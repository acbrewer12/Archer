#!/usr/bin/env python3
"""Owner PIN storage and verification — shared by the web login and the
console login.

Two very different things need to check the same PIN:

  * archer.py's /login page, once the dashboard is up, and
  * the console login that runs on tty1 BEFORE X starts.

The console one is why this is a separate module rather than staying
inline in archer.py. Chromium on this hardware has no GPU acceleration
(simpledrm is mode-setting only, so it renders through SwiftShader) and
takes many seconds to paint its first frame. A login that waits for
Chromium can't be fast, so the console asks for the PIN first — which
means the check has to run without importing the whole Flask app and its
multi-second dependency graph. This module imports only stdlib.

Both paths therefore share one implementation and one file format. A
second copy of the hashing would be a correctness hazard: change the cost
parameters in one place and the other silently stops matching.

CLI, for the console script:
    pin.py is-configured     -> exit 0 if a PIN is set, 1 if not
    pin.py verify            -> reads the PIN on stdin, exit 0 on match
    pin.py set               -> reads the PIN on stdin, writes it
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

CRED_FILE = os.environ.get('ARCHER_PIN_FILE', '/etc/archer/owner.json')

# Deliberately conservative scrypt cost. n=2**14 with r=8,p=1 is ~16MB and a
# few hundred ms on a weak head-unit CPU — slow enough to make offline
# brute-force of a 6-digit PIN (only a million candidates) expensive, fast
# enough that unlocking the truck does not feel broken. Anyone who pulls the
# USB stick can read this file, so the cost is the only thing protecting it.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def is_configured():
    """True once a PIN has been set. Never raises — callers use this to pick
    between the setup and login screens, and must not 500 on a corrupt file."""
    try:
        with open(CRED_FILE) as f:
            d = json.load(f)
        return bool(d.get('salt') and d.get('hash'))
    except Exception:
        return False


def _hash(pin, salt_bytes):
    return hashlib.scrypt(pin.encode('utf-8'), salt=salt_bytes,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32).hex()


def validate(pin):
    """Policy check only — does not touch disk. Returns (ok, error)."""
    pin = (pin or '').strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 12):
        return False, 'PIN must be 4-12 digits'
    if len(set(pin)) == 1:
        return False, 'PIN cannot be all the same digit'
    return True, ''


def save(pin):
    """Write a new owner PIN. Returns (ok, error)."""
    ok, err = validate(pin)
    if not ok:
        return False, err
    salt = secrets.token_bytes(16)
    payload = {
        'salt':       salt.hex(),
        'hash':       _hash(pin.strip(), salt),
        'created_at': int(time.time()),
        'algo':       f'scrypt-{SCRYPT_N}-{SCRYPT_R}-{SCRYPT_P}',
    }
    try:
        os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
        # temp-file + rename: an ignition-off mid-write must never be able to
        # leave a half-written credential that locks the owner out.
        tmp = CRED_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, CRED_FILE)
        return True, ''
    except OSError as e:
        return False, f'Could not save credential: {e}'


def verify(pin):
    """Constant-time PIN check. Returns bool. Throttling is the caller's job —
    the web login and the console login need different lockout behaviour."""
    try:
        with open(CRED_FILE) as f:
            d = json.load(f)
        salt = bytes.fromhex(d['salt'])
        expected = d['hash']
    except Exception:
        return False
    return hmac.compare_digest(_hash((pin or '').strip(), salt), expected)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'is-configured':
        return 0 if is_configured() else 1
    if cmd == 'verify':
        return 0 if verify(sys.stdin.readline().rstrip('\n')) else 1
    if cmd == 'set':
        ok, err = save(sys.stdin.readline().rstrip('\n'))
        if not ok:
            print(err, file=sys.stderr)
        return 0 if ok else 1
    print(__doc__.strip().splitlines()[-4:][0], file=sys.stderr)
    print('usage: pin.py {is-configured|verify|set}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
