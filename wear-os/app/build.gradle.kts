plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.aydencatman.archerwear"
    compileSdk = 35

    defaultConfig {
        // CONFIRMED against archer-app/app.json (expo.android.package): the
        // real phone companion app's applicationId is "com.ayden.archer" —
        // NOT "com.aydencatman.archer" (this file's prior placeholder) and
        // NOT "com.ayden.archerbrowser" (that's the separate archer-browser
        // app, which per its own HANDOFF.md pushes to a different GitHub
        // repo entirely — not the phone companion for this watch app).
        applicationId = "com.ayden.archer"
        minSdk = 30 // Wear OS 3.0+
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        // Centralized backend URL - change this one line (or override
        // per build variant below) rather than hunting through source
        // files when switching from HuggingFace to a home server
        buildConfigField("String", "ARCHER_BASE_URL", "\"https://aydencatman-archer.hf.space\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    // Once self-hosting is live, a second flavor pointing at the home
    // server (e.g. a Tailscale address) can be added here without
    // touching any Kotlin source:
    // productFlavors {
    //     create("cloud") { buildConfigField("String", "ARCHER_BASE_URL", "\"https://aydencatman-archer.hf.space\"") }
    //     create("home") { buildConfigField("String", "ARCHER_BASE_URL", "\"https://archer.your-tailscale-name.ts.net\"") }
    // }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// ── AUTHENTICATION — UNRESOLVED, NOT JUST UNWIRED ──────────────────────────
// There is no auth mechanism in this module, and this isn't a small gap to
// wire up later — it's a real design decision, made more pressing by the
// fact that Remote Start (POST /remote/start) is one of the actions a watch
// tile/complication would realistically want to expose, and that route is
// Owner-only server-side (archer.py: get_request_tier(request) > 1 → 403).
//
// Checked against the actual server code (archer.py + archer_state.py):
// get_request_tier() — the function every tier-gated route calls to decide
// who's asking — reads ONLY the archer_auth cookie. It never inspects the
// Authorization header for tier resolution at all. (csrf_required() does
// accept a Bearer JWT, but only to satisfy the CSRF check for native
// clients that can't hold a WebView's session cookie — it discards the
// token's payload afterward and never assigns a tier from it. This is
// already how the Android app's VoiceActivity works, and voice_command
// still gets treated as Tier 4/unregistered there for exactly this reason.)
//
// Concretely: this means simply adding an OkHttp Authorization header with
// some future stored token would NOT silently create a false sense of
// security — Remote Start would still correctly 403 as Owner-only against
// an unrecognized caller. But it also means nothing here can ever actually
// work as Tier 1 without one of two real changes, and picking between them
// is a security decision that needs to be made deliberately, not inferred
// from wiring:
//
//   (a) Extend get_request_tier() server-side to also resolve tier from a
//       validated Bearer JWT (the JWT verification itself — HMAC-SHA256,
//       archer_state.decode_auth_jwt — is already sound; the change is
//       widening what's allowed to assert a tier this way, which broadens
//       attack surface for every route that currently trusts cookie-only
//       auth and needs its own review, not just this one).
//   (b) Give the watch its own pairing/registration flow — e.g. reusing
//       the existing /register_mac invite-code system (already used by
//       browsers) — and store the resulting JWT the same way the phone
//       app does (SecureStore-equivalent on Wear OS), rather than trying
//       to inherit the phone's session at all.
//
// Do not add a bearer token to any request in this module without first
// deciding between (a) and (b) above.

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.01.00"))
    implementation("androidx.wear.compose:compose-material3:1.5.0")
    implementation("androidx.wear.compose:compose-foundation:1.5.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.wear.tiles:tiles:1.4.0")
    implementation("androidx.wear.tiles:tiles-material:1.4.0")
    implementation("androidx.wear.protolayout:protolayout:1.2.0")
    implementation("androidx.wear.protolayout:protolayout-material3:1.2.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.json:json:20250107")
}
