package com.ayden.archer

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Talks directly to Archer's backend, same pattern as the Roku channel.
 *
 * AUTH STATUS — confirmed against the real backend (blueprints/roku.py,
 * blueprints/vehicle.py, archer_state.py), and it's NOT the same answer
 * for every call in this file:
 *
 * - fetchStatus() → GET /roku/status is guarded by blueprints/roku.py's
 *   `_roku_auth` decorator, which DOES properly validate an
 *   `Authorization: Bearer <archer_auth JWT>` header and resolve tier from
 *   it (tier ≤ 3 required) — this is the exact same mechanism and the same
 *   token format the Roku channel already uses in production. Sending a
 *   real archer_auth JWT here works today.
 *
 * - triggerRemoteStart() → POST /remote/start is gated by
 *   `get_request_tier()` directly (blueprints/vehicle.py), which — unlike
 *   `_roku_auth` — only ever reads the archer_auth COOKIE for tier
 *   resolution and never inspects the Authorization header for that
 *   purpose. archer_state.csrf_required() *will* accept a valid Bearer JWT
 *   to satisfy the CSRF check specifically (that's the one place a bearer
 *   token is honored on this route), but that only gets the request past
 *   CSRF — get_request_tier() then independently resolves tier from the
 *   (absent) cookie and the route 403s as "Owner only" regardless of what
 *   was sent as Bearer. So this call fails closed with a clear 403, not a
 *   silent no-op — but it genuinely cannot succeed as Tier 1 from this
 *   client today.
 *
 *   The fix is a small, concrete backend change, not a big open-ended one:
 *   apply the same `_roku_auth` pattern already shipped and working for
 *   /roku/* to /remote/start (tightened to require tier == 1 instead of
 *   ≤ 3). That is an archer.py decision to make deliberately — do not
 *   route around it by, say, spoofing a cookie header from this client.
 */
object ArcherApiClient {

    private val BASE_URL = BuildConfig.ARCHER_BASE_URL

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    data class StatusResult(
        val success: Boolean,
        val status: String = "",
        val message: String = "",
        val alert: Boolean = false,
        // Real API shape: "alert" is a descriptive string (e.g. "HIGH COOLANT
        // TEMP — 235°F") or JSON null — the boolean above only says whether
        // one exists; this carries what it actually says. A watch surfacing
        // "there is an alert" with no text isn't very useful on its own.
        val alertText: String = "",
        val errorMessage: String = ""
    )

    suspend fun fetchStatus(authToken: String? = null): StatusResult = withContext(Dispatchers.IO) {
        try {
            val requestBuilder = Request.Builder()
                .url("$BASE_URL/roku/status")
                .header("Accept", "application/json")

            // Confirmed working: /roku/status accepts this exact header via
            // blueprints/roku.py's _roku_auth — see class-level doc comment.
            authToken?.let {
                requestBuilder.header("Authorization", "Bearer $it")
            }

            val response = client.newCall(requestBuilder.build()).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: return@withContext StatusResult(
                    success = false, errorMessage = "Empty response"
                )
                val json = JSONObject(body)
                // Real response shape (blueprints/roku.py roku_status()):
                // "alert" is either a descriptive string or JSON null, never
                // a boolean — presence, not truthiness, is the real signal.
                val alertText = if (json.isNull("alert")) "" else json.optString("alert", "")
                StatusResult(
                    success = true,
                    status = json.optString("status", "UNKNOWN"),
                    message = json.optString("message", ""),
                    alert = alertText.isNotEmpty(),
                    alertText = alertText
                )
            } else {
                StatusResult(success = false, errorMessage = "Server returned ${response.code}")
            }
        } catch (e: Exception) {
            StatusResult(success = false, errorMessage = e.message ?: "Connection failed")
        }
    }

    data class ActionResult(val success: Boolean, val errorMessage: String = "")

    /**
     * Triggers remote start. NOT functional for tier enforcement until the
     * backend change described in the class-level comment ships — this
     * will currently 403 as "Owner only" against any real request. Kept
     * implemented (rather than stubbed out) because the failure mode is
     * already correct — a clear rejection, not a false success — and the
     * request shape here is what the eventual working version needs
     * anyway. Confirmation UI in MainActivity.kt still requires an
     * explicit tap before this gets called, which is a real client-side
     * safeguard, but it is NOT a substitute for real server-side tier
     * checking, and will not become one by editing this file.
     */
    suspend fun triggerRemoteStart(authToken: String? = null): ActionResult = withContext(Dispatchers.IO) {
        try {
            val requestBuilder = Request.Builder()
                .url("$BASE_URL/remote/start")
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .header("Accept", "application/json")

            // See class-level doc comment: this satisfies CSRF on the
            // server (archer_state.csrf_required accepts a valid Bearer
            // JWT for that specific check) but does NOT satisfy the
            // Owner-only tier check, which reads the cookie instead and
            // will not see this header at all.
            authToken?.let {
                requestBuilder.header("Authorization", "Bearer $it")
            }

            val response = client.newCall(requestBuilder.build()).execute()

            if (response.isSuccessful) {
                ActionResult(success = true)
            } else {
                ActionResult(success = false, errorMessage = "Server returned ${response.code}")
            }
        } catch (e: Exception) {
            ActionResult(success = false, errorMessage = e.message ?: "Connection failed")
        }
    }
}
