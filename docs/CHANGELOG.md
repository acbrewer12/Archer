# Archer Changelog

## v5 — Shared helpers, serial lazy init (2026-06)

- Moved `helpers.js` to repo root so both `archer-app` and `archer-browser` import
  from `../helpers` — eliminates cross-app drift risk (isTruckSsid fix was landing
  in only one app)
- `serial_auth.py`: `_SERIAL_SECRET` is now lazy-initialized on first call instead
  of at import time — HSM failures at import no longer crash unrelated startup paths
- `ReplayGuard._order` changed from `list` to `collections.deque` — `popleft()` is
  O(1) vs the O(n) `pop(0)` it replaced
- `detectTruckHotspot` in `archer-app/App.js` now delegates to `getTruckUrlFromNetInfo`
  — the exact-SSID check was added in v3 but the call site was missed
- Root `npm test` via `jest.config.js` covers shared helpers (47 tests)

## v4 — HF failover, browser storage, TLS (2026-06)

- `useConnectionMonitor` in `archer-app/App.js` now returns `effectiveUrl`; when the
  Pi fails `FAIL_THRESH` consecutive polls the hook pings `HF_FALLBACK_URL` and if
  reachable switches the WebView source automatically — no manual intervention needed
- `OfflineOverlay` shows "CLOUD MODE" with the fallback URL instead of a generic
  offline banner when already on the HF fallback
- `archer-browser/App.js`: `ARCHER_BASE` reads from `EXPO_PUBLIC_ARCHER_BASE` env var
  or `Constants.expoConfig.extra.archerBase` — URL is now config, not source
- `secureGet`/`secureSet` use `expo-secure-store` for values ≤ 1800 bytes and
  `AsyncStorage` as fallback for larger payloads (SecureStore 2 KB limit)
- Lazy tab mounting via `mountedIds` Set — unvisited browser tabs are not rendered at
  all, reducing memory and preventing background fetches
- `_SERIAL_SECRET` lazy init; `ReplayGuard` deque fix; 7 `TestSerialAuth` tests added
- `sync-to-hf.yml` now copies `hsm.py`, `db.py`, `serial_auth.py`, `config.py` to the
  HF Space — fixed `ModuleNotFoundError: No module named 'hsm'` on HF startup

## v3 — Security audit pass (2026-06)

- `isTruckSsid` changed from `ssid.includes('ARCHER')` to exact match `ssid === TRUCK_SSID`
  — prevents SSID spoofing (any network with "ARCHER" in the name would auto-connect)
- `HF_FALLBACK_URL` extracted to `helpers.js` constant; `pingServer` hits `/health`
  instead of `/sim/status`
- `beamng_bridge.py` UDP listener bound to `127.0.0.1` instead of `0.0.0.0` — BeamNG
  data endpoint is only reachable from the local machine
- `BEAMNG_TOKEN` shared secret: bridge sends `X-BeamNG-Token` header; server rejects
  mismatched or missing tokens with 403
- `/beamng_data` bounds checking on all numeric telemetry fields — rejects out-of-range
  values (e.g. RPM > 8000, coolant > 300 °C)
- SSE push endpoint `/display_data/stream` replaces 500ms polling JS `setInterval`
- `_get_tls_context()`: auto-generates self-signed cert when `USE_TLS=true`; graceful
  fallback if openssl unavailable
- `save_trip()` now records `peak_oil_temp`, `peak_coolant_temp`, `trip_distance`,
  `trip_mpg`, `fuel_used_gal`, active `fault_codes`
- OLED `stale` flag: written when sim disabled AND last OBD update > 10 s ago;
  OLED display shows "! NO DATA ! / SENSOR LOST" in that state
- `.githooks/pre-commit` added — blocks staging `archer.env`, `obd_auth.key`,
  `google-services.json`, `*.keystore`; scans for embedded secrets
- 34 new tests across `TestBeamNGToken`, `TestDisplayDataSSE`, `TestBeamNGBounds`,
  `TestTripLogEnrichment`, `TestOLEDStaleFlag`, `TestTLSContext`

## v2 — Blueprint refactor, HSM, JWT (2026-05)

- Decomposed `archer.py` monolith into Flask Blueprint modules under `blueprints/`
  — `terminal.py`, and supporting modules; `display_app` remains as the main app
- `hsm.py`: software HSM using `/etc/archer/master.key` → HKDF-SHA256 derivation;
  server exits if HSM is unavailable (fail-closed, no insecure fallback)
- `archer_state.py`: JWT-only auth (`make_auth_jwt`/`decode_auth_jwt`) with HS256
  (stdlib `hmac` + `hashlib`, no PyJWT dependency); `_revoked_tokens` OrderedDict
  for jti revocation; `_revoked_names` for immediate name-level revocation
- CSRF double-submit cookie: `archer_sid` + `X-CSRF-Token` header; `csrf_required`
  decorator applied to all state-mutating endpoints
- `db.py`: SQLite WAL-mode persistence replaces `archer_memory.json`; `db_save` /
  `db_load` flat key-value interface; backward-compat read from JSON on first boot
- `serial_auth.py`: HMAC-SHA256 serial framing (`MSG:<payload>:<nonce>:<tag>`);
  256-nonce sliding-window `ReplayGuard` rejects replayed messages
- `beamng_bridge.py`: UDP OutGauge listener for BeamNG telemetry

## v1 — Initial system (2025)

- `archer.py`: monolithic Flask server with OBD-II integration (real + emulated),
  Spotify, weather, trip logging, awareness engine, GPS, voice (Vosk STT + Piper TTS),
  4-tier auth via MAC whitelist + one-time codes
- `archer-app`: React Native WebView wrapper with connection monitoring, hotspot
  auto-detect, ConnectionBar, tier display, haptics on warnings
- `archer-browser`: multi-tab React Native browser for in-cab use (ARCHER, KHLOE,
  SIMULATOR, VALET, FANS, GITHUB, CLAUDE tabs)
- Raspberry Pi integration: OLED display (`pi/oled_display.py`), Arduino serial
  control, OBD-II via python-obd
- HuggingFace Space auto-sync via `sync-to-hf.yml` GitHub Actions workflow
