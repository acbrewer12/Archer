"""
config.py — Environment variable documentation and defaults for Archer.

All values are loaded from environment variables (set via archer.env,
/etc/archer/archer.env, or GitHub Secrets for HuggingFace deployment).
This file documents every variable, its purpose, and its default.

NEVER hard-code secrets here. Set them in archer.env or GitHub Secrets.
"""
import os

# ── CORE SECURITY ─────────────────────────────────────────────────────────────

# HMAC-SHA256 key for JWT session tokens and CSRF double-submit cookies.
# Derived automatically from /etc/archer/master.key (HSM) on first boot if not set.
# Override by setting this env var explicitly in archer.env or GitHub Secrets.
# The server REFUSES to start if this cannot be derived — no insecure fallbacks.
ARCHER_SECRET = os.environ.get('ARCHER_SECRET', '')  # empty = use HSM auto-derive

# Master owner PIN for Tier 1 sign-in (numeric, min 4 digits).
ARCHER_OWNER_PIN = os.environ.get('ARCHER_OWNER_PIN', '')

# Master invite code (used when no PIN/MAC auth is set up yet).
ARCHER_MASTER_CODE = os.environ.get('ARCHER_MASTER_CODE', '')

# Token the Raspberry Pi uses to register its tunnel URL with /terminal/pi_register.
# Must match on both the Pi (export ARCHER_PI_TOKEN=...) and the server.
# Server rejects all Pi registrations if this is not set.
ARCHER_PI_TOKEN = os.environ.get('ARCHER_PI_TOKEN', '')  # empty = Pi registration disabled

# Shared secret between beamng_bridge.py and the /beamng_data endpoint.
# beamng_bridge.py sends this in X-BeamNG-Token; server rejects requests without it
# if this variable is set. Both must be set to the same value.
BEAMNG_TOKEN = os.environ.get('BEAMNG_TOKEN', '')  # empty = token check disabled

# ── AI / LLM ──────────────────────────────────────────────────────────────────

# Groq API key for fast Llama inference (voice command AI responses).
# Get one at console.groq.com — free tier available.
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Google Gemini API key (fallback AI provider).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# OpenRouter API key — OpenAI-compatible gateway, tried as a 4th fallback
# step after Groq/Cerebras/Gemini (kept separate from those on purpose, not
# a replacement — see the comment on the OpenRouter step in ask_archer()).
# Get one at openrouter.ai/keys.
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')

# ── DISCORD BOT (slash commands, alert buttons, digests) ───────────────────────

# Bot token — needed only to register slash commands (discord_register_commands.py).
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')

# Application public key, from the Discord Developer Portal — verifies that
# requests to /discord/interactions genuinely came from Discord.
DISCORD_PUBLIC_KEY = os.environ.get('DISCORD_PUBLIC_KEY', '')

# Application ID, from the Discord Developer Portal.
DISCORD_APPLICATION_ID = os.environ.get('DISCORD_APPLICATION_ID', '')

# Discord user ID allowed to run commands and click alert buttons.
# Right-click your own name in Discord (Developer Mode on) -> Copy User ID.
DISCORD_OWNER_ID = os.environ.get('DISCORD_OWNER_ID', '')

# Comma-separated Discord user IDs at each tier, DMed directly by
# discord_dm_fanout() based on alert severity — a different, plural
# concept from DISCORD_OWNER_ID above (which just gates who can run
# commands). Same static-allowlist tradeoff as Slack's *_USER_IDS below:
# revocation means editing this and restarting, not an in-app action.
DISCORD_OWNER_USER_IDS     = os.environ.get('DISCORD_OWNER_USER_IDS', '')
DISCORD_PASSENGER_USER_IDS = os.environ.get('DISCORD_PASSENGER_USER_IDS', '')
DISCORD_FAMILY_USER_IDS    = os.environ.get('DISCORD_FAMILY_USER_IDS', '')
DISCORD_VALET_USER_IDS     = os.environ.get('DISCORD_VALET_USER_IDS', '')

# Channel ID for alerts that carry buttons (crash, parking armed) — these
# go out via the bot token, not a webhook. Right-click the channel -> Copy
# Channel ID.
DISCORD_ALERTS_CHANNEL_ID = os.environ.get('DISCORD_ALERTS_CHANNEL_ID', '')

# 24-hour local hour to send the daily digest (default 20 = 8 PM).
# Shared by the Slack digest below — one schedule, not two.
DISCORD_DIGEST_HOUR = os.environ.get('DISCORD_DIGEST_HOUR', '20')

