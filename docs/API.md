# Archer API Reference

All endpoints are served by the Flask server running on the Raspberry Pi at port `7860` (or the HuggingFace Space fallback). Base URL: `http://<pi-ip>:7860`.

---

## Authentication

Every request is authenticated via the `archer_auth` cookie, which contains a **signed HS256 JWT** issued by the server on login.

```
archer_auth = <header>.<payload>.<signature>   (standard JWT, HS256)
```

Payload fields: `tier` (int), `name` (str), `jti` (UUID), `iat`, `exp` (Unix timestamps).

| `tier` | Role | Access |
|---|---|---|
| Not set / 5 | Guest | Read-only fan access |
| 4 | Valet | Dashboard read-only, speed limit enforced |
| 3 | Family | Dashboard, weather, Spotify |
| 2 | Passenger | Dashboard, Spotify, voice, drag |
| 1 | Owner | Full control (Ayden) |

The old `tier:name:hmac` cookie format is **disabled** — the server rejects it to prevent downgrade attacks. All sessions are issued as JWTs.

**CSRF protection** — all state-changing `POST` requests require an `X-CSRF-Token` header obtained from `GET /csrf_token`. Exemptions: `/voice_command`, `/boot`, `/init`, and a few low-risk read routes.

**Log stream** — `/terminal/log_stream` is an SSE endpoint. Because browsers cannot send custom headers on `EventSource`, CSRF cannot protect it directly. Instead, call `POST /terminal/log_stream_key` (CSRF-protected) to obtain a 30-second one-time key, then open the SSE URL as `/terminal/log_stream?key=<key>`.

---

## Core

### `GET /csrf_token`
Returns a CSRF token for the current session. Sets `archer_sid` session cookie.

**Response**
```json
{ "token": "abc123..." }
```

---

### `GET /display_data`
Primary telemetry endpoint — polled every 1 s by the UI.

**Auth:** None required (public, rate-limited)

**Response fields**

| Field | Type | Description |
|---|---|---|
| `rpm` | int | Engine RPM |
| `speed` | int | Vehicle speed (mph) |
| `coolant_temp` | int | Coolant temperature (°F) |
| `throttle` | float | Throttle position (%) |
| `boost` | float | MAP-derived boost (psi) |
| `oil_temp` | int | Oil temperature (°F) — PID 015C |
| `maf` | float | Mass air flow (g/s) — PID 0110 |
| `timing` | float | Ignition timing advance (° BTDC) — PID 010E |
| `engine_load` | float | Calculated engine load (%) — PID 0104 |
| `stft_b1` | float | Short-term fuel trim, bank 1 (%) |
| `ltft_b1` | float | Long-term fuel trim, bank 1 (%) |
| `stft_b2` | float | Short-term fuel trim, bank 2 (%) |
| `ltft_b2` | float | Long-term fuel trim, bank 2 (%) |
| `drive_mode` | str | `"normal"` / `"eco"` / `"tow"` |
| `obd_mode` | str | `"REAL_OBD"` / `"EMULATED"` / `"BEAMNG"` / `"DISCONNECTED"` |
| `maintenance_mode` | bool | True when maintenance banner is active |
| `vehicle_make` | str\|null | `"GMC"` or `"Chevrolet"`, null until set |
| `vehicle_model` | str\|null | `"Sierra 2500HD"` or `"Silverado 2500HD"` |
| `vehicle_name` | str | `"2006 GMC Sierra 2500HD"` or generic placeholder |
| `vehicle_name_header` | str | ALL-CAPS header variant |
| `device_tier` | int | Tier of the requesting device |
| `dtc_codes` | list | Active DTC fault code strings |
| `fuel_level` | float\|null | Fuel level (%), null if unknown |
| `battery_voltage` | float\|null | Battery voltage (V) |

---

### `POST /voice_command`
Process a voice command.

**Auth:** Tier 1–3 only. Tier 4 blocked. Rate-limited: 40/min, 200/hr. While moving >10 mph: 3/min.

**Body**
```json
{ "command": "what is my oil temp", "log_only": false }
```

| Field | Type | Description |
|---|---|---|
| `command` | str | Voice command text (max 500 chars) |
| `log_only` | bool | If true, log only — no AI response |

**Response**
```json
{ "response": "Oil temp is 194 degrees." }
```

Dangerous commands (`shut down`, `engine off`, `reboot`, `tc off`, etc.) are blocked for Tier 2+.

---

### `GET /system_health`
Returns system diagnostics: CPU temp, memory, uptime, OBD connection status.

**Auth:** Tier 1–2

---

### `GET /health`
Alias for `/system_health`.

---

### `GET /logs`
Returns recent log lines from the in-memory ring buffer (last 2000 lines).

**Auth:** Tier 1 only

**Response**
```json
{ "logs": ["[OBD] Connected", "[ARCHER] Boot complete", ...] }
```

---

### `GET /sensor_history`
Returns the last N readings for charting.

