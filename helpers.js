/**
 * Shared helper functions for archer-app and archer-browser.
 * Pure functions and constants are safe to import in a Node test environment.
 * secureGet / secureSet lazy-require React Native modules at call time.
 */

const DEFAULT_PORT    = '7860';
const TRUCK_SSID      = 'ARCHER-2500HD';
const TRUCK_IP        = '192.168.4.1';
const FAIL_THRESH     = 3;
const HF_FALLBACK_URL = 'https://aydencatman-archer.hf.space';
// Overridable at EAS build time via EXPO_PUBLIC_ARCHER_BASE env var.
const ARCHER_BASE     = process.env.EXPO_PUBLIC_ARCHER_BASE || HF_FALLBACK_URL;
const SEC_LIMIT       = 1800; // expo-secure-store ~2 KB per-value cap

/**
 * Build a base URL from a stored IP string.
 * Accepts bare IPs, IP:port, or full http:// URLs.
 */
function buildUrl(ip) {
  if (!ip) return null;
  if (ip.startsWith('http')) return ip.replace(/\/$/, '');
  const [host, port] = ip.split(':');
  return `http://${host}:${port || DEFAULT_PORT}`;
}

/**
 * Check whether a string exactly matches the Archer truck SSID.
 * Substring matching was too broad — any network with "ARCHER" in the name
 * would trigger auto-connect, allowing SSID spoofing.
 */
function isTruckSsid(ssid) {
  return typeof ssid === 'string' && ssid === TRUCK_SSID;
}

/**
 * Determine the auto-detect truck URL from a NetInfo state object.
 * Returns the candidate URL string, or null if criteria not met.
 */
function getTruckUrlFromNetInfo(state) {
  if (!state) return null;
  if (state.type !== 'wifi') return null;
  if (!isTruckSsid(state.details?.ssid)) return null;
  return buildUrl(TRUCK_IP);
}

/**
 * Decide whether a poll failure count has exceeded the offline threshold.
 */
function isPiOffline(failCount) {
  return failCount >= FAIL_THRESH;
}

/**
 * Format OBD mode string for display (matches archer_tier1.html color-map keys).
 */
function formatOBDMode(rawMode) {
  const VALID = ['REAL_OBD', 'EMULATED', 'BEAMNG', 'DISCONNECTED'];
  return VALID.includes(rawMode) ? rawMode : 'DISCONNECTED';
}

/**
 * Clamp a speed value to the expected display range.
 */
function clampSpeed(speed, max) {
  if (typeof speed !== 'number' || isNaN(speed)) return 0;
  return Math.min(max, Math.max(0, speed));
}

/**
 * Compute the speedometer arc fill length (px) from speed.
 * Track length = 198, max speed = 120 mph.
 */
function arcFill(speed) {
  const TRACK = 198;
  const MAX   = 120;
  return (clampSpeed(speed, MAX) / MAX) * TRACK;
}

// ── React Native storage helpers ──────────────────────────────────────────────
// Modules are lazy-required at call time so this file stays importable in Node.

async function secureSet(key, value) {
  const SecureStore  = require('expo-secure-store');
  const AsyncStorage = require('@react-native-async-storage/async-storage');
  if (value.length <= SEC_LIMIT) {
    try { await SecureStore.setItemAsync(key, value); return; } catch (_) {}
  }
  await AsyncStorage.setItem(key, value);
}

async function secureGet(key) {
  const SecureStore  = require('expo-secure-store');
  const AsyncStorage = require('@react-native-async-storage/async-storage');
  try {
    const v = await SecureStore.getItemAsync(key);
    if (v != null) return v;
  } catch (_) {}
  return AsyncStorage.getItem(key);
}

module.exports = {
  buildUrl,
  isTruckSsid,
  getTruckUrlFromNetInfo,
  isPiOffline,
  formatOBDMode,
  clampSpeed,
  arcFill,
  secureGet,
  secureSet,
  DEFAULT_PORT,
  TRUCK_IP,
  TRUCK_SSID,
  FAIL_THRESH,
  HF_FALLBACK_URL,
  ARCHER_BASE,
  SEC_LIMIT,
};