# ── SLACK BOT (slash commands, alert buttons, digests, tiered routing) ─────────
# Replaces the Discord integration above as the live path — see
# self-host/README.md "Slack bot setup". Discord's own env vars/code are
# left intact and still work if you re-enable discord_config['enabled'].

# Signing Secret, from the Slack app's Basic Information page — verifies
# requests to /slack/interactions genuinely came from Slack.
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')

# Bot User OAuth Token (starts with xoxb-), from OAuth & Permissions after
# installing the app to your workspace.
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')

# One Slack channel ID per tier (1-3 only — see self-host/README.md for why
# Valet/Public aren't part of this).
SLACK_CHANNEL_OWNER     = os.environ.get('SLACK_CHANNEL_OWNER', '')
SLACK_CHANNEL_PASSENGER = os.environ.get('SLACK_CHANNEL_PASSENGER', '')
SLACK_CHANNEL_FAMILY    = os.environ.get('SLACK_CHANNEL_FAMILY', '')

# Comma-separated Slack user IDs allowed at each tier. Slack has no
# equivalent of the web dashboard's MAC/JWT tier system, so this mapping is
# the explicit source of truth for who's who in Slack.
SLACK_OWNER_USER_IDS     = os.environ.get('SLACK_OWNER_USER_IDS', '')
SLACK_PASSENGER_USER_IDS = os.environ.get('SLACK_PASSENGER_USER_IDS', '')
SLACK_FAMILY_USER_IDS    = os.environ.get('SLACK_FAMILY_USER_IDS', '')

# Single dedicated channel for panic-mode alerts (POST /panic/activate).
# Bypasses the tier-based channel routing above entirely — not part of the
# owner/passenger/family split.
SLACK_CHANNEL_PANIC = os.environ.get('SLACK_CHANNEL_PANIC', '')

# ── SPOTIFY ───────────────────────────────────────────────────────────────────

# Create an app at developer.spotify.com, set redirect URI to:
# http://localhost:7860/spotify/callback  (or your HF Space URL)
SPOTIFY_CLIENT_ID     = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')

# ── WEATHER ───────────────────────────────────────────────────────────────────

# WeatherAPI.com key for the weather compare feature.
# Free tier supports 1M calls/month at weatherapi.com
WEATHERAPI_KEY = os.environ.get('WEATHERAPI_KEY', '')

# ── DEPLOYMENT ────────────────────────────────────────────────────────────────

# HuggingFace token for syncing to Spaces (used in GitHub Actions only).
HF_TOKEN = os.environ.get('HF_TOKEN', '')

# Firebase service account JSON for push notifications (Android FCM).
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')

# ── HARDWARE PORTS (Raspberry Pi / local only) ────────────────────────────────

# OBD-II adapter serial port. Auto-detected if not set.
# Examples: /dev/ttyUSB0 (Linux), COM3 (Windows)
OBD_PORT = os.environ.get('OBD_PORT', '')

# Signed tier-ceiling token for the OBDLink connection (archer.py:
# _resolve_obd_connection_tier / obd_autodetect). Mint one with
# archer_state.make_obd_token(tier) — e.g. make_obd_token(1) for an
# owner-ceiling connection. Missing or invalid means obd_autodetect() never
# opens a real connection at all (fail-closed — same as no adapter found),
# regardless of whether real OBD hardware is actually present.
OBD_ACCESS_TOKEN = os.environ.get('OBD_ACCESS_TOKEN', '')

# ── SERVER-TO-PI CONFIG SANITY HANDSHAKE ────────────────────────────────────
# Ground truth for POST /terminal/pi_config_check (blueprints/terminal.py),
# which pi/config_sanity_check.py calls on every Pi boot, before
# obd_gatekeeper.py's own HMAC handshake runs. None of these three existed
# anywhere on the server before this — confirmed by reading the code, not
# assumed — so this is a new, explicit ground-truth store, not a reused one.
# All three must be set to the SAME value the Pi independently expects
# (PI_EXPECTED_SERVER_IP / PI_EXPECTED_OBDLINK_SERIAL / the Pi's real key
# file, respectively) — same "operator keeps two copies in sync" pattern
# already used for ARCHER_PI_TOKEN above, not a live-verified value.

