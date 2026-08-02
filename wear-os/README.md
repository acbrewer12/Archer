# Wear OS — staged, not a working app yet

This folder currently holds only `app/build.gradle.kts` — a build config
staged from an earlier planning session. There is no Kotlin source, no
`settings.gradle.kts`, no manifest, no activities/tiles here yet. It will
not build as-is.

Two things must be resolved before writing any Kotlin:

1. **Package ID** — `com.ayden.archer`, confirmed against `archer-app/app.json`
   (the real phone companion app). Already fixed in the gradle file.
2. **Authentication** — unresolved by design, not just unwired. See the
   comment block in `app/build.gradle.kts` above `dependencies {}` for the
   full explanation: `get_request_tier()` in `archer.py` only reads the
   `archer_auth` cookie, never a Bearer header, so this app can't
   authenticate as Tier 1 (needed for Remote Start) without a deliberate
   server-side or pairing-flow decision first.