**Auth:** Tier 1–2

**Response**
```json
{ "history": [{ "ts": 1750000000, "rpm": 800, "speed": 0 }, ...] }
```

---

### `GET /trip_stats`
Returns trip statistics: distance, avg speed, fuel economy estimate, runtime.

**Auth:** Tier 1–2

---

### `GET /export/trip`
Download trip data as JSON.

**Auth:** Tier 1

---

## Vehicle Identity

### `POST /set_vehicle`
Record which truck was purchased. Purely cosmetic — no functional behavior changes since both Sierra SLT and Silverado LT3 share the GMT800 platform, LQ4 engine, 4L80E, and DTC database.

**Auth:** Tier 1 only

**Body**
```json
{ "make": "GMC", "model": "Sierra 2500HD" }
```

| Field | Allowed values |
|---|---|
| `make` | `"GMC"`, `"Chevrolet"` |
| `model` | `"Sierra 2500HD"`, `"Silverado 2500HD"` |

**Response**
```json
{ "ok": true, "vehicle": "2006 GMC Sierra 2500HD" }
```

**Errors**
- `400` — Invalid make or model
- `403` — Not Tier 1

---

## Auth & Device Management

### `POST /register_device`
Register a new device with a name and access tier.

**Body**
```json
{ "fingerprint": "abc123", "name": "Passenger Phone", "tier": 2 }
```

Tier is capped at 2 (non-owner devices cannot self-request Tier 1). Tier 4 (valet) is accepted as-is.

**Response**
```json
{ "ok": true, "name": "Passenger Phone", "tier": 2 }
```

---

### `POST /device_tier`
Query what tier a device fingerprint has been granted.

**Body**
```json
{ "fingerprint": "abc123" }
```

**Response**
```json
{ "tier": 2, "name": "Passenger Phone" }
```

---

### `POST /register_mac`
Register a MAC address for auto-login. **Tier 1 only.**

**Body**
```json
{ "mac": "aa:bb:cc:dd:ee:ff", "name": "Ayden's Phone", "tier": 1 }
```

---

### `POST /deregister_mac`
Remove a MAC from the whitelist. **Tier 1 only.**

**Body**
```json
{ "mac": "aa:bb:cc:dd:ee:ff" }
```

---

### `GET /registered_devices`
List all registered MACs and device fingerprints. **Tier 1 only.**

---

### `GET /devices`
Alias for `/registered_devices`.

---

### `POST /generate_code`
Generate a one-time sign-in invite code. **Tier 1 only.**

**Response**
```json
{ "code": "ABC123", "expires_at": 1750000000 }
```

---

### `POST /revoke_code`
Revoke an invite code immediately.

**Body**
```json
{ "code": "ABC123" }
```

---

### `GET /sign_in_code/status`
Check if the sign-in code feature is enabled and whether an active code exists.

---

### `POST /sign_in_code/toggle`
Enable or disable the one-time sign-in flow. **Tier 1 only.**

---

### `POST /sign_in_code/refresh`
Generate a fresh invite code, revoking the previous one. **Tier 1 only.**

---

### `POST /logout`
Clear the `archer_auth` cookie and revoke the session token.

---

## UI Pages

| Route | Tier required | Description |
|---|---|---|
| `/` | None | Redirects based on detected tier |
| `/tier1`, `/ayden`, `/display` | 1 | Owner cockpit |
| `/tier2`, `/pass`, `/passenger` | 2 | Passenger dashboard |
| `/tier3`, `/family` | 3 | Family read-only |
| `/tier4`, `/valet` | 4 | Valet limited view |
| `/limited` | 4 | Restricted valet screen |
| `/boot`, `/init` | None | Boot splash + initialization page |
| `/maintenance` | None | Maintenance-mode status page |

All UI routes go through the `require_boot` gate — a `_bt` one-time token must be in the query string or a valid session cookie present. The boot gate is transparent to normal browser navigation (the browser auto-follows the redirect to `/boot` and back).

---

## Navigation

### `POST /nav/save_place`
Save a named location.

**Body**
```json
{ "name": "Home", "lat": 37.6, "lng": -91.5 }
```

---

### `GET /nav/places`
List all saved places.

---

### `GET /navigate`
Open navigation to a saved place or address.

**Query params:** `?place=Home` or `?address=Salem+MO`

---

## Drag Racing

### `POST /drag/stage`
Stage the vehicle for a drag run. Resets the timer and arms launch detection.

**Auth:** Tier 1–2

---

### `POST /drag/launch`
Mark launch time. Triggers split-time tracking.

**Auth:** Tier 1–2

---

## Build Tracker

### `GET /specs`
Returns current build specifications (HP, TQ, displacement, upgrades).

---

### `POST /build/update`
Bulk-update the build list. **Tier 1 only.**

---

### `GET /build/part/search`
Search for parts. Query param: `?q=cam`

---

### `POST /build/part/add`
Add a part to the build tracker. **Tier 1 only.**

