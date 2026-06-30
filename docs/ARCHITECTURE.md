# Archer Architecture

## Component Map

```
┌──────────────────────────────────────────────────────────────────┐
│  Raspberry Pi (2006 GMC Sierra 2500HD)                           │
│                                                                  │
│  archer.py (Flask, port 7860)                                    │
│  ├── blueprints/terminal.py  — terminal, log stream, Pi tunnel   │
│  ├── archer_state.py         — JWT, CSRF, rate limiting          │
│  ├── hsm.py                  — master.key → HKDF secret          │
│  ├── db.py                   — SQLite WAL persistence            │
│  ├── serial_auth.py          — HMAC serial protocol              │
│  └── beamng_bridge.py        — BeamNG UDP → Flask relay          │
│                                                                  │
│  Hardware                                                        │
│  ├── OBD-II adapter  (USB serial → python-obd)                   │
│  ├── Arduino         (HMAC serial → gauge/lighting control)      │
│  └── SSD1306 OLED    (pi/oled_display.py ← /tmp/oled_state.json) │
└──────────────────┬───────────────────────────────────────────────┘
                   │ 192.168.4.x (truck hotspot)
        ┌──────────┴──────────┐
        │                     │
┌───────▼──────┐     ┌────────▼──────┐
│ archer-app   │     │ archer-browser │
│ (React Native│     │ (React Native  │
│  WebView)    │     │  multi-tab)    │
│              │     │                │
│ Connection   │     │ 7-tab browser  │
│ monitor,     │     │ ARCHER/KHLOE/  │
│ tier display,│     │ SIMULATOR/etc  │
│ HF failover  │     │ Lazy mounting  │
└──────────────┘     └────────────────┘
        Both import from /helpers.js (shared)

Public internet fallback:
┌────────────────────────────────────┐
│ HuggingFace Space                  │
│ aydencatman-archer.hf.space        │
│ Same archer.py — read-only mode    │
│ _IS_PI=False: no OBD/serial/GPIO   │
└────────────────────────────────────┘
```

## Tier System State Machine

```
Unknown device → /fans (fan page)
                 └── enters 6-digit code at /fans#signin

Code valid?
  ├── master_code (Tier 1)  → /  (owner dashboard)
  ├── one_time_code tier 2  → /passenger
  ├── one_time_code tier 3  → /family
  └── one_time_code tier 4  → /valet

MAC registered?
  └── GET / → MAC lookup → direct to tier page (no code needed)

JWT cookie present?
  └── GET / → decode_auth_jwt() → route to tier page
```

Tier permissions summary:

| Endpoint class | T1 | T2 | T3 | T4 |
|---|:---:|:---:|:---:|:---:|
| Full dashboard + OBD | ✓ | | | |
| Passenger dashboard | ✓ | ✓ | | |
| Family view | ✓ | ✓ | ✓ | |
| Valet speed display | ✓ | ✓ | ✓ | ✓ |
| Terminal exec | ✓ | | | |
| Device management | ✓ | | | |
| Code generation | ✓ | | | |
| Code revocation | ✓ | | | |
| notify_tier1 | ✓ | ✓ | | |
| Trip export | ✓ | ✓ | | |
| Pi tunnel URL | ✓ | | | |

## Session Auth Flow

```
1. Client GETs /csrf_token
   └── Server sets archer_sid cookie (random 32-hex)
   └── Returns CSRF token = HMAC(secret, sid)

2. Client POSTs to state-mutating endpoint
   ├── Sends X-CSRF-Token: <token> header
   └── Sends archer_sid cookie (httponly=false so JS can read it)

3. csrf_required decorator validates:
   ├── HMAC(secret, sid) == X-CSRF-Token  ← double-submit check
   └── Rejects with 403 on mismatch

4. Auth check: get_request_tier(request)
   ├── Reads archer_auth JWT cookie
   ├── decode_auth_jwt() → validates HS256 signature + expiry
   ├── Checks _revoked_tokens (jti) and _revoked_names (name:tier)
   └── Falls back to tier 5 (unauthenticated) if any check fails

5. Response sets archer_auth cookie (rolling 30-day expiry)
   └── secure=True when USE_TLS=true
```

