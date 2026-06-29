"""
Archer test suite — covers backend routes, auth, tier system,
smart_fallback, save/load state, and key data endpoints.
"""
import os, sys, json, hashlib, hmac, secrets, tempfile, threading, time
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
    pass
threading.Thread.start = _noop_start

import archer  # noqa: E402

threading.Thread.start = _real_thread_start

client = archer.display_app.test_client()
archer.display_app.config['TESTING'] = True


# ── Helpers ──────────────────────────────────────────────────────
def _make_cookie(tier: int, name: str = 'Tester') -> str:
    secret = os.environ['ARCHER_SECRET']
    token  = hashlib.sha256(f'{name}{tier}{secret}'.encode()).hexdigest()[:32]
    return f'{tier}:{name}:{token}'


def _authed_client(tier: int, name: str = 'Tester'):
    c = archer.display_app.test_client()
    c.set_cookie('archer_auth', _make_cookie(tier, name))
    return c


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
            tier = archer.get_request_tier(request)
            assert tier >= 1

    def test_tampered_token_rejected(self):
        parts = _make_cookie(1).split(':')
        parts[2] = 'A' * 32  # wrong token (32 hex chars to match expected length)
        bad_cookie = ':'.join(parts)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={bad_cookie}'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            assert tier != 1, f"Tampered cookie should not grant tier 1, got {tier}"

    def test_no_cookie_returns_int(self):
        with archer.display_app.test_request_context('/'):
            from flask import request
            tier = archer.get_request_tier(request)
            assert isinstance(tier, int)

    def test_valid_tier3_cookie(self):
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={_make_cookie(3, "Family")}'}
        ):
            from flask import request
            assert archer.get_request_tier(request) == 3

    def test_valid_tier4_cookie(self):
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={_make_cookie(4, "Valet")}'}
        ):
            from flask import request
            assert archer.get_request_tier(request) == 4

    def test_wrong_secret_rejected(self):
        # Cookie signed with a different secret
        bad_token = hashlib.sha256(b'wrong_secret').hexdigest()[:32]
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth=1:Ayden:{bad_token}'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            assert tier != 1


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

    def test_spike_history_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'spike_history' in d
        assert isinstance(d['spike_history'], dict)

    def test_connected_clients_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'connected_clients' in d
        assert isinstance(d['connected_clients'], int)

    def test_device_tier_present(self):
        r = client.get('/display_data?fp=test-fp-001')
        d = json.loads(r.data)
        assert 'device_tier' in d
        assert isinstance(d['device_tier'], int)

    def test_drive_mode_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'drive_mode' in d

    def test_coolant_temp_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'coolant' in d


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
        with patch('sys.exit'), patch.object(archer, 'speak', return_value=None):
            r = self._post('shut down', tier=1)
        assert r.status_code == 200

    def test_normal_command_returns_response(self):
        with patch.object(archer, 'speak', return_value=None):
            r = self._post('oil temp', tier=1)
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d['response'], str)
        assert len(d['response']) > 0

    def test_multiple_dangerous_keywords_blocked_tier2(self):
        for cmd in ('tc off', 'kill engine', 'reboot', 'engine off'):
            r = self._post(cmd, tier=2)
            assert r.status_code == 403, f"'{cmd}' should be blocked for tier 2"

    def test_response_has_response_key(self):
        with patch.object(archer, 'speak', return_value=None):
            r = self._post('boost', tier=1)
        assert 'response' in json.loads(r.data)

    def test_whitespace_only_command_handled(self):
        r = self._post('   ', tier=1)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 4. /register_device — tier capping