# This server's current Tailscale IP. Exists so the Pi has ONE authoritative
# place to check this against, instead of only discovering drift the hard
# way if Tailscale reassigns it or the server gets rebuilt — the real,
# flagged fragility this whole feature exists to catch (see Self Hosting
# vault note). Does not itself fix the separate, still-open problem of this
# IP being hardcoded across Grafana/Caddy configs too.
SERVER_TAILSCALE_IP = os.environ.get('SERVER_TAILSCALE_IP', '')

# SHA-256 hex digest of the real Gatekeeper HMAC key
# (/etc/archer/obd_auth.key on the Pi) — compute once with:
#   sha256sum /etc/archer/obd_auth.key
# Deliberately a fingerprint, not the raw key: the key itself must only
# ever exist on the Pi (key_manager.py's own module docstring), never
# transmitted or stored server-side.
GATEKEEPER_KEY_FINGERPRINT = os.environ.get('GATEKEEPER_KEY_FINGERPRINT', '')

# Canonical serial of the specific OBDLink MX+ unit this build uses. No
# code anywhere reads a live serial from the adapter itself (confirmed by
# inspecting obd_autodetect()'s ELM327 AT command set — no serial-query
# command exists) — this check can only confirm the server's and Pi's own
# independently-configured expectations agree with each other, not that
# either one matches the physical hardware. Real hardware validation still
# needed once the Pi/adapter exist — see pi/config_sanity_check.py.
OBDLINK_SERIAL = os.environ.get('OBDLINK_SERIAL', '')

# Arduino serial port for gauge/lighting control. Auto-detected if not set.
ARDUINO_PORT = os.environ.get('ARDUINO_PORT', '')

# A ReSpeaker mic array (any model with "ReSpeaker"/"Seeed" in its device
# name) is auto-detected by check_microphone()/listen_once() in archer.py
# — nothing to set for that. This only controls which of the array's
# channels gets fed to the recognizer once one is found: default 0 (the
# first raw mic channel — present on every ReSpeaker variant, the safest
# default). Override for a specific model where a different channel is
# known to work better (e.g. channel 4 = processed/beamformed audio on
# the 6-channel ReSpeaker USB Mic Array v2.0).
RESPEAKER_CHANNEL_INDEX = os.environ.get('RESPEAKER_CHANNEL_INDEX', '0')

# ── FEATURE FLAGS ─────────────────────────────────────────────────────────────

# Set to 'true' to enable OBD emulator (no real OBD hardware needed).
USE_EMULATOR = os.environ.get('USE_EMULATOR', 'true').lower() == 'true'

# Set to 'true' to enable BeamNG telemetry bridge.
USE_BEAMNG = os.environ.get('USE_BEAMNG', 'false').lower() == 'true'

# Set to 'true' to enable HTTPS on the Flask server (local Pi/hotspot deployment).
# On first boot with USE_TLS=true, a self-signed cert is auto-generated at
# /etc/archer/archer.crt + archer.key (override with ARCHER_TLS_CERT/ARCHER_TLS_KEY).
USE_TLS = os.environ.get('USE_TLS', 'false').lower() == 'true'

# Override default cert/key paths (only used when USE_TLS=true).
ARCHER_TLS_CERT = os.environ.get('ARCHER_TLS_CERT', '/etc/archer/archer.crt')
ARCHER_TLS_KEY  = os.environ.get('ARCHER_TLS_KEY',  '/etc/archer/archer.key')

# ── QUICK REFERENCE ───────────────────────────────────────────────────────────
#
# Minimum required to run locally:
#   ARCHER_SECRET=<random-string>
#   GROQ_API_KEY=<your-groq-key>         (or GEMINI_API_KEY)
#
# Required for full functionality:
#   ARCHER_OWNER_PIN=<4+ digit pin>
#   SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET
#   WEATHERAPI_KEY
#   FIREBASE_SERVICE_ACCOUNT_JSON        (Android push notifications)
#
# Required for HuggingFace deployment (GitHub Secrets):
#   HF_TOKEN
#   ARCHER_SECRET
#   GROQ_API_KEY
#   SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
#   WEATHERAPI_KEY
#   FIREBASE_SERVICE_ACCOUNT_JSON
#
# archer.env file format (never commit this file):
#   ARCHER_SECRET=your-secret-here
#   GROQ_API_KEY=gsk_...
#   SPOTIFY_CLIENT_ID=...
#   SPOTIFY_CLIENT_SECRET=...
#   WEATHERAPI_KEY=...
#   ARCHER_OWNER_PIN=1234
