/**
 * Pure helper functions extracted from App.js for unit testing.
 * No React Native imports — safe to run in a Node test environment.
 */

const DEFAULT_PORT = '7860';
const TRUCK_SSID   = 'ARCHER-2500HD';
const TRUCK_IP     = '192.168.4.1';
const FAIL_THRESH  = 3;

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

module.exports = {
  buildUrl,
  isTruckSsid,
  getTruckUrlFromNetInfo,
  isPiOffline,
  formatOBDMode,
  clampSpeed,
  arcFill,
  DEFAULT_PORT,
  TRUCK_IP,
  TRUCK_SSID,
  FAIL_THRESH,
};
