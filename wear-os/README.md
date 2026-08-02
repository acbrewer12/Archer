# Wear OS — staged, not a working app yet

This folder holds `app/build.gradle.kts` and one Kotlin file,
`ArcherApiClient.kt` — both staged from planning/handoff sessions, not
scaffolded fresh. There is no `settings.gradle.kts`, no manifest, no
activities/tiles/UI here yet, and no `MainActivity.kt` despite being
referenced in comments below (written with the expectation it will exist
once real app scaffolding happens). It will not build as-is.

Two things were resolved before any of this landed:

1. **Package ID** — `com.ayden.archer`, confirmed against `archer-app/app.json`
   (the real phone companion app). Fixed in the gradle file's `applicationId`
   and used as `ArcherApiClient.kt`'s actual package/directory.
2. **Authentication** — confirmed precisely against the real backend
   (`blueprints/roku.py`, `blueprints/vehicle.py`, `archer_state.py`), and
   it's not a single answer for the whole app. See the class-level doc
   comment in `ArcherApiClient.kt` for the full detail, short version:
   - `fetchStatus()` → `GET /roku/status` — **works today**. That route's
     `_roku_auth` decorator properly validates a Bearer JWT and resolves
     tier from it (tier ≤ 3), the same mechanism the Roku channel already
     uses in production.
   - `triggerRemoteStart()` → `POST /remote/start` — **does not work**,
     confirmed. That route uses `get_request_tier()` directly, which only
     ever reads the `archer_auth` cookie for tier resolution, never the
     Authorization header. A Bearer JWT satisfies CSRF on this route but
     not the actual Owner-only tier check, so it fails closed with a 403
     rather than silently doing nothing or succeeding incorrectly.
     Fixing this for real is a small, concrete backend change — apply the
     same already-shipped `_roku_auth` pattern to `/remote/start`,
     tightened to `tier == 1` — not a big open-ended security redesign.
     That's an `archer.py` decision to make deliberately, not something
     this file works around.

Also fixed while confirming the above: `blueprints/roku.py`'s
`roku_status()` had a real bug independent of this app — it checked
`obd2_display['mode']` against values (`'REAL_OBD'`/`'EMULATED'`/etc.)
that `archer.py` never actually sets that field to, so `/roku/status`
always reported `"offline"` regardless of real vehicle state. Fixed to
use the same live-computed expression `get_display_data()` already uses.