**Body**
```json
{
  "name": "Comp Cams Stage 2",
  "category": "Camshaft",
  "status": "ordered",
  "hp_gain": 45,
  "tq_gain": 40,
  "notes": "with matching springs"
}
```

---

### `POST /build/part/update`
Update a part's status or fields. **Tier 1 only.**

**Body**
```json
{ "id": "part-uuid", "status": "installed" }
```

---

### `POST /build/part/remove`
Remove a part by ID. **Tier 1 only.**

---

## Spotify

All Spotify endpoints require Tier 1–2 and a connected Spotify account.

| Route | Method | Description |
|---|---|---|
| `/spotify/login` | GET | Start OAuth flow |
| `/spotify/callback` | GET | OAuth callback |
| `/spotify/status` | GET | Playback status, current track |
| `/spotify/play` | POST | Resume or play a track URI |
| `/spotify/pause` | POST | Pause playback |
| `/spotify/next` | POST | Skip to next track |
| `/spotify/prev` | POST | Previous track |
| `/spotify/volume` | POST | Set volume `{"volume": 75}` |
| `/spotify/seek` | POST | Seek `{"position_ms": 30000}` |
| `/spotify/playlists` | GET | List user playlists |
| `/spotify/play_playlist` | POST | Play a playlist by URI |
| `/spotify/queue` | GET | Current queue |
| `/spotify/dj` | POST | Toggle DJ mode (auto-selects tracks by mood) |
| `/spotify/dj/intensity` | POST | Set DJ intensity `{"intensity": 0.7}` |
| `/spotify/disconnect` | GET | Disconnect Spotify session |

---

## Weather

### `GET /weather/compare`
Weather comparison page for two locations.

### `GET /weather/compare/data`
Raw weather data for comparison.

**Query params:** `?city1=Salem+MO&city2=St+Louis+MO`

---

## TPMS

### `GET /tpms`
Returns current tire pressure values (psi) for all four corners.

### `POST /tpms`
Update a tire pressure value (from Arduino sensor input or manual entry). **Tier 1 only.**

**Body**
```json
{ "position": "FL", "psi": 65.0 }
```

| Position | Meaning |
|---|---|
| `FL` | Front left |
| `FR` | Front right |
| `RL` | Rear left |
| `RR` | Rear right |

---

## BeamNG Integration

### `POST /beamng_data`
Receive telemetry from BeamNG.drive over UDP (internal use).

### `GET /beamng/status`
Returns `{ "connected": true/false }` — whether a live BeamNG session is active.

---

## OBD Gatekeeper

### `GET /obd_auth`
Returns the current authentication state of the OBD gatekeeper relay.

**Response**
```json
{ "authenticated": true, "relay_open": true }
```

---

## Archer OS

### `GET /archer_os`
Returns Pi system info: OS version, hostname, uptime, storage.

**Auth:** Tier 1 only

---

## Push Notifications

### `POST /notify_tier1`
Send a push notification to the Tier 1 (owner) device.

**Auth:** Tier 2+ can request Tier 1 attention; Tier 1 self-notifies.

**Body**
```json
{ "message": "Passenger requests music change" }
```

---

### `GET /tier_notifications`
Poll for pending tier-change or attention requests.

---

### `POST /tier_cancel`
Cancel a pending notification/request.

---

### `POST /tier_respond`
Respond to a tier notification (approve/deny).

---

### `GET /tier_response_status`
Check the status of a sent request.

---

## Miscellaneous

### `GET /boot/status`
Internal — returns boot sequence progress. Used by the UI boot screen.

### `GET /truck.jpg`
Serve the truck image asset.

### `GET /audio_stream`
Server-sent events stream for TTS audio.

### `POST /location/update`
Update current GPS location (from Android app).

**Body**
```json
{ "lat": 37.6, "lng": -91.5, "road": "Hwy 72" }
```

---

## Error Codes

| HTTP Status | Meaning |
|---|---|
| `200` | OK |
| `400` | Bad request — invalid body or missing required field |
| `401` | Not authenticated — set or refresh `archer_auth` cookie |
| `403` | Forbidden — your tier does not have access |
| `429` | Rate limited — slow down or wait |
| `500` | Server error — check `/logs` |

---

## Rate Limits

| Limit | Scope |
|---|---|
| 200 req/min per IP | Global |
| 40/min, 200/hr | `/voice_command` |
| 30/min | `/drag/stage`, `/drag/launch` |
| 3/min while speed > 10 mph | Voice commands while driving |

---

## Extending Archer

To add a new endpoint:

1. Define the route in `archer.py` using `@display_app.route(...)`.
2. Use `get_request_tier(request)` to enforce access level.
3. Add `@csrf_required` decorator for any state-changing POST that isn't voice.
4. Update this file and add a test class in `test_archer.py`.

All DTC codes live in `DTC_DATABASE` (inline dict) and are exported to `dtc_codes.json` at startup.
