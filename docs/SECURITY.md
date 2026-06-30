# Archer Security Model

## Threat Model

Archer runs in two distinct environments with very different trust profiles:

| Context | Network | Hardware | Auth method |
|---|---|---|---|
| **Pi hotspot** | Private 192.168.4.x | Real OBD, Arduino, GPIO | MAC whitelist → JWT cookie |
| **HuggingFace Space** | Public internet | None (read-only) | Owner PIN → JWT cookie |

Attacker goals and mitigations:

- **Gain Tier 1 (owner) access** → 6-digit rate-limited code (5/min), CSRF double-submit, JWT revocation
- **Forge a session** → HS256 JWT signed with HSM-derived secret; tampered tokens rejected at decode
- **Hijack another user's cookie** → HttpOnly + SameSite=Lax; Secure flag set automatically when USE_TLS=true
- **Replay a serial command** → HMAC-SHA256 tag + 256-nonce sliding window in `serial_auth.py`
- **SSID spoof the truck hotspot** → Exact SSID match only (`ARCHER-2500HD`) in `helpers.js`
- **Abuse the terminal** → Explicit allowlist (ps, df, free, uptime, cat, journalctl, curl→localhost, systemctl status, python3 -m json.tool); all other commands rejected 403
- **Inject via BeamNG bridge** → Shared token (`BEAMNG_TOKEN`) + numeric bounds check on all telemetry fields

## Tier System

```
Tier 1 — Owner (Ayden)
  Full access: terminal, device management, secret rotation,
  trip log export, code generation/revocation, all API endpoints.

Tier 2 — Passenger (e.g. Khloe)
  Dashboard view, music/media controls, notify-Tier-1 requests.
  Cannot access terminal, device list, or revoke codes.

Tier 3 — Family
  Read-only dashboard, limited controls.

Tier 4 — Valet
  Speed display only. Blocked from all config and state endpoints.
```

## HSM Key Rotation

The HSM master key lives at `/etc/archer/master.key`. The HMAC-SHA256
`ARCHER_SECRET` and serial auth key are derived from it via HKDF-SHA256.

**To rotate (from the in-cab terminal):**
```
rotate-secrets
```

This calls `hsm.rotate_master_key()`, writes a new master key, re-derives
`ARCHER_SECRET`, invalidates all active JWT sessions (clears `_revoked_tokens`
and `_revoked_names`), and returns immediately. All logged-in users must
re-authenticate. The Pi/Arduino serial link auto-reconnects because both sides
re-derive the key from the same HSM.

**To rotate manually on the Pi:**
```bash
python3 -c "from hsm import rotate_master_key; rotate_master_key()"
sudo systemctl restart archer
```

## Active Session Audit

From the Tier 1 dashboard → **Devices** tab:
- See all registered MACs, their tier, and last-seen timestamp
- Deregister a device to revoke its session immediately (JWT revocation by name)

Via API (Tier 1 JWT required):
```
GET /registered_devices
```

## Pi Compromise Response

If the Pi is compromised:
1. Rotate secrets (above) — invalidates all sessions derived from the old key
2. Deregister all MACs from the dashboard
3. Change `ARCHER_PI_TOKEN` and `BEAMNG_TOKEN` in archer.env
4. Restart archer: `sudo systemctl restart archer`

The HuggingFace Space is read-only and has no Pi credentials — compromise of
the Space does not grant access to the Pi.

## HuggingFace Deployment (Read-Only)

The HF Space runs the same `archer.py` but with these differences:
- No ARP-based MAC auth (no network access to client layer-2)
- Owner PIN (`ARCHER_OWNER_PIN`) is the only Tier 1 auth path
- `_IS_PI = False` disables serial, GPIO, OBD, Arduino, BeamNG, and OLED
- `/terminal/pi_register` and `/terminal/pi_status` are present but the Pi
  does not connect to the public HF URL — Pi connects only to the local server

## What `rotate-secrets` Does

```python
1. hsm.rotate_master_key() → overwrites /etc/archer/master.key
2. ARCHER_SECRET re-derived from new key
3. _revoked_tokens.clear() — all active JWTs are now invalid
4. _revoked_names.clear()  — name-level revocations cleared
5. archer_state._ARCHER_SECRET updated in-process (no restart needed)
```

All currently-logged-in users will get 401 on their next request and be
redirected to the sign-in page.
