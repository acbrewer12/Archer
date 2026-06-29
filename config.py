"""
config.py — Environment variable documentation and defaults for Archer.

All values are loaded from environment variables (set via archer.env,
/etc/archer/archer.env, or GitHub Secrets for HuggingFace deployment).
This file documents every variable, its purpose, and its default.

NEVER hard-code secrets here. Set them in archer.env or GitHub Secrets.
"""
import os

# ── CORE SECURITY ─────────────────────────────────────────────────────────────
import secrets as _secrets

# Used for: CSRF tokens, cookie signing, session validation.
# If not set, a random ephemeral value is used — safe but sessions won't survive restarts.
# Set ARCHER_SECRET in archer.env (local) or GitHub Secrets (HuggingFace).
ARCHER_SECRET = os.environ.get('ARCHER_SECRET') or _secrets.token_hex(32)

# Master owner PIN for Tier 1 sign-in (numeric, min 4 digits).
ARCHER_OWNER_PIN = os.environ.get('ARCHER_OWNER_PIN', '')

# Master invite code (used when no PIN/MAC auth is set up yet).
ARCHER_MASTER_CODE = os.environ.get('ARCHER_MASTER_CODE', '')

# Token the Raspberry Pi uses to register its tunnel URL.
# Set ARCHER_PI_TOKEN in archer.env on both the Pi and the server — must match.
# If not set, a random ephemeral value is used (Pi registration won't work across restarts).
ARCHER_PI_TOKEN = os.environ.get('ARCHER_PI_TOKEN') or _secrets.token_hex(16)

# ── AI / LLM ──────────────────────────────────────────────────────────────────

# Groq API key for fast Llama inference (voice command AI responses).
# Get one at console.groq.com — free tier available.
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Google Gemini API key (fallback AI provider).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

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

# Arduino serial port for gauge/lighting control. Auto-detected if not set.
ARDUINO_PORT = os.environ.get('ARDUINO_PORT', '')

# ── FEATURE FLAGS ─────────────────────────────────────────────────────────────

# Set to 'true' to enable OBD emulator (no real OBD hardware needed).
USE_EMULATOR = os.environ.get('USE_EMULATOR', 'true').lower() == 'true'

# Set to 'true' to enable BeamNG telemetry bridge.
USE_BEAMNG = os.environ.get('USE_BEAMNG', 'false').lower() == 'true'

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
