/**
 * Archer App — unit tests for pure helper functions.
 * Run with: npx jest  (from archer-app/)
 */

const {
  buildUrl,
  isTruckSsid,
  getTruckUrlFromNetInfo,
  isPiOffline,
  formatOBDMode,
  clampSpeed,
  arcFill,
  DEFAULT_PORT,
  TRUCK_IP,
  FAIL_THRESH,
} = require('../helpers');


// ── buildUrl ────────────────────────────────────────────────────
describe('buildUrl', () => {
  test('null input returns null', () => {
    expect(buildUrl(null)).toBeNull();
  });

  test('empty string returns null', () => {
    expect(buildUrl('')).toBeNull();
  });

  test('bare IP uses default port', () => {
    expect(buildUrl('192.168.4.1')).toBe(`http://192.168.4.1:${DEFAULT_PORT}`);
  });

  test('IP with custom port', () => {
    expect(buildUrl('192.168.4.1:8080')).toBe('http://192.168.4.1:8080');
  });

  test('full http:// URL passes through unchanged', () => {
    expect(buildUrl('http://192.168.4.1:7860')).toBe('http://192.168.4.1:7860');
  });

  test('trailing slash stripped from full URL', () => {
    expect(buildUrl('http://192.168.4.1:7860/')).toBe('http://192.168.4.1:7860');
  });

  test('HuggingFace URL passes through', () => {
    const hf = 'https://aydencatman-archer.hf.space';
    expect(buildUrl(hf)).toBe(hf);
  });

  test('TRUCK_IP builds expected URL', () => {
    expect(buildUrl(TRUCK_IP)).toBe(`http://${TRUCK_IP}:${DEFAULT_PORT}`);
  });
});


// ── isTruckSsid ─────────────────────────────────────────────────
describe('isTruckSsid', () => {
  test('exact truck SSID matches', () => {
    expect(isTruckSsid('ARCHER-2500HD')).toBe(true);
  });

  test('substring ARCHER matches', () => {
    expect(isTruckSsid('ARCHER_TRUCK')).toBe(true);
  });

  test('non-truck SSID does not match', () => {
    expect(isTruckSsid('Xfinity-Home')).toBe(false);
  });

  test('empty string does not match', () => {
    expect(isTruckSsid('')).toBe(false);
  });

  test('null returns false', () => {
    expect(isTruckSsid(null)).toBe(false);
  });

  test('undefined returns false', () => {
    expect(isTruckSsid(undefined)).toBe(false);
  });
});


// ── getTruckUrlFromNetInfo ───────────────────────────────────────
describe('getTruckUrlFromNetInfo', () => {
  test('wifi + ARCHER SSID returns truck URL', () => {
    const state = { type: 'wifi', details: { ssid: 'ARCHER-2500HD' } };
    expect(getTruckUrlFromNetInfo(state)).toBe(`http://${TRUCK_IP}:${DEFAULT_PORT}`);
  });

  test('cellular connection returns null', () => {
    const state = { type: 'cellular', details: { ssid: null } };
    expect(getTruckUrlFromNetInfo(state)).toBeNull();
  });

  test('wifi but wrong SSID returns null', () => {
    const state = { type: 'wifi', details: { ssid: 'MyHomeWiFi' } };
    expect(getTruckUrlFromNetInfo(state)).toBeNull();
  });

  test('null state returns null', () => {
    expect(getTruckUrlFromNetInfo(null)).toBeNull();
  });

  test('missing details returns null', () => {
    const state = { type: 'wifi' };
    expect(getTruckUrlFromNetInfo(state)).toBeNull();
  });

  test('null SSID returns null', () => {
    const state = { type: 'wifi', details: { ssid: null } };
    expect(getTruckUrlFromNetInfo(state)).toBeNull();
  });
});


// ── isPiOffline ──────────────────────────────────────────────────
describe('isPiOffline', () => {
  test('0 fails → online', () => {
    expect(isPiOffline(0)).toBe(false);
  });

  test('below threshold → online', () => {
    expect(isPiOffline(FAIL_THRESH - 1)).toBe(false);
  });

  test('at threshold → offline', () => {
    expect(isPiOffline(FAIL_THRESH)).toBe(true);
  });

  test('above threshold → offline', () => {
    expect(isPiOffline(FAIL_THRESH + 10)).toBe(true);
  });
});


// ── formatOBDMode ────────────────────────────────────────────────
describe('formatOBDMode', () => {
  test('REAL_OBD passes through', () => {
    expect(formatOBDMode('REAL_OBD')).toBe('REAL_OBD');
  });

  test('EMULATED passes through', () => {
    expect(formatOBDMode('EMULATED')).toBe('EMULATED');
  });

  test('BEAMNG passes through', () => {
    expect(formatOBDMode('BEAMNG')).toBe('BEAMNG');
  });

  test('DISCONNECTED passes through', () => {
    expect(formatOBDMode('DISCONNECTED')).toBe('DISCONNECTED');
  });

  test('unknown string maps to DISCONNECTED', () => {
    expect(formatOBDMode('BOGUS')).toBe('DISCONNECTED');
  });

  test('empty string maps to DISCONNECTED', () => {
    expect(formatOBDMode('')).toBe('DISCONNECTED');
  });

  test('null maps to DISCONNECTED', () => {
    expect(formatOBDMode(null)).toBe('DISCONNECTED');
  });
});


// ── clampSpeed ───────────────────────────────────────────────────
describe('clampSpeed', () => {
  test('zero stays zero', () => {
    expect(clampSpeed(0, 120)).toBe(0);
  });

  test('normal speed within range', () => {
    expect(clampSpeed(65, 120)).toBe(65);
  });

  test('over-max clamped to max', () => {
    expect(clampSpeed(150, 120)).toBe(120);
  });

  test('negative clamped to 0', () => {
    expect(clampSpeed(-5, 120)).toBe(0);
  });

  test('NaN returns 0', () => {
    expect(clampSpeed(NaN, 120)).toBe(0);
  });

  test('non-number returns 0', () => {
    expect(clampSpeed('fast', 120)).toBe(0);
  });
});


// ── arcFill ──────────────────────────────────────────────────────
describe('arcFill', () => {
  test('zero speed → 0 fill', () => {
    expect(arcFill(0)).toBe(0);
  });

  test('max speed → full track (198)', () => {
    expect(arcFill(120)).toBe(198);
  });

  test('60 mph → half track (99)', () => {
    expect(arcFill(60)).toBe(99);
  });

  test('over-max speed still returns full track', () => {
    expect(arcFill(200)).toBe(198);
  });

  test('negative speed returns 0', () => {
    expect(arcFill(-10)).toBe(0);
  });

  test('result is proportional', () => {
    expect(arcFill(30)).toBeCloseTo(49.5, 1);
  });
});
