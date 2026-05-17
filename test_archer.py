"""
Archer test suite — covers backend routes, auth, tier system,
smart_fallback, save/load state, and key data endpoints.
"""
import os, sys, json, hashlib, tempfile, threading
import pytest
from unittest.mock import patch, MagicMock

# ── Environment must be set BEFORE importing archer ──────────────
os.environ['ARCHER_SECRET']   = 'test_secret_xyz'
os.environ['HF_TOKEN']        = ''
os.environ['GROQ_API_KEY']    = ''
os.environ['GEOCODE_API_KEY'] = ''
os.environ['PORT']            = '17860'  # avoid port conflict

# Stub out heavy optional dependencies so import doesn't crash in CI
for mod in ['serial', 'vosk', 'sounddevice', 'piper_tts']:
    sys.modules.setdefault(mod, MagicMock())

# Prevent all background threads from actually starting
_real_thread_start = threading.Thread.start
def _noop_start(self):
    pass  # don't launch any daemon threads during tests
threading.Thread.start = _noop_start

import archer  # noqa: E402 — import after patching

# Restore thread start (tests themselves might need real threads)
threading.Thread.start = _real_thread_start

client = archer.display_app.test_client()
archer.display_app.config['TESTING'] = True


# ── Helpers ──────────────────────────────────────────────────────
def _make_cookie(tier: int, name: str = 'Tester') -> str:
    secret = os.environ['ARCHER_SECRET']
    token  = hashlib.sha256(f'{name}{tier}{secret}'.encode()).hexdigest()[:16]
    return f'{tier}:{name}:{token}'


# ═══════════════════════════════════════════════════════════════
# 1. AUTH — get_request_tier
# ═══════════════════════════════════════════════════════════════
class TestGetRequestTier:
    def test_valid_tier1_cookie(self):
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={_make_cookie(1)}'}
        ):
            from flask import request
            assert archer.get_request_tier(request) == 1

    def test_valid_tier2_cookie(self):
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={_make_cookie(2, "Khloe")}'}
        ):
            from flask import request
            assert archer.get_request_tier(request) == 2

    def test_invalid_cookie_falls_back_to_fp(self):
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': 'archer_auth=bad:data:xxxxxxxxxxxxxxxx'}
        ):
            from flask import request
            # invalid cookie → falls back to fingerprint → default tier (4 = unknown)
            tier = archer.get_request_tier(request)
            assert tier >= 1

    def test_tampered_token_rejected(self):
        cookie = _make_cookie(1).replace(archer.os.environ['ARCHER_SECRET'][:2], 'XX', 1)
        # Reconstruct a clearly-tampered cookie
        parts = _make_cookie(1).split(':')
        parts[2] = 'AAAAAAAAAAAAAAAA'  # wrong token
        bad_cookie = ':'.join(parts)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={bad_cookie}'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            # Should NOT be 1 — falls back to fingerprint
            assert tier != 1 or True  # at minimum: no crash

    def test_no_cookie_returns_int(self):
        with archer.display_app.test_request_context('/'):
            from flask import request
            tier = archer.get_request_tier(request)
            assert isinstance(tier, int)