## Pi ↔ Arduino Serial Protocol

Every message from Pi to Arduino is framed as:

```
MSG:<payload>:<nonce>:<tag>\n
```

Where:
- `payload` = comma-separated `key=value` pairs, e.g. `cmd=HELIX,preset=2`
- `nonce` = 8 hex chars (32-bit random, incremented per message)
- `tag` = first 16 hex chars of HMAC-SHA256(`serial_secret`, `payload|nonce`)

`serial_secret` is derived via HKDF from the HSM master key. Both Pi and
Arduino share the same key (Arduino receives it at pairing time over USB).

The `ReplayGuard` in `serial_auth.py` maintains a 256-nonce sliding window —
any nonce seen before is rejected as a replay attack.

```python
encode_message('cmd=HELIX,preset=2')
# → 'MSG:cmd=HELIX,preset=2:a3f2b1c0:8e4d2f1a3b5c6d7e'

decode_message(line, guard=replay_guard)
# → {'payload': 'cmd=HELIX,preset=2', 'nonce': 'a3f2b1c0'}
# Raises ValueError on bad tag or replay
```

## Pi vs HuggingFace Deployment Differences

| Feature | Pi (local) | HuggingFace |
|---|---|---|
| OBD-II | Real adapter or emulator | Emulator only (`USE_EMULATOR=true`) |
| Arduino serial | HMAC-signed serial | Disabled (`_IS_PI=False`) |
| GPIO / OLED | Active | Disabled |
| BeamNG bridge | Optional UDP listener | Disabled |
| MAC auth | ARP table lookup | Unavailable — PIN only |
| Terminal exec | Enabled (Tier 1) | Routes exist but Pi not connected |
| Pi tunnel | Registers via `/terminal/pi_register` | N/A |
| TLS | Optional (`USE_TLS=true`) | HF handles TLS termination |
| Auth Secure cookie | Set when USE_TLS=true | Always HTTPS (HF) |

The HuggingFace Space receives code via `sync-to-hf.yml` GitHub Actions.
Secrets (`ARCHER_SECRET`, `GROQ_API_KEY`, etc.) are stored as HF Space secrets
and injected as environment variables — they are never in the repository.

## archer-app vs archer-browser

Both are React Native / Expo apps but serve different use cases:

**archer-app** — Primary connection monitor app
- Single WebView pointing at the active server URL
- `useConnectionMonitor`: polls `/health` every 5s, auto-fails over to
  `HF_FALLBACK_URL` when Pi is unreachable for `FAIL_THRESH` consecutive polls
- Shows `ConnectionBar` with tier and ARCHER SPEAKING indicator
- Hotspot auto-detect via `getTruckUrlFromNetInfo` (exact SSID match)
- Haptics on warnings; push notifications for Archer messages when backgrounded

**archer-browser** — In-cab multi-tab browser
- 7 persistent tabs: ARCHER, KHLOE, SIMULATOR, VALET, FANS, GITHUB, CLAUDE
- Lazy tab mounting: unvisited tabs render `null` (no network requests)
- Edge-swipe navigation (left/right 50px swipe = tab change)
- `secureGet`/`secureSet` with AsyncStorage fallback for > 1800 byte state
- Full navigation bar hidden, keep-awake active

**Shared code** (`/helpers.js` at repo root):
- `buildUrl`, `isTruckSsid`, `getTruckUrlFromNetInfo`, `isPiOffline`
- `formatOBDMode`, `clampSpeed`, `arcFill`
- `TRUCK_SSID`, `TRUCK_IP`, `DEFAULT_PORT`, `FAIL_THRESH`, `HF_FALLBACK_URL`, `ARCHER_BASE`
- `secureGet`, `secureSet` (lazy-require RN modules — Node-safe for testing)