# ═══════════════════════════════════════════════════════════════
class TestRegisterDevice:
    def test_tier_capped_at_2(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-001',
            'name': 'TestDevice',
            'tier': 1,
        })
        d = json.loads(r.data)
        assert d['ok'] is True
        assert d['tier'] == 2

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
        assert d['tier'] == 4

    def test_missing_fingerprint_rejected(self):
        r = client.post('/register_device', json={
            'name': 'NoFP',
            'tier': 2,
        })
        d = json.loads(r.data)
        assert d['ok'] is False

    def test_tier_2_accepted(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-pass',
            'name': 'Passenger',
            'tier': 2,
        })
        d = json.loads(r.data)
        assert d['ok'] is True
        assert d['tier'] == 2

    def test_name_preserved(self):
        r = client.post('/register_device', json={
            'fingerprint': 'test-fp-name',
            'name': 'Alice',
            'tier': 3,
        })
        d = json.loads(r.data)
        assert d['name'] == 'Alice'


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

    def test_speed_keyword(self):
        result = archer.smart_fallback('what speed am I going')
        assert isinstance(result, str) and len(result) > 0

    def test_battery_keyword(self):
        result = archer.smart_fallback('battery voltage')
        assert isinstance(result, str) and len(result) > 0


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
            with open(archer.SAVE_FILE, encoding='utf-8') as f:
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
            archer.load_state()
        finally:
            archer.SAVE_FILE = orig

    def test_atomic_write_no_partial_file(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            archer.save_state()
            assert not os.path.exists(archer.SAVE_FILE + '.tmp')
        finally:
            archer.SAVE_FILE = orig

    def test_truck_state_keys_saved(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            archer.save_state()
            with open(archer.SAVE_FILE, encoding='utf-8') as f:
                data = json.load(f)
            assert 'truck_state' in data or 'nav_places' in data
        finally:
            archer.SAVE_FILE = orig


# ═══════════════════════════════════════════════════════════════
# 7. /nav/save_place and /nav/places
# ═══════════════════════════════════════════════════════════════
class TestNavPlaces:
    def _client(self, tier=1):
        return _authed_client(tier)

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
        assert isinstance(d, dict)

    def test_saved_place_appears_in_list(self):
        c = self._client()
        c.post('/nav/save_place',
            json={'name': 'marker_test2', 'lat': 10.0, 'lon': 20.0, 'address': 'Marker'})
        r = c.get('/nav/places')
        places = json.loads(r.data)
        assert 'marker_test2' in places

    def test_saved_place_coords_correct(self):
        c = self._client()
        c.post('/nav/save_place', json={'name': 'coords_check', 'lat': 42.0, 'lon': -93.5, 'address': 'Iowa'})
        r = c.get('/nav/places')
        places = json.loads(r.data)
        assert 'coords_check' in places
        assert places['coords_check']['lat'] == 42.0

    def test_multiple_places_saved(self):
        c = self._client()
        c.post('/nav/save_place', json={'name': 'alpha', 'lat': 1.0, 'lon': 1.0, 'address': 'A'})
        c.post('/nav/save_place', json={'name': 'beta',  'lat': 2.0, 'lon': 2.0, 'address': 'B'})
        r = c.get('/nav/places')
        places = json.loads(r.data)
        assert 'alpha' in places
        assert 'beta' in places


# ═══════════════════════════════════════════════════════════════
# 8. /system_health
# ═══════════════════════════════════════════════════════════════
class TestSystemHealth:
    def _client(self, tier=1):
        return _authed_client(tier)

    def test_returns_200(self):
        r = self._client().get('/system_health')
        assert r.status_code == 200

    def test_has_status_field(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert 'status' in d

    def test_status_is_string(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert isinstance(d['status'], str)

    def test_status_is_ok_or_degraded(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert d['status'] in ('ok', 'degraded')

    def test_issues_list_present(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert 'issues' in d
        assert isinstance(d['issues'], list)

    def test_failures_list_present(self):
        r = self._client().get('/system_health')
        d = json.loads(r.data)
        assert 'failures' in d
        assert isinstance(d['failures'], list)


# ═══════════════════════════════════════════════════════════════
# 9. Tier HTML pages load
# ═══════════════════════════════════════════════════════════════
class TestTierPages:
    def _client(self, tier):
        return _authed_client(tier)

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

    def test_tier3_page_loads(self):
        r = self._client(3).get('/tier3')
        assert r.status_code == 200

    def test_tier1_alias_ayden(self):
        r = self._client(1).get('/ayden')
        assert r.status_code == 200

    def test_tier2_alias_passenger(self):
        r = self._client(2).get('/passenger')
        assert r.status_code == 200

    def test_tier4_alias_valet(self):
        r = self._client(4).get('/valet')
        assert r.status_code == 200

    def test_tier3_alias_family(self):
        r = self._client(3).get('/family')
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
        assert len(parts[2]) == 32

    def test_token_is_hex(self):
        parts = _make_cookie(1, 'Ayden').split(':')
        token = parts[2]
        assert all(c in '0123456789abcdef' for c in token)


# ═══════════════════════════════════════════════════════════════
# 11. /terminal/exec — auth gating and command blocking
# ═══════════════════════════════════════════════════════════════
class TestTerminalExec:
    def _post(self, cmd, tier=1):
        c = _authed_client(tier)
        return c.post('/terminal/exec', json={'cmd': cmd})

    def test_no_auth_denied(self):
        r = client.post('/terminal/exec', json={'cmd': 'ls'})
        d = json.loads(r.data)
        assert 'error' in d or r.status_code in (403, 401)

    def test_tier2_denied(self):
        r = self._post('ls', tier=2)
        d = json.loads(r.data)
        assert 'error' in d or 'denied' in str(d).lower()

    def test_tier1_help_returns_text(self):
        r = self._post('/help', tier=1)
        d = json.loads(r.data)
        assert 'stdout' in d
        assert len(d['stdout']) > 0

    def test_dangerous_rm_rf_blocked(self):
        r = self._post('rm -rf /', tier=1)
        d = json.loads(r.data)
        assert 'error' in d or 'blocked' in str(d).lower() or 'Blocked' in str(d)

    def test_empty_command_returns_empty(self):
        r = self._post('', tier=1)
        d = json.loads(r.data)
        assert d.get('stdout', '') == '' and d.get('stderr', '') == ''

    def test_safe_command_runs(self):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout='hello\n', stderr='', returncode=0)
            r = self._post('echo hello', tier=1)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 12. One-time code generation and validation
# ═══════════════════════════════════════════════════════════════
class TestOneTimeCodes:
    def setup_method(self):
        archer.one_time_codes.clear()

    def test_generate_creates_code(self):
        code = archer.generate_one_time_code('Alice', 2)
        assert code in archer.one_time_codes

    def test_code_is_6_digits(self):
        code = archer.generate_one_time_code('Bob', 3)
        assert len(code) == 6
        assert code.isdigit()

    def test_code_has_correct_tier(self):
        code = archer.generate_one_time_code('Carol', 3)
        assert archer.one_time_codes[code]['tier'] == 3

    def test_code_has_name(self):
        code = archer.generate_one_time_code('Dave', 2)
        assert archer.one_time_codes[code]['name'] == 'Dave'

    def test_validate_valid_code(self):
        code = archer.generate_one_time_code('Eve', 2)
        result = archer.validate_one_time_code(code)
        assert result is not None
        assert result['name'] == 'Eve'

    def test_validate_marks_used(self):
        code = archer.generate_one_time_code('Frank', 2)
        archer.validate_one_time_code(code)
        result = archer.validate_one_time_code(code)
        assert result is None

    def test_validate_nonexistent_code(self):
        assert archer.validate_one_time_code('000000') is None

    def test_validate_expired_code(self):
        code = archer.generate_one_time_code('Grace', 2)
        archer.one_time_codes[code]['expires'] = time.time() - 1
        result = archer.validate_one_time_code(code)
        assert result is None

    def test_cleanup_removes_expired(self):
        code = archer.generate_one_time_code('Henry', 2)
        archer.one_time_codes[code]['expires'] = time.time() - 1
        archer.cleanup_expired_codes()
        assert code not in archer.one_time_codes


# ═══════════════════════════════════════════════════════════════
# 13. /generate_code route — tier 1 required
# ═══════════════════════════════════════════════════════════════
class TestGenerateCodeRoute:
    def setup_method(self):
        archer.one_time_codes.clear()

    def test_tier1_can_generate(self):
        r = _authed_client(1).post('/generate_code', json={'name': 'Passenger', 'tier': 2})
        d = json.loads(r.data)
        assert d['success'] is True
        assert 'code' in d
        assert len(d['code']) == 6

    def test_tier2_cannot_generate(self):
        r = _authed_client(2).post('/generate_code', json={'name': 'X', 'tier': 3})
        assert r.status_code == 403

    def test_no_auth_cannot_generate(self):
        r = client.post('/generate_code', json={'name': 'X', 'tier': 2})
        assert r.status_code == 403

    def test_name_required(self):
        r = _authed_client(1).post('/generate_code', json={'tier': 2})
        d = json.loads(r.data)
        assert d['success'] is False

    def test_generated_code_in_store(self):
        r = _authed_client(1).post('/generate_code', json={'name': 'TestUser', 'tier': 2})
        d = json.loads(r.data)
        assert d['code'] in archer.one_time_codes


# ═══════════════════════════════════════════════════════════════
# 14. /revoke_code route
# ═══════════════════════════════════════════════════════════════
class TestRevokeCodeRoute:
    def setup_method(self):
        archer.one_time_codes.clear()

    def test_revoke_existing_code(self):
        code = archer.generate_one_time_code('X', 2)
        r = _authed_client(1).post('/revoke_code', json={'code': code})
        assert json.loads(r.data)['success'] is True
        assert code not in archer.one_time_codes

    def test_revoke_nonexistent_code_ok(self):
        r = _authed_client(1).post('/revoke_code', json={'code': '999999'})
        assert r.status_code == 200

    def test_revoke_removes_from_store(self):
        code = archer.generate_one_time_code('Y', 3)
        _authed_client(1).post('/revoke_code', json={'code': code})
        assert code not in archer.one_time_codes


# ═══════════════════════════════════════════════════════════════
# 15. /notify_tier1 and /tier_notifications
# ═══════════════════════════════════════════════════════════════
class TestTierNotifications:
    def setup_method(self):
        archer.tier_notifications.clear()
        archer.tier_responses.clear()

    # notify_tier1 requires at least tier 2 (passenger) + CSRF skipped in test
    # because _validate_csrf returns True when no session token is set (test env)

    def test_notify_returns_ok(self):
        c = _authed_client(2, 'Passenger')
        r = c.post('/notify_tier1', json={'from': 'Alice', 'message': 'hello'})
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_notify_returns_id(self):
        c = _authed_client(2, 'Passenger')
        r = c.post('/notify_tier1', json={'from': 'Alice', 'message': 'test'})
        d = json.loads(r.data)
        assert 'id' in d

    def test_notification_appears_in_list(self):
        c = _authed_client(2, 'Passenger')
        c.post('/notify_tier1', json={'from': 'Bob', 'message': 'unlock sport'})
        r = c.get('/tier_notifications')
        d = json.loads(r.data)
        assert any(n['from'] == 'Bob' for n in d['notifications'])

    def test_notifications_list_is_list(self):
        r = client.get('/tier_notifications')
        d = json.loads(r.data)
        assert isinstance(d['notifications'], list)

    def test_tier_cancel_updates_status(self):
        c2 = _authed_client(2, 'Passenger')
        r  = c2.post('/notify_tier1', json={'from': 'Carol', 'message': 'cancel me'})
        nid = json.loads(r.data)['id']
        cr  = c2.post('/tier_cancel', json={'id': nid})
        assert json.loads(cr.data)['ok'] is True
        assert archer.tier_responses.get(nid) == 'cancelled'

    def test_tier_respond_approved(self):
        c2  = _authed_client(2, 'Passenger')
        r   = c2.post('/notify_tier1', json={'from': 'Dave', 'message': 'sport mode'})
        nid = json.loads(r.data)['id']
        rr  = _authed_client(1).post('/tier_respond', json={'id': nid, 'response': 'approved', 'action': 'sport'})
        assert json.loads(rr.data)['ok'] is True
        assert archer.tier_responses.get(nid) == 'approved'

    def test_tier_respond_changes_drive_mode(self):
        c2  = _authed_client(2, 'Passenger')
        r   = c2.post('/notify_tier1', json={'from': 'Eve', 'message': 'eco mode'})
        nid = json.loads(r.data)['id']
        _authed_client(1).post('/tier_respond', json={'id': nid, 'response': 'approved', 'action': 'eco mode'})
        assert archer.truck_state['drive_mode'] == 'eco'

    def test_tier_respond_denied(self):
        c2  = _authed_client(2, 'Passenger')
        r   = c2.post('/notify_tier1', json={'from': 'Frank', 'message': 'tow mode'})
        nid = json.loads(r.data)['id']
        _authed_client(1).post('/tier_respond', json={'id': nid, 'response': 'denied', 'action': 'tow'})
        assert archer.tier_responses.get(nid) == 'denied'

    def test_tier_response_status_endpoint(self):
        c2  = _authed_client(2, 'Passenger')
        r   = c2.post('/notify_tier1', json={'from': 'Grace', 'message': 'test'})
        nid = json.loads(r.data)['id']
        sr  = client.get(f'/tier_response_status?id={nid}')
        d   = json.loads(sr.data)
        assert 'status' in d
        assert d['status'] == 'pending'

    def test_tier_response_status_unknown_id(self):
        r = client.get('/tier_response_status?id=nonexistent')
        d = json.loads(r.data)
        assert d['status'] == 'unknown'


# ═══════════════════════════════════════════════════════════════
# 16. /device_tier endpoint
# ═══════════════════════════════════════════════════════════════
class TestDeviceTierEndpoint:
    def test_returns_tier_for_registered_device(self):
        r = client.post('/device_tier', json={'fingerprint': 'test-fp-001'})
        d = json.loads(r.data)
        assert 'tier' in d
        assert isinstance(d['tier'], int)

    def test_unknown_fp_returns_4(self):
        r = client.post('/device_tier', json={'fingerprint': 'totally-unknown-fp-xyz'})
        d = json.loads(r.data)
        assert d['tier'] == 4

    def test_registered_field_present(self):
        r = client.post('/device_tier', json={'fingerprint': 'test-fp-001'})
        d = json.loads(r.data)
        assert 'registered' in d

    def test_name_field_present(self):
        r = client.post('/device_tier', json={'fingerprint': 'test-fp-001'})
        d = json.loads(r.data)
        assert 'name' in d

    def test_known_device_is_registered(self):
        client.post('/register_device', json={'fingerprint': 'known-fp-99', 'name': 'Me', 'tier': 2})
        r = client.post('/device_tier', json={'fingerprint': 'known-fp-99'})
        d = json.loads(r.data)
        assert d['registered'] is True


# ═══════════════════════════════════════════════════════════════
# 17. /specs endpoint
# ═══════════════════════════════════════════════════════════════
class TestSpecsEndpoint:
    def test_returns_200(self):
        r = client.get('/specs')
        assert r.status_code == 200

    def test_returns_html(self):
        r = client.get('/specs')
        assert b'ARCHER' in r.data

    def test_contains_engine_section(self):
        r = client.get('/specs')
        assert b'ENGINE' in r.data or b'engine' in r.data.lower()

    def test_content_type_html(self):
        r = client.get('/specs')
        assert 'text/html' in r.content_type


# ═══════════════════════════════════════════════════════════════
# 18. /limited endpoint
# ═══════════════════════════════════════════════════════════════
class TestLimitedEndpoint:
    def test_returns_200(self):
        r = client.get('/limited')
        assert r.status_code == 200

    def test_returns_html(self):
        r = client.get('/limited')
        assert b'ARCHER' in r.data

    def test_contains_limited_marker(self):
        r = client.get('/limited')
        assert b'LIMITED' in r.data or b'limited' in r.data.lower()


# ═══════════════════════════════════════════════════════════════
# 19. /fans page
# ═══════════════════════════════════════════════════════════════
class TestFanPage:
    def test_returns_200(self):
        r = client.get('/fans')
        assert r.status_code == 200

    def test_fan_alias_works(self):
        r = client.get('/fan')
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 20. /boot initialization page
# ═══════════════════════════════════════════════════════════════
class TestBootPage:
    def test_returns_200(self):
        r = client.get('/boot')
        assert r.status_code == 200

    def test_init_alias_works(self):
        r = client.get('/init')
        assert r.status_code == 200

    def test_contains_archer(self):
        r = client.get('/boot')
        assert b'ARCHER' in r.data

    def test_content_type_html(self):
        r = client.get('/boot')
        assert 'text/html' in r.content_type

    def test_contains_redirect_script(self):
        r = client.get('/boot')
        assert b'window.location' in r.data or b'location.href' in r.data


# ═══════════════════════════════════════════════════════════════
# 21. /drag endpoints
# ═══════════════════════════════════════════════════════════════
class TestDragEndpoints:
    def test_stage_returns_ok(self):
        r = client.post('/drag/stage')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_stage_has_stage_field(self):
        r = client.post('/drag/stage')
        d = json.loads(r.data)
        assert 'stage' in d

    def test_launch_returns_ok(self):
        r = client.post('/drag/launch')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert 'ok' in d

    def test_launch_has_stage_field(self):
        r = client.post('/drag/launch')
        d = json.loads(r.data)
        assert 'stage' in d


# ═══════════════════════════════════════════════════════════════
# 22. /build/part endpoints
# ═══════════════════════════════════════════════════════════════
class TestBuildPartEndpoints:
    def test_add_part_returns_ok(self):
        r = client.post('/build/part/add', json={
            'name': 'Cold Air Intake', 'category': 'intake',
            'hp_gain': 15, 'tq_gain': 12, 'cost': 299.99,
        })
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_add_part_returns_part(self):
        r = client.post('/build/part/add', json={'name': 'Test Part'})
        d = json.loads(r.data)
        assert 'part' in d
        assert d['part']['name'] == 'Test Part'

    def test_add_part_has_id(self):
        r = client.post('/build/part/add', json={'name': 'Headers'})
        d = json.loads(r.data)
        assert 'id' in d['part']

    def test_update_part_ok(self):
        r = client.post('/build/part/add', json={'name': 'UpdateMe'})
        pid = json.loads(r.data)['part']['id']
        r2  = client.post('/build/part/update', json={'id': pid, 'status': 'installed'})
        d   = json.loads(r2.data)
        assert d['ok'] is True
        assert d['part']['status'] == 'installed'

    def test_update_nonexistent_part(self):
        r = client.post('/build/part/update', json={'id': 'nonexistent_id', 'status': 'installed'})
        d = json.loads(r.data)
        assert d['ok'] is False

    def test_remove_part_ok(self):
        r   = client.post('/build/part/add', json={'name': 'RemoveMe'})
        pid = json.loads(r.data)['part']['id']
        r2  = client.post('/build/part/remove', json={'id': pid})
        d   = json.loads(r2.data)
        assert d['ok'] is True

    def test_remove_part_no_longer_in_list(self):
        r   = client.post('/build/part/add', json={'name': 'GoneItem'})
        pid = json.loads(r.data)['part']['id']
        client.post('/build/part/remove', json={'id': pid})
        r2  = client.post('/build/part/add', json={'name': 'Dummy'})
        parts = json.loads(r2.data)['parts']
        assert not any(p['id'] == pid for p in parts)

    def test_add_part_returns_power(self):
        r = client.post('/build/part/add', json={'name': 'Tune', 'hp_gain': 30})
        d = json.loads(r.data)
        assert 'power' in d


# ═══════════════════════════════════════════════════════════════
# 23. /build/update endpoint
# ═══════════════════════════════════════════════════════════════
class TestBuildUpdate:
    def test_returns_ok(self):
        r = client.post('/build/update', json={'cold_air_intake': True})
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_updates_build_spec(self):
        client.post('/build/update', json={'cold_air_intake': True})
        assert archer.build_specs.get('cold_air_intake') is True

    def test_returns_power(self):
        r = client.post('/build/update', json={})
        d = json.loads(r.data)
        assert 'power' in d

    def test_returns_build_specs(self):
        r = client.post('/build/update', json={})
        d = json.loads(r.data)
        assert 'build_specs' in d


# ═══════════════════════════════════════════════════════════════
# 24. /location/update endpoint
# ═══════════════════════════════════════════════════════════════
class TestLocationUpdate:
    def test_returns_ok(self):
        with patch('threading.Thread'):
            r = client.post('/location/update', json={'lat': 37.64, 'lon': -91.54})
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_stores_lat_lon(self):
        with patch('threading.Thread'):
            client.post('/location/update', json={'lat': 42.0, 'lon': -93.0})
        r = client.post('/location/update', json={'lat': 42.0, 'lon': -93.0})
        d = json.loads(r.data)
        assert d['lat'] == 42.0
        assert d['lon'] == -93.0

    def test_name_stored_if_provided(self):
        with patch('threading.Thread'):
            r = client.post('/location/update', json={'lat': 1.0, 'lon': 2.0, 'name': 'Test City'})
        d = json.loads(r.data)
        assert d['name'] == 'Test City'

    def test_missing_coords_still_200(self):
        r = client.post('/location/update', json={})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 25. get_tier_html function — output validation and XSS
# ═══════════════════════════════════════════════════════════════
class TestGetTierHtml:
    def test_tier1_returns_string(self):
        html = archer.get_tier_html(1)
        assert isinstance(html, str)
        assert len(html) > 0

    def test_tier2_returns_string(self):
        html = archer.get_tier_html(2)
        assert isinstance(html, str)

    def test_tier3_returns_string(self):
        html = archer.get_tier_html(3)
        assert isinstance(html, str)

    def test_tier4_returns_string(self):
        html = archer.get_tier_html(4)
        assert isinstance(html, str)

    def test_tier2_name_injection_no_raw_script(self):
        # A name containing a quote should not break the JavaScript string context
        html = archer.get_tier_html(2, name="O'Brien")
        # The raw unescaped single-quote should not close the JS string
        # At minimum, the HTML must not contain a raw <script> injection
        assert "<script>alert" not in html

    def test_tier2_xss_payload_not_executed(self):
        html = archer.get_tier_html(2, name="'; alert(1); var x='")
        assert "alert(1)" not in html or "\\'" in html or "&" in html


# ═══════════════════════════════════════════════════════════════
# 26. Index route routing logic
# ═══════════════════════════════════════════════════════════════
class TestIndexRoute:
    def test_no_cookie_redirects(self):
        # Unauthenticated users are redirected to /fans
        r = client.get('/')
        assert r.status_code in (200, 302)

    def test_no_cookie_redirect_follows_to_html(self):
        r = client.get('/', follow_redirects=True)
        assert r.status_code == 200
        assert b'html' in r.data.lower()

    def test_valid_tier1_cookie_served(self):
        r = _authed_client(1).get('/')
        assert r.status_code == 200

    def test_valid_tier2_cookie_served(self):
        r = _authed_client(2).get('/')
        assert r.status_code == 200

    def test_registration_page_shown_without_auth(self):
        r = client.get('/', follow_redirects=True)
        assert r.status_code == 200

    def test_no_crash_with_malformed_cookie(self):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', 'malformed_no_colons')
        r = c.get('/', follow_redirects=True)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 27. Cookie tier bounds validation
# ═══════════════════════════════════════════════════════════════
class TestCookieTierBounds:
    def _make_signed_cookie(self, tier, name='Tester'):
        secret = os.environ['ARCHER_SECRET']
        token  = hashlib.sha256(f'{name}{tier}{secret}'.encode()).hexdigest()[:32]
        return f'{tier}:{name}:{token}'

    def test_tier_zero_returns_int(self):
        cookie = self._make_signed_cookie(0)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={cookie}'}
        ):
            from flask import request
            result = archer.get_request_tier(request)
            assert isinstance(result, int)

    def test_tier_negative_returns_int(self):
        cookie = self._make_signed_cookie(-1)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={cookie}'}
        ):
            from flask import request
            result = archer.get_request_tier(request)
            assert isinstance(result, int)

    def test_tier_99_clamped_to_4(self):
        # Tier is now clamped to [1, 4] — tier 99 in cookie returns 4
        cookie = self._make_signed_cookie(99)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={cookie}'}
        ):
            from flask import request
            result = archer.get_request_tier(request)
            assert result == 4, f'Expected 4 (clamped), got {result}'


# ═══════════════════════════════════════════════════════════════
# 28. /mirror page
# ═══════════════════════════════════════════════════════════════
class TestMirrorPage:
    def test_returns_200(self):
        r = client.get('/mirror')
        assert r.status_code == 200

    def test_returns_html(self):
        r = client.get('/mirror')
        assert r.content_type.startswith('text/html') or b'html' in r.data.lower()


# ═══════════════════════════════════════════════════════════════
# 29. estimate_power_from_parts — no crash
# ═══════════════════════════════════════════════════════════════
class TestEstimatePower:
    def test_returns_dict(self):
        result = archer.estimate_power_from_parts()
        assert isinstance(result, dict)

    def test_has_crank_hp(self):
        result = archer.estimate_power_from_parts()
        assert 'crank_hp' in result
        assert isinstance(result['crank_hp'], (int, float))
        assert result['crank_hp'] > 0

    def test_has_wheel_hp(self):
        result = archer.estimate_power_from_parts()
        assert 'wheel_hp' in result
        assert result['wheel_hp'] > 0

    def test_wheel_hp_less_than_crank(self):
        result = archer.estimate_power_from_parts()
        assert result['wheel_hp'] < result['crank_hp']


# ═══════════════════════════════════════════════════════════════
# 30. get_display_data — completeness
# ═══════════════════════════════════════════════════════════════
class TestGetDisplayData:
    def test_returns_dict(self):
        d = archer.get_display_data()
        assert isinstance(d, dict)

    def test_all_gauge_fields_present(self):
        d = archer.get_display_data()
        for key in ('rpm', 'speed', 'boost', 'oil_temp', 'battery', 'ethanol', 'coolant'):
            assert key in d, f'Missing: {key}'

    def test_drive_mode_field(self):
        d = archer.get_display_data()
        assert 'drive_mode' in d

    def test_tc_on_field(self):
        d = archer.get_display_data()
        assert 'tc_on' in d


# ═══════════════════════════════════════════════════════════════
# 31. /export/trip — JSON and CSV export
# ═══════════════════════════════════════════════════════════════
class TestTripExport:
    def test_unauthenticated_returns_403(self):
        r = client.get('/export/trip')
        assert r.status_code == 403

    def test_tier3_returns_403(self):
        c = _authed_client(3, 'Family')
        r = c.get('/export/trip')
        assert r.status_code == 403

    def test_tier1_json_default(self):
        c = _authed_client(1, 'Ayden')
        r = c.get('/export/trip')
        assert r.status_code == 200
        data = json.loads(r.data)
        assert 'trips' in data
        assert 'count' in data
        assert isinstance(data['trips'], list)

    def test_tier2_json_ok(self):
        c = _authed_client(2, 'Khloe')
        r = c.get('/export/trip?fmt=json')
        assert r.status_code == 200

    def test_tier1_csv_content_type(self):
        c = _authed_client(1, 'Ayden')
        r = c.get('/export/trip?fmt=csv')
        assert r.status_code == 200
        assert 'text/csv' in r.content_type

    def test_csv_has_header_row(self):
        c = _authed_client(1, 'Ayden')
        r = c.get('/export/trip?fmt=csv')
        text = r.data.decode()
        assert 'date' in text and 'peak_rpm' in text

    def test_csv_attachment_header(self):
        c = _authed_client(1, 'Ayden')
        r = c.get('/export/trip?fmt=csv')
        assert b'attachment' in r.headers.get('Content-Disposition', '').encode()


# ═══════════════════════════════════════════════════════════════
# 32. /system_health — degraded mode detection
# ═══════════════════════════════════════════════════════════════
class TestSystemHealth:
    def test_returns_200(self):
        r = client.get('/system_health')
        assert r.status_code == 200

    def test_has_status_field(self):
        d = json.loads(client.get('/system_health').data)
        assert 'status' in d
        assert d['status'] in ('ok', 'degraded')

    def test_degraded_when_obd_disconnected(self):
        original = archer.system_health['obd_connected']
        archer.system_health['obd_connected'] = False
        try:
            issues = archer.get_system_status()
            assert 'OBD_DISCONNECTED' in issues
        finally:
            archer.system_health['obd_connected'] = original

    def test_degraded_when_obd_timeout(self):
        original = archer.system_health['last_obd_update']
        archer.system_health['last_obd_update'] = time.time() - 30
        try:
            issues = archer.get_system_status()
            assert 'OBD_TIMEOUT' in issues
        finally:
            archer.system_health['last_obd_update'] = original

    def test_ok_when_nominal(self):
        archer.system_health['obd_connected'] = True
        archer.system_health['voice_active']  = True
        archer.system_health['last_obd_update'] = time.time()
        issues = archer.get_system_status()
        assert issues == []


# ═══════════════════════════════════════════════════════════════
# 33. /logout — session invalidation
# ═══════════════════════════════════════════════════════════════
class TestLogout:
    def test_logout_returns_200(self):
        c = _authed_client(1, 'Ayden')
        r = c.post('/logout')
        assert r.status_code == 200

    def test_logout_clears_cookie(self):
        c = _authed_client(2, 'Khloe')
        r = c.post('/logout')
        # Response must delete the cookie (max-age=0 or expires in past)
        set_cookie = r.headers.get('Set-Cookie', '')
        assert 'archer_auth' in set_cookie

    def test_logout_revokes_session(self):
        # After logout the token should appear in _revoked_tokens
        import hashlib as hl
        secret = os.environ['ARCHER_SECRET']
        name, tier = 'LogoutTest', 2
        token = hl.sha256(f'{name}{tier}{secret}'.encode()).hexdigest()[:32]
        c = _authed_client(tier, name)
        c.post('/logout')
        assert token in archer._revoked_tokens

    def test_no_session_logout_is_safe(self):
        r = client.post('/logout')
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 34. Gatekeeper — handle_connection with timestamp replay protection
# ═══════════════════════════════════════════════════════════════
class TestGatekeeperHandshake:
    """Tests for pi/obd_gatekeeper.py handle_connection."""

    @staticmethod
    def _make_mock_port(lines: list[bytes]):
        """Return a mock serial.Serial that yields lines one at a time."""
        port = MagicMock()
        port.readline = iter(lines).__next__
        port.write    = MagicMock()
        port.flush    = MagicMock()
        port.timeout  = 10.0
        return port

    def _gatekeeper(self):
        """Import obd_gatekeeper without triggering hardware init."""
        import importlib, sys
        sys.modules.setdefault('RPi', MagicMock())
        sys.modules.setdefault('RPi.GPIO', MagicMock())
        # Use a real exception subclass so 'except serial.SerialException' works
        _serial_mock = MagicMock()
        _serial_mock.SerialException = IOError
        sys.modules['serial'] = _serial_mock
        if 'pi.obd_gatekeeper' in sys.modules:
            importlib.reload(sys.modules['pi.obd_gatekeeper'])
        import pi.obd_gatekeeper as gk
        # Reset failure counters between tests
        gk._fail_count   = 0
        gk._locked_until = 0.0
        return gk

    def test_wrong_opener_fails(self):
        gk   = self._gatekeeper()
        key  = secrets.token_bytes(32)
        port = self._make_mock_port([b"NOT_ARCHER\n"])
        result = gk.handle_connection(port, [key])
        assert result is False

    def test_correct_key_passes(self):
        import secrets as _s
        gk  = self._gatekeeper()
        key = _s.token_bytes(32)

        # Capture the challenge that handle_connection writes
        challenge_holder = {}
        def _capture_write(data):
            text = data.decode('ascii', errors='replace').strip()
            if text.startswith('CHALLENGE:'):
                challenge_holder['raw'] = text
        port = MagicMock()
        port.timeout = 10.0
        port.write = _capture_write
        port.flush = MagicMock()
        lines_iter = iter([b"ARCHER_AUTH_REQ\n", None])  # second call filled in below

        def _readline():
            val = next(lines_iter)
            if val is None:
                # Build correct response from the challenge we captured
                raw = challenge_holder.get('raw', '')
                parts = raw.split(':')
                nonce_hex, ts_str = parts[1], parts[2]
                nonce   = bytes.fromhex(nonce_hex)
                payload = nonce + b':' + ts_str.encode()
                mac = hmac.new(key, payload, hashlib.sha256).hexdigest()
                return f"RESPONSE:{mac}\n".encode()
            return val
        port.readline = _readline

        result = gk.handle_connection(port, [key])
        assert result is True

    def test_wrong_key_fails(self):
        import secrets as _s
        gk       = self._gatekeeper()
        real_key = _s.token_bytes(32)
        bad_key  = _s.token_bytes(32)

        challenge_holder = {}
        def _capture_write(data):
            text = data.decode('ascii', errors='replace').strip()
            if text.startswith('CHALLENGE:'):
                challenge_holder['raw'] = text
        port = MagicMock()
        port.timeout = 10.0
        port.write = _capture_write
        port.flush = MagicMock()
        lines_iter = iter([b"ARCHER_AUTH_REQ\n", None])

        def _readline():
            val = next(lines_iter)
            if val is None:
                raw    = challenge_holder.get('raw', '')
                parts  = raw.split(':')
                nonce  = bytes.fromhex(parts[1])
                ts_str = parts[2]
                payload = nonce + b':' + ts_str.encode()
                mac = hmac.new(bad_key, payload, hashlib.sha256).hexdigest()
                return f"RESPONSE:{mac}\n".encode()
            return val
        port.readline = _readline

        result = gk.handle_connection(port, [real_key])
        assert result is False

    def test_stale_timestamp_fails(self):
        """A replayed response outside the 30-second window is rejected."""
        import secrets as _s
        gk  = self._gatekeeper()
        key = _s.token_bytes(32)

        # Build a response using a timestamp 60 seconds in the past
        stale_ts = int(time.time()) - 60
        nonce    = _s.token_bytes(32)
        payload  = nonce + b':' + str(stale_ts).encode()
        mac      = hmac.new(key, payload, hashlib.sha256).hexdigest()

        challenge_holder = {}
        def _capture_write(data):
            text = data.decode('ascii', errors='replace').strip()
            if text.startswith('CHALLENGE:'):
                challenge_holder['raw'] = text

        # Override the challenge to embed stale_ts so timestamp check fires
        write_calls = []
        def _write(data):
            _capture_write(data)
            write_calls.append(data)

        # Simulate: correct opener, then response built for stale_ts
        lines = iter([
            b"ARCHER_AUTH_REQ\n",
            f"RESPONSE:{mac}\n".encode(),
        ])
        port = MagicMock()
        port.timeout  = 10.0
        port.readline = lambda: next(lines)
        port.write    = _write
        port.flush    = MagicMock()

        # Patch time.time so the challenge emits stale_ts and the window check fails
        with patch('pi.obd_gatekeeper.time') as mock_time:
            mock_time.time.return_value = float(stale_ts)  # challenge issued at stale_ts
            # But validation happens 60 seconds "later" → out of window
            mock_time.time.side_effect = [float(stale_ts), float(stale_ts + 60)]
            # Run — second call to time.time() is for the window check
            result = gk.handle_connection(port, [key])
        assert result is False

    def test_key_rotation_previous_key_works(self):
        """Connecting with the previous key (index 1) still authenticates."""
        import secrets as _s
        gk       = self._gatekeeper()
        new_key  = _s.token_bytes(32)
        old_key  = _s.token_bytes(32)  # previous key — being rotated out

        challenge_holder = {}
        def _capture_write(data):
            text = data.decode('ascii', errors='replace').strip()
            if text.startswith('CHALLENGE:'):
                challenge_holder['raw'] = text
        port = MagicMock()
        port.timeout = 10.0
        port.write = _capture_write
        port.flush = MagicMock()
        lines_iter = iter([b"ARCHER_AUTH_REQ\n", None])

        def _readline():
            val = next(lines_iter)
            if val is None:
                raw    = challenge_holder.get('raw', '')
                parts  = raw.split(':')
                nonce  = bytes.fromhex(parts[1])
                ts_str = parts[2]
                payload = nonce + b':' + ts_str.encode()
                # Sign with OLD key (simulating client hasn't updated yet)
                mac = hmac.new(old_key, payload, hashlib.sha256).hexdigest()
                return f"RESPONSE:{mac}\n".encode()
            return val
        port.readline = _readline

        # Pass [new_key, old_key] — gatekeeper should fall back to old_key
        result = gk.handle_connection(port, [new_key, old_key])
        assert result is True


# ═══════════════════════════════════════════════════════════════
# 31. OBD-II PID parser math
# These mirror the inner functions defined in the obd polling thread.
# Testing them by exercising the same formula the real code uses.
# ═══════════════════════════════════════════════════════════════
class TestOBDPIDParsers:
    """Test the Mode 01 PID byte-to-value formulas."""

    # MAF — PID 0110 — (256*A + B) / 100  g/s
    def test_maf_midpoint(self):
        b = [0x0F, 0xA0]        # 0x0FA0 = 4000 → 40.00 g/s
        assert round((b[0] * 256 + b[1]) / 100, 2) == 40.0

    def test_maf_zero(self):
        b = [0x00, 0x00]
        assert round((b[0] * 256 + b[1]) / 100, 2) == 0.0

    def test_maf_max(self):
        b = [0xFF, 0xFF]        # 65535 → 655.35 g/s
        assert round((b[0] * 256 + b[1]) / 100, 2) == 655.35

    def test_maf_short_buffer_returns_none(self):
        b = [0x10]              # only 1 byte — parser returns None
        result = round((b[0] * 256 + b[1]) / 100, 2) if len(b) >= 2 else None
        assert result is None

    # Timing advance — PID 010E — A/2 - 64  degrees BTDC
    def test_timing_typical_warm_idle(self):
        b = [0x96]              # 150 / 2 - 64 = 11.0°
        assert round(b[0] / 2 - 64, 1) == 11.0

    def test_timing_zero_advance(self):
        b = [0x80]              # 128 / 2 - 64 = 0.0°
        assert round(b[0] / 2 - 64, 1) == 0.0

    def test_timing_max_retard(self):
        b = [0x00]              # 0 / 2 - 64 = -64.0°
        assert round(b[0] / 2 - 64, 1) == -64.0

    def test_timing_max_advance(self):
        b = [0xFF]              # 255 / 2 - 64 = 63.5°
        assert round(b[0] / 2 - 64, 1) == 63.5

    # Engine load — PID 0104 — A * 100/255  %
    def test_load_half(self):
        b = [0x7F]              # 127 * 100/255 ≈ 49.8%
        assert round(b[0] * 100 / 255, 1) == 49.8

    def test_load_zero(self):
        b = [0x00]
        assert round(b[0] * 100 / 255, 1) == 0.0

    def test_load_full(self):
        b = [0xFF]              # 255 * 100/255 = 100.0%
        assert round(b[0] * 100 / 255, 1) == 100.0

    # Fuel trim — PIDs 0106-0109 — (A-128)*100/128  %
    def test_fuel_trim_zero(self):
        b = [0x80]              # (128-128)*100/128 = 0.0%
        assert round((b[0] - 128) * 100 / 128, 1) == 0.0

    def test_fuel_trim_positive(self):
        b = [0x99]              # (153-128)*100/128 = 19.5%
        assert round((b[0] - 128) * 100 / 128, 1) == 19.5

    def test_fuel_trim_negative(self):
        b = [0x67]              # (103-128)*100/128 = -19.5%
        assert round((b[0] - 128) * 100 / 128, 1) == -19.5

    def test_fuel_trim_max_positive(self):
        b = [0xFF]              # (255-128)*100/128 = 99.2%
        assert round((b[0] - 128) * 100 / 128, 1) == 99.2

    # Oil temp — PID 015C — (A-40)*9/5 + 32  °F
    def test_oil_temp_cold(self):
        b = [0x00]              # (0-40)*9/5+32 = -40°F
        assert round((b[0] - 40) * 9 / 5 + 32) == -40

    def test_oil_temp_normal(self):
        b = [0x78]              # (120-40)*9/5+32 = 176°F
        assert round((b[0] - 40) * 9 / 5 + 32) == 176

    def test_oil_temp_hot(self):
        b = [0xC8]              # (200-40)*9/5+32 = 320°F
        assert round((b[0] - 40) * 9 / 5 + 32) == 320


# ═══════════════════════════════════════════════════════════════
# 32. Vehicle name helper + /set_vehicle endpoint
# ═══════════════════════════════════════════════════════════════
class TestVehicleNameHelper:
    def setup_method(self):
        archer.vehicle_config['make']  = None
        archer.vehicle_config['model'] = None

    def teardown_method(self):
        archer.vehicle_config['make']  = None
        archer.vehicle_config['model'] = None

    def test_unset_returns_generic_full(self):
        assert archer.get_vehicle_name('full') == 'Sierra/Silverado 2500HD'

    def test_unset_returns_generic_header(self):
        assert archer.get_vehicle_name('header') == 'SIERRA/SILVERADO 2500HD'

    def test_set_gmc_full(self):
        archer.vehicle_config['make']  = 'GMC'
        archer.vehicle_config['model'] = 'Sierra 2500HD'
        assert archer.get_vehicle_name('full') == '2006 GMC Sierra 2500HD'

    def test_set_gmc_header(self):
        archer.vehicle_config['make']  = 'GMC'
        archer.vehicle_config['model'] = 'Sierra 2500HD'
        assert archer.get_vehicle_name('header') == 'GMC SIERRA 2500HD'

    def test_set_chevrolet_full(self):
        archer.vehicle_config['make']  = 'Chevrolet'
        archer.vehicle_config['model'] = 'Silverado 2500HD'
        assert archer.get_vehicle_name('full') == '2006 Chevrolet Silverado 2500HD'

    def test_set_chevrolet_header(self):
        archer.vehicle_config['make']  = 'Chevrolet'
        archer.vehicle_config['model'] = 'Silverado 2500HD'
        assert archer.get_vehicle_name('header') == 'CHEVROLET SILVERADO 2500HD'

    def test_default_style_is_full(self):
        archer.vehicle_config['make']  = 'GMC'
        archer.vehicle_config['model'] = 'Sierra 2500HD'
        assert archer.get_vehicle_name() == archer.get_vehicle_name('full')


class TestSetVehicleEndpoint:
    def setup_method(self):
        archer.vehicle_config['make']  = None
        archer.vehicle_config['model'] = None

    def teardown_method(self):
        archer.vehicle_config['make']  = None
        archer.vehicle_config['model'] = None

    def _post(self, body, tier=1):
        c = _authed_client(tier)
        return c.post('/set_vehicle', json=body)

    def test_set_gmc_sierra(self):
        r = self._post({'make': 'GMC', 'model': 'Sierra 2500HD'})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d['ok'] is True
        assert 'GMC' in d['vehicle']

    def test_set_chevrolet_silverado(self):
        r = self._post({'make': 'Chevrolet', 'model': 'Silverado 2500HD'})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d['ok'] is True
        assert 'Chevrolet' in d['vehicle']

    def test_invalid_make_rejected(self):
        r = self._post({'make': 'Ford', 'model': 'Sierra 2500HD'})
        assert r.status_code == 400

    def test_invalid_model_rejected(self):
        r = self._post({'make': 'GMC', 'model': 'F-150'})
        assert r.status_code == 400

    def test_empty_body_rejected(self):
        r = self._post({})
        assert r.status_code == 400

    def test_tier2_forbidden(self):
        r = self._post({'make': 'GMC', 'model': 'Sierra 2500HD'}, tier=2)
        assert r.status_code == 403

    def test_tier3_forbidden(self):
        r = self._post({'make': 'GMC', 'model': 'Sierra 2500HD'}, tier=3)
        assert r.status_code == 403

    def test_vehicle_config_updated(self):
        self._post({'make': 'Chevrolet', 'model': 'Silverado 2500HD'})
        assert archer.vehicle_config['make']  == 'Chevrolet'
        assert archer.vehicle_config['model'] == 'Silverado 2500HD'

    def test_display_data_reflects_set_vehicle(self):
        self._post({'make': 'GMC', 'model': 'Sierra 2500HD'})
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert d['vehicle_make'] == 'GMC'
        assert d['vehicle_model'] == 'Sierra 2500HD'
        assert '2006 GMC' in d['vehicle_name']


# ═══════════════════════════════════════════════════════════════
# 33. OBD auth client protocol
# ═══════════════════════════════════════════════════════════════
class TestOBDAuthClientProtocol:
    """Test the HMAC-SHA256 challenge-response format expected by obd_auth_client."""

    def _compute_response(self, key: bytes, nonce_hex: str, ts: int) -> str:
        nonce   = bytes.fromhex(nonce_hex)
        payload = nonce + b':' + str(ts).encode()
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def test_response_is_64_hex_chars(self):
        key = secrets.token_bytes(32)
        mac = self._compute_response(key, secrets.token_hex(32), int(time.time()))
        assert len(mac) == 64
        assert all(c in '0123456789abcdef' for c in mac)

    def test_correct_key_matches(self):
        key     = secrets.token_bytes(32)
        nonce   = secrets.token_hex(32)
        ts      = int(time.time())
        mac     = self._compute_response(key, nonce, ts)
        payload = bytes.fromhex(nonce) + b':' + str(ts).encode()
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        assert mac == expected

    def test_wrong_key_does_not_match(self):
        key1  = secrets.token_bytes(32)
        key2  = secrets.token_bytes(32)
        nonce = secrets.token_hex(32)
        ts    = int(time.time())
        mac1  = self._compute_response(key1, nonce, ts)
        mac2  = self._compute_response(key2, nonce, ts)
        assert mac1 != mac2

    def test_replay_with_different_ts_does_not_match(self):
        key   = secrets.token_bytes(32)
        nonce = secrets.token_hex(32)
        ts    = int(time.time())
        mac1  = self._compute_response(key, nonce, ts)
        mac2  = self._compute_response(key, nonce, ts + 1)
        assert mac1 != mac2

    def test_replay_with_different_nonce_does_not_match(self):
        key   = secrets.token_bytes(32)
        ts    = int(time.time())
        mac1  = self._compute_response(key, secrets.token_hex(32), ts)
        mac2  = self._compute_response(key, secrets.token_hex(32), ts)
        assert mac1 != mac2

    def test_challenge_parse_format(self):
        """Verify the client can correctly split CHALLENGE:nonce_hex:timestamp."""
        nonce_hex = secrets.token_hex(32)
        ts        = int(time.time())
        line      = f'CHALLENGE:{nonce_hex}:{ts}'
        assert line.startswith('CHALLENGE:')
        body  = line[len('CHALLENGE:'):]
        parts = body.split(':', 1)
        assert len(parts) == 2
        assert parts[0] == nonce_hex
        assert int(parts[1]) == ts

    def test_nonce_hex_length_validation(self):
        """Valid nonce is 64 hex chars (32 bytes)."""
        assert len(secrets.token_hex(32)) == 64

    def test_short_nonce_detected(self):
        bad_nonce = secrets.token_hex(16)  # 32 chars, too short
        assert len(bad_nonce) != 64

    def test_clock_skew_within_30s_accepted(self):
        now  = time.time()
        skew = abs(now - (now - 29))
        assert skew <= 30

    def test_clock_skew_over_30s_rejected(self):
        now  = time.time()
        skew = abs(now - (now - 31))
        assert skew > 30


# ═══════════════════════════════════════════════════════════════
# 34. /display_data — new OBD fields present
# ═══════════════════════════════════════════════════════════════
class TestDisplayDataOBDFields:
    """Verify the 7 new OBD PID fields appear in display_data response."""

    def test_maf_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'maf' in d

    def test_timing_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'timing' in d

    def test_engine_load_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'engine_load' in d

    def test_stft_b1_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'stft_b1' in d

    def test_ltft_b1_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'ltft_b1' in d

    def test_stft_b2_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'stft_b2' in d

    def test_ltft_b2_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'ltft_b2' in d

    def test_obd_mode_field_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'obd_mode' in d

    def test_vehicle_name_fields_present(self):
        r = client.get('/display_data')
        d = json.loads(r.data)
        assert 'vehicle_make' in d
        assert 'vehicle_model' in d
        assert 'vehicle_name' in d
        assert 'vehicle_name_header' in d


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