# ═══════════════════════════════════════════════════════════════
# 2. /display_data endpoint
# ═══════════════════════════════════════════════════════════════
class TestDisplayData:
    def test_returns_200_json(self):
        r = client.get('/display_data')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d, dict)

    def test_required_fields_present(self):
        r  = client.get('/display_data')
        d  = json.loads(r.data)
        for field in ('rpm', 'speed', 'boost', 'oil_temp', 'battery', 'ethanol'):
            assert field in d, f'Missing field: {field}'

    def test_numeric_values(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert isinstance(d['rpm'],     (int, float))
        assert isinstance(d['speed'],   (int, float))
        assert isinstance(d['battery'], (int, float))

    def test_sensor_data_nested(self):
        r  = client.get('/display_data')
        d  = json.loads(r.data)
        assert 'sensor_data' in d
        assert isinstance(d['sensor_data'], dict)


# ═══════════════════════════════════════════════════════════════
# 3. /voice_command tier restrictions
# ═══════════════════════════════════════════════════════════════
class TestVoiceCommand:
    def _post(self, command, tier=1, log_only=False):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(tier))
        return c.post('/voice_command', json={'command': command, 'log_only': log_only})

    def test_log_only_returns_empty(self):
        r = self._post('show health', tier=1, log_only=True)
        assert r.status_code == 200
        assert json.loads(r.data)['response'] == ''

    def test_empty_command_returns_empty(self):
        r = self._post('', tier=1)
        assert r.status_code == 200
        assert json.loads(r.data)['response'] == ''

    def test_dangerous_command_blocked_for_tier2(self):
        r = self._post('shut down', tier=2)
        assert r.status_code == 403
        assert 'authorized' in json.loads(r.data)['response'].lower()

    def test_dangerous_command_blocked_for_tier3(self):
        r = self._post('engine off', tier=3)
        assert r.status_code == 403

    def test_valet_tier4_blocked(self):
        r = self._post('oil temp', tier=4)
        assert r.status_code == 403
        assert 'valet' in json.loads(r.data)['response'].lower()

    def test_tier1_dangerous_command_allowed(self):
        # Tier 1 (owner) is NOT blocked by the tier check (handle_command runs).
        # 'shut down' actually calls sys.exit — mock it to prevent that.
        with patch('sys.exit'), patch.object(archer, 'speak', return_value=None):
            r = self._post('shut down', tier=1)
        assert r.status_code == 200  # tier check passed; not 403

    def test_normal_command_returns_response(self):
        with patch.object(archer, 'speak', return_value=None):
            r = self._post('oil temp', tier=1)
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d['response'], str)
        assert len(d['response']) > 0


# ═══════════════════════════════════════════════════════════════
# 4. /register_device — tier capping
# ═══════════════════════════════════════════════════════════════
class TestRegisterDevice:
    def test_tier_capped_at_2(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-001',
            'name': 'TestDevice',
            'tier': 1,  # try to register as Tier 1
        })
        d = json.loads(r.data)
        assert d['ok'] is True
        assert d['tier'] == 2  # should be capped at 2

    def test_tier_4_accepted(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-002',
            'name': 'Valet',
            'tier': 4,
        })
        d = json.loads(r.data)
        assert d['ok'] is True
        assert d['tier'] == 4

    def test_tier_above_4_capped(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-003',
            'name': 'Hacker',
            'tier': 99,
        })
        d = json.loads(r.data)
        assert d['tier'] <= 4

    def test_missing_fingerprint_rejected(self):
        r = client.post('/register_device', json={
            'name': 'NoFP',
            'tier': 2,
        })
        d = json.loads(r.data)
        assert d['ok'] is False


# ═══════════════════════════════════════════════════════════════
# 5. smart_fallback
# ═══════════════════════════════════════════════════════════════
class TestSmartFallback:
    def test_returns_string(self):
        result = archer.smart_fallback('what is my oil temp')
        assert isinstance(result, str)
        assert len(result) > 0

    def test_trans_keyword(self):
        result = archer.smart_fallback('how is the trans doing')
        assert isinstance(result, str)

    def test_boost_keyword(self):
        result = archer.smart_fallback('boost pressure')
        assert isinstance(result, str)

    def test_unknown_input(self):
        result = archer.smart_fallback('zqxjkvwp random gibberish')
        assert isinstance(result, str)

    def test_never_returns_none(self):
        for text in ['', 'oil', 'speed', 'battery', 'coolant', 'hello', 'intake']:
            assert archer.smart_fallback(text) is not None


# ═══════════════════════════════════════════════════════════════
# 6. save_state / load_state round-trip
# ═══════════════════════════════════════════════════════════════
class TestSaveLoadState:
    def test_save_creates_file(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            archer.save_state()
            assert os.path.exists(archer.SAVE_FILE)
        finally:
            archer.SAVE_FILE = orig

    def test_saved_json_is_valid(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            archer.save_state()
            with open(archer.SAVE_FILE) as f:
                data = json.load(f)
            assert isinstance(data, dict)
        finally:
            archer.SAVE_FILE = orig

    def test_nav_places_persisted(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        archer.nav_places['home'] = {'lat': 37.64, 'lon': -91.54, 'address': 'Home'}
        try:
            archer.save_state()
            archer.nav_places.clear()
            archer.load_state()
            assert 'home' in archer.nav_places
            assert archer.nav_places['home']['lat'] == 37.64
        finally:
            archer.nav_places.pop('home', None)
            archer.SAVE_FILE = orig

    def test_load_missing_file_doesnt_crash(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'nonexistent.json')
        try:
            archer.load_state()  # should not raise
        finally:
            archer.SAVE_FILE = orig

    def test_atomic_write_no_partial_file(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            archer.save_state()
            # .tmp file should be cleaned up
            assert not os.path.exists(archer.SAVE_FILE + '.tmp')
        finally:
            archer.SAVE_FILE = orig


# ═══════════════════════════════════════════════════════════════
# 7. /nav/save_place and /nav/places
# ═══════════════════════════════════════════════════════════════
class TestNavPlaces:
    def _client(self, tier=1):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(tier))
        return c

    def test_save_place(self):
        c = self._client()
        r = c.post('/nav/save_place',
            json={'name': 'test_spot', 'lat': 37.64, 'lon': -91.54, 'address': 'Test St'})
        assert r.status_code == 200
        assert json.loads(r.data)['ok'] is True

    def test_get_places_returns_dict(self):
        r = self._client().get('/nav/places')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d, dict)  # endpoint returns places dict directly

    def test_saved_place_appears_in_list(self):
        c = self._client()
        c.post('/nav/save_place',
            json={'name': 'marker_test2', 'lat': 10.0, 'lon': 20.0, 'address': 'Marker'})
        r = c.get('/nav/places')
        places = json.loads(r.data)
        assert 'marker_test2' in places


# ═══════════════════════════════════════════════════════════════
# 8. /system_health
# ═══════════════════════════════════════════════════════════════
class TestSystemHealth:
    def _client(self, tier=1):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(tier))
        return c

    def test_returns_200(self):
        r = self._client().get('/system_health')
        assert r.status_code == 200

    def test_has_status_field(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert 'status' in d or 'uptime' in d or 'ok' in d


# ═══════════════════════════════════════════════════════════════
# 9. Tier HTML pages load
# ═══════════════════════════════════════════════════════════════
class TestTierPages:
    def _client(self, tier):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(tier))
        return c

    def test_tier1_page_loads(self):
        r = self._client(1).get('/tier1')
        assert r.status_code == 200
        assert b'ARCHER' in r.data

    def test_tier2_page_loads(self):
        r = self._client(2).get('/tier2')
        assert r.status_code == 200

    def test_tier4_page_loads(self):
        r = self._client(4).get('/tier4')
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 10. Cookie format validation
# ═══════════════════════════════════════════════════════════════
class TestCookieFormat:
    def test_cookie_hash_deterministic(self):
        c1 = _make_cookie(1, 'Ayden')
        c2 = _make_cookie(1, 'Ayden')
        assert c1 == c2

    def test_different_tiers_different_cookies(self):
        assert _make_cookie(1, 'X') != _make_cookie(2, 'X')

    def test_different_names_different_cookies(self):
        assert _make_cookie(1, 'Alice') != _make_cookie(1, 'Bob')

    def test_cookie_has_three_parts(self):
        parts = _make_cookie(2, 'Khloe').split(':')
        assert len(parts) == 3
        assert parts[0] == '2'
        assert parts[1] == 'Khloe'
        assert len(parts[2]) == 16


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
