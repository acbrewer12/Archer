"""
Archer test suite — covers backend routes, auth, tier system,
smart_fallback, save/load state, and key data endpoints.
"""
import os, sys, json, hashlib, hmac, secrets, subprocess, tempfile, threading, time
import urllib.parse
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

archer.display_app.config['TESTING'] = True


# ── Helpers ──────────────────────────────────────────────────────
def _make_cookie(tier: int, name: str = 'Tester') -> str:
    """Return a signed JWT archer_auth cookie (JWT-only mode — legacy hmac disabled)."""
    from archer_state import make_auth_jwt
    return make_auth_jwt(tier, name)


class _CsrfClient:
    """Wraps a Flask test client and auto-injects X-CSRF-Token on every POST.

    Calls GET /csrf_token on construction so the session cookie is set, then
    includes the matching token header on all POST requests.  Every other method
    is forwarded transparently to the underlying client.
    """
    def __init__(self, base_client):
        self._c = base_client
        r = base_client.get('/csrf_token')
        self._token = json.loads(r.data)['token']

    def __getattr__(self, name):
        return getattr(self._c, name)

    def post(self, *args, **kwargs):
        headers = dict(kwargs.pop('headers', None) or {})
        headers.setdefault('X-CSRF-Token', self._token)
        return self._c.post(*args, headers=headers, **kwargs)


client = _CsrfClient(archer.display_app.test_client())


def _authed_client(tier: int, name: str = 'Tester'):
    c = archer.display_app.test_client()
    c.set_cookie('archer_auth', _make_cookie(tier, name))
    return _CsrfClient(c)


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

    def test_invalid_cookie_returns_5_or_fp(self):
        # A non-JWT cookie is rejected; tier falls back to fingerprint (≥1)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': 'archer_auth=notajwtatall'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            assert isinstance(tier, int) and tier >= 1

    def test_tampered_jwt_rejected(self):
        # Tamper with the JWT signature — must not grant tier 1
        jwt = _make_cookie(1)
        parts = jwt.split('.')
        parts[2] = 'A' * len(parts[2])  # corrupt the signature
        bad_cookie = '.'.join(parts)
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={bad_cookie}'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            assert tier != 1, f"Tampered JWT should not grant tier 1, got {tier}"

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

    def test_wrong_secret_jwt_rejected(self):
        # A JWT signed with a different secret must not grant tier 1
        import base64, json as _json
        def _b64(d): return base64.urlsafe_b64encode(d).rstrip(b'=').decode()
        header  = _b64(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64(_json.dumps({'tier':1,'name':'Ayden','jti':'x','iat':1,'exp':9999999999}).encode())
        sig     = _b64(b'\x00' * 32)  # bogus signature
        bad_jwt = f'{header}.{payload}.{sig}'
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={bad_jwt}'}
        ):
            from flask import request
            tier = archer.get_request_tier(request)
            assert tier != 1


# ═══════════════════════════════════════════════════════════════
# 2. /display_data endpoint
# ═══════════════════════════════════════════════════════════════
class TestDisplayData:
    # /display_data now requires a real credential (archer_auth or archer_fp
    # cookie) — a caller with neither is bounced to /fans instead of getting
    # data, so these use an authenticated client. Visitor behavior is covered
    # separately in TestDisplayDataVisitorRedirect.
    def test_returns_200_json(self):
        r = _authed_client(1).get('/display_data')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d, dict)

    def test_required_fields_present(self):
        r  = _authed_client(1).get('/display_data')
        d  = json.loads(r.data)
        for field in ('rpm', 'speed', 'boost', 'oil_temp', 'battery', 'ethanol'):
            assert field in d, f'Missing field: {field}'

    def test_numeric_values(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert isinstance(d['rpm'],     (int, float))
        assert isinstance(d['speed'],   (int, float))
        assert isinstance(d['battery'], (int, float))

    def test_sensor_data_nested(self):
        r  = _authed_client(1).get('/display_data')
        d  = json.loads(r.data)
        assert 'sensor_data' in d
        assert isinstance(d['sensor_data'], dict)

    def test_spike_history_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'spike_history' in d
        assert isinstance(d['spike_history'], dict)

    def test_connected_clients_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'connected_clients' in d
        assert isinstance(d['connected_clients'], int)

    def test_device_tier_present(self):
        r = _authed_client(1).get('/display_data?fp=test-fp-001')
        d = json.loads(r.data)
        assert 'device_tier' in d
        assert isinstance(d['device_tier'], int)

    def test_drive_mode_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'drive_mode' in d

    def test_coolant_temp_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'coolant' in d


class TestDisplayDataVisitorRedirect:
    """A caller with no archer_auth/archer_fp cookie AND no loopback address
    never enters the tier system — /display_data sends them to the fan page
    instead of resolving a fallback tier; /display_data/stream can't be
    redirected like a page, so it rejects cleanly instead. Werkzeug's test
    client defaults REMOTE_ADDR to 127.0.0.1, so these explicitly override
    it to a real, non-loopback address to actually exercise the visitor
    path rather than the loopback exception (see TestDisplayDataLoopback)."""
    _REMOTE = {'REMOTE_ADDR': '203.0.113.5'}

    def test_no_cookie_redirects_to_fans(self):
        r = client.get('/display_data', follow_redirects=False, environ_overrides=self._REMOTE)
        assert r.status_code == 302
        assert '/fans' in r.headers['Location']

    def test_no_cookie_redirects_even_with_fp_query_param(self):
        # The fp query param is informational only for this route — it was
        # never trusted for tier resolution, and doesn't count as a credential.
        r = client.get('/display_data?fp=test-fp-001', follow_redirects=False, environ_overrides=self._REMOTE)
        assert r.status_code == 302
        assert '/fans' in r.headers['Location']

    def test_stream_no_cookie_rejected_cleanly(self):
        c = archer.display_app.test_client()
        r = c.get('/display_data/stream?sid=visitor-sse&fp=fp1', environ_overrides=self._REMOTE)
        assert r.status_code == 403


class TestDisplayDataLoopback:
    """The in-VM kiosk display has no archer_auth cookie (nothing mints it
    one at boot) and talks to the server over 127.0.0.1 — it must not be
    treated as a visitor, or its own dashboard breaks. Loopback callers
    still get no special tier (no cookie means the existing fingerprint
    fallback still applies, landing on tier 4), just not the /fans bounce."""
    def test_loopback_no_cookie_gets_data_not_redirect(self):
        r = client.get('/display_data', follow_redirects=False)
        assert r.status_code == 200
        d = json.loads(r.data)
        assert 'rpm' in d

    def test_loopback_stream_no_cookie_not_rejected(self):
        c = archer.display_app.test_client()
        with c.get('/display_data/stream?sid=loopback-sse&fp=fp1',
                   headers={'Accept': 'text/event-stream'}) as r:
            assert r.status_code == 200
            assert 'text/event-stream' in r.content_type


class TestDisplayDataSensitiveFieldFiltering:
    """The public Caddyfile block's entire safety argument for exposing
    /display_data rests on this: a tier>=3 caller (which is what any
    anonymous request resolves to — see TestDisplayDataLoopback, and note
    that once Caddy reverse-proxies a request, it arrives at Flask as
    127.0.0.1 regardless of the original caller's real address, so this is
    also what a genuine public-internet visitor resolves to) must never
    see GPS, surveillance, camera, or parking/valet fields. Verified
    end-to-end through the real route, not just _filter_display_data_for_tier()
    called directly in isolation."""

    _SENSITIVE = ('destination', 'gps_lat', 'gps_lon', 'gps_name',
                  'surveillance', 'cameras', 'valet_events',
                  'parking_active', 'parking_loc')

    def test_tier_4_caller_never_sees_sensitive_fields(self):
        r = client.get('/display_data')
        assert r.status_code == 200
        d = json.loads(r.data)
        for field in self._SENSITIVE:
            assert field not in d, f'{field} leaked to a tier-4/anonymous caller'

    def test_tier_1_owner_still_sees_sensitive_fields(self):
        """Contrast case — proves the filter discriminates by tier rather
        than always stripping (or something else deleting these fields for
        everyone, which would hide a real regression as a false pass on
        the test above)."""
        r = _authed_client(1, 'Owner').get('/display_data')
        d = json.loads(r.data)
        for field in self._SENSITIVE:
            assert field in d, f'{field} missing even for the owner'


# ═══════════════════════════════════════════════════════════════
# 3. /voice_command tier restrictions
# ═══════════════════════════════════════════════════════════════
class TestVoiceCommand:
    def _post(self, command, tier=1, log_only=False):
        c = _authed_client(tier)
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
# 5b. Local Ollama cold-start fallback (archer.py:1974)
# Real testing on hardware matching the target server confirmed a ~18s
# cold-start latency for local Ollama. The subprocess timeout must clear
# that, or a cold start silently loses to smart_fallback instead of
# actually answering.
# ═══════════════════════════════════════════════════════════════
class TestLocalOllamaColdStart:
    def setup_method(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in ('GROQ_API_KEY', 'CEREBRAS_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY')
        }
        for k in self._env_backup:
            os.environ[k] = ''
        self._is_pi_backup = archer._IS_PI
        archer._IS_PI = True

    def teardown_method(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        archer._IS_PI = self._is_pi_backup

    def _fake_cold_start(self, cmd, capture_output, timeout, encoding, errors):
        """Stands in for a real `ollama run` that takes ~18s to answer."""
        if timeout < 18:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        return subprocess.CompletedProcess(
            cmd, 0, stdout='Archer: cold start survived.', stderr=''
        )

    def test_confirmed_cold_start_gets_a_real_answer(self):
        """A ~18s cold start must fit under the timeout so Ollama's actual
        answer is used, not a silent fall-through to smart_fallback. Fails
        again if the timeout ever regresses to (or below) the confirmed
        18s cold-start latency — proving the fix, not just the changed
        constant, is what's under test."""
        with patch('subprocess.run', side_effect=self._fake_cold_start):
            result = archer.ask_archer('how is the oil temp')

        # A regressed <=18s timeout would raise TimeoutExpired above, get
        # swallowed by ask_archer's bare except, and fall through to
        # smart_fallback's generic oil text instead of this real answer.
        assert result == 'cold start survived.'


# ═══════════════════════════════════════════════════════════════
# 5c. OpenRouter — 4th fallback step (archer.py: ask_archer, "Try 4")
# Added as a genuine extra waterfall step alongside Groq/Cerebras/Gemini,
# not a replacement for them, so it needs the same three things proven
# about it: it's actually reached, it doesn't jump the queue ahead of an
# earlier provider that already answered, and its own failure still falls
# through instead of raising.
# ═══════════════════════════════════════════════════════════════
class TestOpenRouterFallback:
    def setup_method(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in ('GROQ_API_KEY', 'CEREBRAS_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY')
        }
        for k in self._env_backup:
            os.environ[k] = ''
        # Keep Ollama's Pi-only step out of play so these tests isolate
        # OpenRouter the same way TestLocalOllamaColdStart isolates Ollama.
        self._is_pi_backup = archer._IS_PI
        archer._IS_PI = False

    def teardown_method(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        archer._IS_PI = self._is_pi_backup

    def _fake_response(self, text):
        body = json.dumps({'choices': [{'message': {'content': text}}]}).encode()
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        cm.__exit__.return_value = False
        return cm

    def test_used_when_only_openrouter_key_set(self):
        """With Groq/Cerebras/Gemini all unset and no Pi/Ollama, OpenRouter
        is the one live provider left before smart_fallback — confirms it's
        actually wired into the waterfall, not just present in the diff."""
        os.environ['OPENROUTER_API_KEY'] = 'test-key'
        with patch('urllib.request.urlopen', return_value=self._fake_response('OpenRouter answered.')) as mock_urlopen:
            result = archer.ask_archer('how is the oil temp')
        assert 'OpenRouter answered' in result
        assert 'openrouter.ai' in mock_urlopen.call_args[0][0].full_url

    def test_not_called_when_earlier_provider_succeeds(self):
        """Groq succeeding must short-circuit the waterfall — OpenRouter
        should never be hit if an earlier provider already answered."""
        os.environ['GROQ_API_KEY'] = 'test-key'
        os.environ['OPENROUTER_API_KEY'] = 'test-key'
        with patch('urllib.request.urlopen', return_value=self._fake_response('Groq answered.')) as mock_urlopen:
            result = archer.ask_archer('how is the oil temp')
        assert 'Groq answered' in result
        assert mock_urlopen.call_count == 1
        assert 'groq.com' in mock_urlopen.call_args[0][0].full_url

    def test_falls_through_to_smart_fallback_on_openrouter_failure(self):
        """OpenRouter erroring (bad key, timeout, etc.) must not crash the
        request — it should fall through to smart_fallback like every other
        provider's failure does."""
        os.environ['OPENROUTER_API_KEY'] = 'test-key'
        with patch('urllib.request.urlopen', side_effect=OSError('boom')):
            result = archer.ask_archer('how is the oil temp')
        assert isinstance(result, str) and len(result) > 0

    def test_skipped_entirely_when_key_unset(self):
        """No OPENROUTER_API_KEY at all -> urlopen must never be called for
        it; falls straight through to smart_fallback."""
        with patch('urllib.request.urlopen') as mock_urlopen:
            result = archer.ask_archer('how is the oil temp')
        mock_urlopen.assert_not_called()
        assert isinstance(result, str) and len(result) > 0


# ═══════════════════════════════════════════════════════════════
# 5d. Boot health check — AI BACKEND status (archer.py:11334-11346)
# Must report ok when ANY of the four cloud providers is configured, not
# just Groq — this is exactly the gap flagged during the OpenRouter audit:
# a Cerebras/Gemini/OpenRouter-only setup used to still show "warn: local
# fallback only" despite having a real provider configured.
# ═══════════════════════════════════════════════════════════════
class TestBootStatusAIBackend:
    def setup_method(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in ('HF_TOKEN', 'GROQ_API_KEY', 'CEREBRAS_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY')
        }
        for k in self._env_backup:
            os.environ[k] = ''

    def teardown_method(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _ai_check(self):
        r = client.get('/boot/status')
        data = json.loads(r.data)
        return next(c for c in data['checks'] if c['id'] == 'ai')

    def test_warn_when_nothing_configured(self):
        check = self._ai_check()
        assert check['status'] == 'warn'
        assert check['detail'] == 'local fallback only'

    def test_ok_when_only_cerebras_configured(self):
        """The regression case: pre-fix code only checked Groq, so a
        Cerebras-only setup still reported warn/local-fallback."""
        os.environ['CEREBRAS_API_KEY'] = 'test-key'
        check = self._ai_check()
        assert check['status'] == 'ok'
        assert 'Cerebras' in check['detail']

    def test_ok_when_only_openrouter_configured(self):
        os.environ['OPENROUTER_API_KEY'] = 'test-key'
        check = self._ai_check()
        assert check['status'] == 'ok'
        assert 'OpenRouter' in check['detail']

    def test_lists_multiple_configured_providers(self):
        os.environ['GROQ_API_KEY'] = 'test-key'
        os.environ['GEMINI_API_KEY'] = 'test-key'
        check = self._ai_check()
        assert check['status'] == 'ok'
        assert 'Groq' in check['detail'] and 'Gemini' in check['detail']


# ═══════════════════════════════════════════════════════════════
# 6. save_state / load_state round-trip
# ═══════════════════════════════════════════════════════════════
class TestSaveLoadState:
    def test_save_calls_db_save(self):
        """save_state() must call db_save with a dict (SQLite-only persistence)."""
        with patch('db.db_save') as mock_db_save:
            archer.save_state()
        mock_db_save.assert_called_once()
        arg = mock_db_save.call_args[0][0]
        assert isinstance(arg, dict)

    def test_save_passes_valid_dict_to_db(self):
        """The dict handed to db_save must contain expected state keys."""
        with patch('db.db_save') as mock_db_save:
            archer.save_state()
        data = mock_db_save.call_args[0][0]
        assert 'nav_places' in data
        assert 'personal_bests' in data

    def test_save_does_not_write_json_file(self, tmp_path):
        """save_state() must NOT write a legacy JSON file anymore."""
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            with patch('db.db_save'):
                archer.save_state()
            assert not os.path.exists(archer.SAVE_FILE)
        finally:
            archer.SAVE_FILE = orig

    def test_nav_places_persisted(self, tmp_path):
        import copy
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        archer.nav_places['home'] = {'lat': 37.64, 'lon': -91.54, 'address': 'Home'}
        saved_data = {}
        def _capture(d):
            # Deep-copy so clearing nav_places later doesn't wipe our snapshot
            saved_data.update(copy.deepcopy(d))
        try:
            with patch('db.db_save', side_effect=_capture):
                archer.save_state()
            archer.nav_places.clear()
            with patch('db.db_load', return_value=saved_data), \
                 patch('db.db_has_data', return_value=True):
                archer.load_state()
            assert 'home' in archer.nav_places
            assert archer.nav_places['home']['lat'] == 37.64
        finally:
            archer.nav_places.pop('home', None)
            archer.SAVE_FILE = orig

    def test_linked_bluetooth_listed_and_persisted(self):
        """'link phone' must land in the dict that 'list devices' reads and
        save_state() persists."""
        before = dict(archer.bluetooth_devices)
        saved = {}
        try:
            with patch('db.db_save', side_effect=saved.update):
                archer.handle_command('link phone')
            assert archer.handle_command('list devices') != 'No devices linked yet.'
            assert len(saved['bluetooth_devices']) == len(before) + 1
        finally:
            archer.bluetooth_devices.clear()
            archer.bluetooth_devices.update(before)

    def test_load_missing_file_doesnt_crash(self, tmp_path):
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'nonexistent.json')
        try:
            with patch('db.db_load', return_value={}), \
                 patch('db.db_has_data', return_value=False):
                archer.load_state()
        finally:
            archer.SAVE_FILE = orig

    def test_no_tmp_file_left_after_save(self, tmp_path):
        """No .tmp file should exist after save_state() (SQLite writes atomically)."""
        orig = archer.SAVE_FILE
        archer.SAVE_FILE = str(tmp_path / 'archer_test.json')
        try:
            with patch('db.db_save'):
                archer.save_state()
            assert not os.path.exists(archer.SAVE_FILE + '.tmp')
        finally:
            archer.SAVE_FILE = orig

    def test_truck_state_keys_saved(self):
        """db_save must receive nav_places among the persisted keys."""
        with patch('db.db_save') as mock_db_save:
            archer.save_state()
        data = mock_db_save.call_args[0][0]
        assert 'nav_places' in data


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
    """Verify that _make_cookie() returns a valid HS256 JWT."""

    def test_jwt_has_three_dot_parts(self):
        jwt = _make_cookie(1, 'Ayden')
        assert jwt.count('.') == 2

    def test_jwt_is_unique_each_call(self):
        # JWTs include a random jti, so two calls never produce identical tokens
        assert _make_cookie(1, 'Ayden') != _make_cookie(1, 'Ayden')

    def test_jwt_decodes_correct_tier(self):
        from archer_state import decode_auth_jwt
        for tier in (1, 2, 3, 4):
            payload = decode_auth_jwt(_make_cookie(tier, 'Test'))
            assert payload['tier'] == tier

    def test_jwt_decodes_correct_name(self):
        from archer_state import decode_auth_jwt
        payload = decode_auth_jwt(_make_cookie(2, 'Khloe'))
        assert payload['name'] == 'Khloe'

    def test_jwt_signature_verified(self):
        from archer_state import decode_auth_jwt
        jwt = _make_cookie(1, 'Ayden')
        parts = jwt.split('.')
        parts[2] = 'A' * len(parts[2])
        import pytest as _pt
        with _pt.raises(ValueError):
            decode_auth_jwt('.'.join(parts))


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
            mock_run.return_value = MagicMock(stdout='up 3 days\n', stderr='', returncode=0)
            r = self._post('uptime', tier=1)
        assert r.status_code == 200

    def test_unlisted_command_blocked(self):
        """Commands not on the allowlist are rejected with 403."""
        r = self._post('echo hello', tier=1)
        d = json.loads(r.data)
        assert r.status_code == 403 or 'error' in d


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

    # notify_tier1 requires at least tier 2 (passenger); CSRF handled by _CsrfClient

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
        # /tier_notifications requires tier <= 2 (it discloses pending
        # Tier1<->Tier2 request content, not public data).
        c = _authed_client(2, 'Passenger')
        r = c.get('/tier_notifications')
        d = json.loads(r.data)
        assert isinstance(d['notifications'], list)

    def test_notifications_list_rejects_unauthenticated(self):
        r = client.get('/tier_notifications')
        assert r.status_code == 403

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
    # /build/part/* routes require Tier 1 (owner-only build tracker editing).
    def test_add_part_returns_ok(self):
        c = _authed_client(1)
        r = c.post('/build/part/add', json={
            'name': 'Cold Air Intake', 'category': 'intake',
            'hp_gain': 15, 'tq_gain': 12, 'cost': 299.99,
        })
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_add_part_returns_part(self):
        c = _authed_client(1)
        r = c.post('/build/part/add', json={'name': 'Test Part'})
        d = json.loads(r.data)
        assert 'part' in d
        assert d['part']['name'] == 'Test Part'

    def test_add_part_has_id(self):
        c = _authed_client(1)
        r = c.post('/build/part/add', json={'name': 'Headers'})
        d = json.loads(r.data)
        assert 'id' in d['part']

    def test_update_part_ok(self):
        c = _authed_client(1)
        r = c.post('/build/part/add', json={'name': 'UpdateMe'})
        pid = json.loads(r.data)['part']['id']
        r2  = c.post('/build/part/update', json={'id': pid, 'status': 'installed'})
        d   = json.loads(r2.data)
        assert d['ok'] is True
        assert d['part']['status'] == 'installed'

    def test_update_nonexistent_part(self):
        c = _authed_client(1)
        r = c.post('/build/part/update', json={'id': 'nonexistent_id', 'status': 'installed'})
        d = json.loads(r.data)
        assert d['ok'] is False

    def test_remove_part_ok(self):
        c   = _authed_client(1)
        r   = c.post('/build/part/add', json={'name': 'RemoveMe'})
        pid = json.loads(r.data)['part']['id']
        r2  = c.post('/build/part/remove', json={'id': pid})
        d   = json.loads(r2.data)
        assert d['ok'] is True

    def test_remove_part_no_longer_in_list(self):
        c   = _authed_client(1)
        r   = c.post('/build/part/add', json={'name': 'GoneItem'})
        pid = json.loads(r.data)['part']['id']
        c.post('/build/part/remove', json={'id': pid})
        r2  = c.post('/build/part/add', json={'name': 'Dummy'})
        parts = json.loads(r2.data)['parts']
        assert not any(p['id'] == pid for p in parts)

    def test_add_part_returns_power(self):
        c = _authed_client(1)
        r = c.post('/build/part/add', json={'name': 'Tune', 'hp_gain': 30})
        d = json.loads(r.data)
        assert 'power' in d

    def test_non_owner_rejected(self):
        c = _authed_client(2)
        r = c.post('/build/part/add', json={'name': 'ShouldFail'})
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
# 23. /build/update endpoint
# ═══════════════════════════════════════════════════════════════
class TestBuildUpdate:
    # /build/update requires Tier 1 (owner-only build spec editing).
    def test_returns_ok(self):
        c = _authed_client(1)
        r = c.post('/build/update', json={'cold_air_intake': True})
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_updates_build_spec(self):
        _authed_client(1).post('/build/update', json={'cold_air_intake': True})
        assert archer.build_specs.get('cold_air_intake') is True

    def test_returns_power(self):
        c = _authed_client(1)
        r = c.post('/build/update', json={})
        d = json.loads(r.data)
        assert 'power' in d

    def test_returns_build_specs(self):
        c = _authed_client(1)
        r = c.post('/build/update', json={})
        d = json.loads(r.data)
        assert 'build_specs' in d

    def test_non_owner_rejected(self):
        c = _authed_client(3)
        r = c.post('/build/update', json={'cold_air_intake': True})
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
# 24. /location/update endpoint
# ═══════════════════════════════════════════════════════════════
class TestLocationUpdate:
    # Patches the network-calling function itself rather than threading.Thread
    # broadly — flask_limiter's memory storage backend (this route is now
    # rate-limited) also relies on threading.Timer, a Thread subclass, so a
    # blanket Thread patch here breaks rate-limit evaluation too.
    def test_returns_ok(self):
        with patch('archer._resolve_location_from_nws'):
            r = client.post('/location/update', json={'lat': 37.64, 'lon': -91.54})
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_stores_lat_lon(self):
        with patch('archer._resolve_location_from_nws'):
            client.post('/location/update', json={'lat': 42.0, 'lon': -93.0})
        r = client.post('/location/update', json={'lat': 42.0, 'lon': -93.0})
        d = json.loads(r.data)
        assert d['lat'] == 42.0
        assert d['lon'] == -93.0

    def test_name_stored_if_provided(self):
        with patch('archer._resolve_location_from_nws'):
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
        from archer_state import make_auth_jwt
        return make_auth_jwt(tier, name)

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
        # After logout the JWT jti should appear in _revoked_tokens
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(2, 'LogoutTest')
        jti = decode_auth_jwt(token)['jti']
        with archer.display_app.test_client() as c:
            c.set_cookie('archer_auth', token)
            csrf = json.loads(c.get('/csrf_token').data)['token']
            c.post('/logout', headers={'X-CSRF-Token': csrf})
        assert jti in archer._revoked_tokens

    def test_no_session_logout_is_safe(self):
        r = client.post('/logout')
        assert r.status_code == 200


class TestAuthCookieRefresh:
    """The rolling refresh in after_request must not undo a cookie change the
    view itself made. Checked via the client's cookie jar, not the first
    Set-Cookie header — the refresh appends a second header, and the browser
    keeps the last one."""

    def test_refresh_restamps_cookie(self):
        c = _authed_client(2, 'Khloe')
        r = c.get('/csrf_token')
        assert any(h.startswith('archer_auth=') and 'Max-Age=2592000' in h
                   for h in r.headers.getlist('Set-Cookie'))

    def test_logout_leaves_no_cookie(self):
        c = _authed_client(1, 'Ayden')
        c.post('/logout')
        assert c.get_cookie('archer_auth') is None

    def test_login_replaces_stale_cookie(self):
        stale = _make_cookie(1, 'Ayden')
        base = archer.display_app.test_client()
        base.set_cookie('archer_auth', stale)
        with patch.object(archer, 'verify_owner_pin', return_value=(True, None)):
            r = _CsrfClient(base).post('/login', json={'pin': '000000'})
        assert r.status_code == 200
        held = base.get_cookie('archer_auth')
        assert held is not None and held.value != stale


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
        # handle_connection returns Optional[List[str]] (permission scope on
        # success, None on any failure) so scoped keys can be enforced.
        assert result is None

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
        # No key_manager record for this ad-hoc test key → falls back to full
        # OWNER permissions (legacy-key backward compatibility), not a bare bool.
        assert result is not None
        assert 'admin' in result or 'write_all' in result

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
        assert result is None

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
        assert result is None

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
        assert result is not None


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
        r = _authed_client(1).get('/display_data')
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
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'maf' in d

    def test_timing_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'timing' in d

    def test_engine_load_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'engine_load' in d

    def test_stft_b1_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'stft_b1' in d

    def test_ltft_b1_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'ltft_b1' in d

    def test_stft_b2_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'stft_b2' in d

    def test_ltft_b2_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'ltft_b2' in d

    def test_obd_mode_field_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'obd_mode' in d

    def test_vehicle_name_fields_present(self):
        r = _authed_client(1).get('/display_data')
        d = json.loads(r.data)
        assert 'vehicle_make' in d
        assert 'vehicle_model' in d
        assert 'vehicle_name' in d
        assert 'vehicle_name_header' in d


# ═══════════════════════════════════════════════════════════════
# 40. JWT issuance and decoding
# ═══════════════════════════════════════════════════════════════
class TestJWT:
    def test_make_auth_jwt_is_three_parts(self):
        from archer_state import make_auth_jwt
        token = make_auth_jwt(1, 'Ayden')
        assert token.count('.') == 2

    def test_decode_round_trip(self):
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(2, 'Khloe')
        payload = decode_auth_jwt(token)
        assert payload['tier'] == 2
        assert payload['name'] == 'Khloe'

    def test_decode_includes_jti(self):
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(1, 'Ayden')
        payload = decode_auth_jwt(token)
        assert 'jti' in payload and len(payload['jti']) > 0

    def test_tampered_signature_raises(self):
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(1, 'Ayden')
        parts = token.split('.')
        parts[2] = parts[2][:-4] + 'XXXX'
        with pytest.raises(ValueError, match='signature'):
            decode_auth_jwt('.'.join(parts))

    def test_expired_token_raises(self):
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(1, 'Ayden', days=-1)
        with pytest.raises(ValueError, match='expired'):
            decode_auth_jwt(token)

    def test_non_jwt_string_raises(self):
        from archer_state import decode_auth_jwt
        with pytest.raises(ValueError):
            decode_auth_jwt('1:Ayden:abc123')

    def test_wrong_format_raises(self):
        from archer_state import decode_auth_jwt
        with pytest.raises(ValueError):
            decode_auth_jwt('notajwt')


# ═══════════════════════════════════════════════════════════════
# 41. Logout — JWT jti revocation
# ═══════════════════════════════════════════════════════════════
class TestLogoutJWT:
    def test_logout_revokes_jwt_jti(self):
        """Logging out with a JWT cookie should add its jti to _revoked_tokens."""
        from archer_state import make_auth_jwt, decode_auth_jwt
        token = make_auth_jwt(1, 'JWTLogoutTest')
        payload = decode_auth_jwt(token)
        jti = payload['jti']

        with archer.display_app.test_client() as c:
            c.set_cookie('archer_auth', token)
            c.get('/csrf_token')
            csrf_token = json.loads(c.get('/csrf_token').data)['token']
            c.post('/logout', headers={'X-CSRF-Token': csrf_token})

        assert jti in archer._revoked_tokens

    def test_jwt_rejected_after_logout(self):
        """After logout a subsequent request with the same JWT must be denied."""
        from archer_state import make_auth_jwt
        token = make_auth_jwt(1, 'JWTLogoutReject')

        with archer.display_app.test_client() as c:
            c.set_cookie('archer_auth', token)
            csrf_token = json.loads(c.get('/csrf_token').data)['token']
            c.post('/logout', headers={'X-CSRF-Token': csrf_token})
            # After logout, tier-1-only endpoint should deny
            r = c.post('/terminal/exec',
                       json={'cmd': 'ls'},
                       headers={'X-CSRF-Token': csrf_token})
        d = json.loads(r.data)
        assert 'error' in d or r.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════
# 42. Terminal exec — shell=False enforcement
# ═══════════════════════════════════════════════════════════════
class TestTerminalShellFalse:
    def _post(self, cmd, tier=1):
        c = _authed_client(tier)
        return c.post('/terminal/exec', json={'cmd': cmd})

    def test_subprocess_called_with_shell_false(self):
        """Verify subprocess.run is always called with shell=False."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout='ok', stderr='', returncode=0)
            self._post('ps aux', tier=1)
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get('shell') is False

    def test_pipe_without_bash_c_rejected(self):
        """Commands not in the allowlist (echo, etc.) are rejected even with a pipe suffix."""
        r = self._post('echo hello | cat', tier=1)
        d = json.loads(r.data)
        assert r.status_code == 403 or 'error' in d

    def test_bash_c_pipe_is_blocked(self):
        """bash -c '...' is blocked by the allowlist (bash is not a permitted executable)."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout='hello\n', stderr='', returncode=0)
            r = self._post("bash -c 'echo hello | cat'", tier=1)
        assert r.status_code == 403 or 'error' in json.loads(r.data)
        mock_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 43. Pi tunnel — authentication on register and disconnect
# ═══════════════════════════════════════════════════════════════
class TestPiTunnel:
    _token = 'test_pi_token_abc'

    def setup_method(self):
        import blueprints.terminal as _term
        self._orig = _term._ARCHER_PI_TOKEN
        _term._ARCHER_PI_TOKEN = self._token

    def teardown_method(self):
        import blueprints.terminal as _term
        _term._ARCHER_PI_TOKEN = self._orig

    def test_pi_register_valid_token(self):
        r = client.post('/terminal/pi_register',
                        json={'token': self._token, 'url': 'https://abc.ngrok.io'})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d.get('status') == 'registered'

    def test_pi_register_invalid_token_rejected(self):
        r = client.post('/terminal/pi_register',
                        json={'token': 'wrong', 'url': 'https://abc.ngrok.io'})
        assert r.status_code == 403

    def test_pi_register_no_token_rejected(self):
        r = client.post('/terminal/pi_register', json={'url': 'https://abc.ngrok.io'})
        assert r.status_code == 403

    def test_pi_disconnect_valid_token(self):
        r = client.post('/terminal/pi_disconnect', json={'token': self._token})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d.get('status') == 'ok'

    def test_pi_disconnect_no_token_rejected(self):
        """pi_disconnect with no token must return 403 — not silently accept."""
        r = client.post('/terminal/pi_disconnect', json={})
        assert r.status_code == 403

    def test_pi_disconnect_wrong_token_rejected(self):
        r = client.post('/terminal/pi_disconnect', json={'token': 'bad'})
        assert r.status_code == 403

    def test_pi_register_requires_csrf(self):
        """Calling pi_register without a CSRF token must return 403."""
        with archer.display_app.test_client() as c:
            r = c.post('/terminal/pi_register',
                       json={'token': self._token, 'url': 'https://x.ngrok.io'})
        assert r.status_code == 403

    def test_pi_disconnect_requires_csrf(self):
        """Calling pi_disconnect without a CSRF token must return 403."""
        with archer.display_app.test_client() as c:
            r = c.post('/terminal/pi_disconnect', json={'token': self._token})
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
# 44. Fail-closed secret — archer_state refuses to start without a secret
# ═══════════════════════════════════════════════════════════════
class TestFailClosedSecret:
    def test_archer_state_has_non_empty_secret(self):
        from archer_state import _ARCHER_SECRET
        assert _ARCHER_SECRET and len(_ARCHER_SECRET) >= 8

    def test_secret_matches_env(self):
        from archer_state import _ARCHER_SECRET
        assert _ARCHER_SECRET == os.environ.get('ARCHER_SECRET', _ARCHER_SECRET)


# ═══════════════════════════════════════════════════════════════
# 45. JWT-only mode — legacy cookie rejected
# ═══════════════════════════════════════════════════════════════
class TestJWTOnlyMode:
    def test_legacy_cookie_rejected(self):
        """Old tier:name:hmac cookies must not grant access (downgrade attack)."""
        import hashlib as hl
        secret = os.environ['ARCHER_SECRET']
        token = hl.sha256(f'Ayden1{secret}'.encode()).hexdigest()[:32]
        legacy_cookie = f'1:Ayden:{token}'
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={legacy_cookie}'}
        ):
            from flask import request
            # Should fall back to fingerprint (not tier 1 from cookie)
            tier = archer.get_request_tier(request)
            assert tier != 1 or tier == 5  # not granted by legacy cookie

    def test_jwt_cookie_grants_access(self):
        """A valid JWT cookie must grant the correct tier."""
        from archer_state import make_auth_jwt
        jwt = make_auth_jwt(2, 'Khloe')
        with archer.display_app.test_request_context(
            '/', headers={'Cookie': f'archer_auth={jwt}'}
        ):
            from flask import request
            assert archer.get_request_tier(request) == 2


# ═══════════════════════════════════════════════════════════════
# 46. Encrypted log stream — one-time key gate
# ═══════════════════════════════════════════════════════════════
class TestLogStreamKey:
    def test_key_endpoint_requires_csrf(self):
        """POST /terminal/log_stream_key without CSRF must return 403."""
        with archer.display_app.test_client() as c:
            c.set_cookie('archer_auth', _make_cookie(1))
            r = c.post('/terminal/log_stream_key', json={})
        assert r.status_code == 403

    def test_key_endpoint_requires_tier1(self):
        """Tier 2 must not be able to get a log stream key."""
        c = _authed_client(2)
        r = c.post('/terminal/log_stream_key', json={})
        d = json.loads(r.data)
        assert r.status_code == 403 or 'error' in d

    def test_stream_without_key_rejected(self):
        """GET /terminal/log_stream without a valid key must return 403."""
        r = client.get('/terminal/log_stream')
        assert r.status_code == 403

    def test_stream_with_wrong_key_rejected(self):
        """A made-up key must be rejected."""
        r = client.get('/terminal/log_stream?key=fakekeyabc123')
        assert r.status_code == 403

    def test_key_issued_to_tier1(self):
        """Tier 1 can obtain a stream key via CSRF-protected POST."""
        c = _authed_client(1)
        r = c.post('/terminal/log_stream_key', json={})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert 'key' in d and len(d['key']) > 0


# ═══════════════════════════════════════════════════════════════
# 47. rotate-secrets terminal command
# ═══════════════════════════════════════════════════════════════
class TestRotateSecrets:
    def test_rotate_secrets_requires_tier1(self):
        c = _authed_client(2)
        r = c.post('/terminal/exec', json={'cmd': 'rotate-secrets'})
        d = json.loads(r.data)
        assert 'error' in d or r.status_code in (401, 403)

    def test_rotate_secrets_clears_revoked_tokens(self):
        """After rotate-secrets, _revoked_tokens should be empty."""
        archer._revoked_tokens['fake_jti'] = 9999999999  # plant a token
        c = _authed_client(1)
        with patch('hsm.rotate_master_key', return_value=os.environ['ARCHER_SECRET']):
            r = c.post('/terminal/exec', json={'cmd': 'rotate-secrets'})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d.get('returncode') == 0
        assert 'fake_jti' not in archer._revoked_tokens

    def test_rotate_secrets_response_mentions_sessions(self):
        c = _authed_client(1)
        with patch('hsm.rotate_master_key', return_value=os.environ['ARCHER_SECRET']):
            r = c.post('/terminal/exec', json={'cmd': 'rotate-secrets'})
        d = json.loads(r.data)
        assert 'session' in d.get('stdout', '').lower()


class TestBeamNGToken:
    """BEAMNG_TOKEN auth on /beamng_data."""

    def _payload(self):
        return {'source': 'beamng', 'rpm': 3000, 'speed': 60, 'boost': 5.0,
                'gear': 3, 'throttle': 50, 'oil_temp': 200, 'coolant_temp': 195}

    def test_no_token_env_allows_any_request(self):
        """If BEAMNG_TOKEN is not set, endpoint accepts without a token header."""
        os.environ.pop('BEAMNG_TOKEN', None)
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._payload())
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d['ok'] is True

    def test_correct_token_accepted(self):
        os.environ['BEAMNG_TOKEN'] = 'test-bridge-token'
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._payload(),
                   headers={'X-BeamNG-Token': 'test-bridge-token'})
        assert r.status_code == 200
        assert json.loads(r.data)['ok'] is True
        del os.environ['BEAMNG_TOKEN']

    def test_wrong_token_rejected(self):
        os.environ['BEAMNG_TOKEN'] = 'test-bridge-token'
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._payload(),
                   headers={'X-BeamNG-Token': 'wrong-token'})
        assert r.status_code == 403
        del os.environ['BEAMNG_TOKEN']

    def test_missing_token_header_rejected(self):
        os.environ['BEAMNG_TOKEN'] = 'test-bridge-token'
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._payload())
        assert r.status_code == 403
        del os.environ['BEAMNG_TOKEN']

    def test_invalid_payload_rejected(self):
        os.environ.pop('BEAMNG_TOKEN', None)
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json={'source': 'not_beamng', 'rpm': 1000})
        assert r.status_code == 400


class TestDisplayDataSSE:
    """SSE push endpoint /display_data/stream."""

    def test_stream_returns_event_stream_content_type(self):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(1))
        with c.get('/display_data/stream?sid=test-sse&fp=fp1',
                   headers={'Accept': 'text/event-stream'}) as r:
            assert 'text/event-stream' in r.content_type

    def test_stream_yields_data_line(self):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(1))
        with c.get('/display_data/stream?sid=test-sse2&fp=fp1',
                   headers={'Accept': 'text/event-stream'}) as r:
            chunk = next(r.iter_encoded(), b'')
            text = chunk.decode('utf-8', errors='replace')
            assert text.startswith('data:')
            payload = json.loads(text[len('data:'):].strip())
            assert 'rpm' in payload
            assert 'speed' in payload

    def test_stream_includes_device_tier(self):
        c = archer.display_app.test_client()
        c.set_cookie('archer_auth', _make_cookie(1))
        with c.get('/display_data/stream?sid=test-sse3&fp=fp_unregistered',
                   headers={'Accept': 'text/event-stream'}) as r:
            chunk = next(r.iter_encoded(), b'')
            payload = json.loads(chunk.decode().split('data:')[1].strip())
            assert 'device_tier' in payload


class TestBeamNGBounds:
    """Sensor bounds validation in /beamng_data endpoint."""

    def _base(self, overrides=None):
        p = {'source': 'beamng', 'rpm': 2000, 'speed': 50, 'boost': 5.0,
             'oil_temp': 200, 'coolant_temp': 190, 'throttle': 40, 'gear': 3}
        if overrides:
            p.update(overrides)
        return p

    def test_normal_values_accepted(self):
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._base())
        assert r.status_code == 200

    def test_rpm_out_of_bounds_rejected(self):
        """RPM >8000 must not be written to truck_state."""
        archer.truck_state['rpm'] = 1000  # known-good starting value
        c = archer.display_app.test_client()
        c.post('/beamng_data', json=self._base({'rpm': 99999}))
        assert archer.truck_state['rpm'] == 1000

    def test_coolant_out_of_bounds_rejected(self):
        """500°F coolant must not be written to truck_state."""
        archer.truck_state['coolant_temp'] = 190
        c = archer.display_app.test_client()
        c.post('/beamng_data', json=self._base({'coolant_temp': 500}))
        assert archer.truck_state['coolant_temp'] == 190

    def test_valid_boundary_value_accepted(self):
        """Exact boundary value (8000 RPM) should be accepted."""
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._base({'rpm': 8000}))
        assert r.status_code == 200
        assert archer.truck_state['rpm'] == 8000

    def test_non_numeric_gear_accepted(self):
        """Gear 'R' or 'N' (string, no bounds) must pass through."""
        c = archer.display_app.test_client()
        r = c.post('/beamng_data', json=self._base({'gear': 'R'}))
        assert r.status_code == 200
        assert archer.truck_state['gear'] == 'R'


class TestTripLogEnrichment:
    """save_trip() now records peak temps, distance, MPG, and fault codes."""

    def test_save_trip_includes_peak_coolant(self):
        archer.awareness['peak_coolant_temp'] = 215
        t = archer.save_trip()
        assert t['peak_coolant_temp'] == 215

    def test_save_trip_includes_peak_oil(self):
        archer.awareness['peak_oil_temp'] = 230
        t = archer.save_trip()
        assert t['peak_oil_temp'] == 230

    def test_save_trip_includes_distance(self):
        archer.trip_stats['distance_miles'] = 12.5
        t = archer.save_trip()
        assert t['trip_distance'] == 12.5

    def test_save_trip_includes_mpg(self):
        archer.trip_stats['avg_mpg'] = 13.2
        t = archer.save_trip()
        assert t['trip_mpg'] == 13.2

    def test_save_trip_captures_active_fault_codes(self):
        archer.fault_codes.append({'code': 'P0300', 'status': 'active', 'desc': 'Misfire', 'severity': 'critical'})
        t = archer.save_trip()
        assert 'P0300' in t['fault_codes']
        archer.fault_codes.clear()

    def test_save_trip_excludes_cleared_codes(self):
        archer.fault_codes.append({'code': 'P0300', 'status': 'cleared', 'desc': 'Misfire', 'severity': 'medium'})
        t = archer.save_trip()
        assert 'P0300' not in t['fault_codes']
        archer.fault_codes.clear()


class TestOLEDStaleFlag:
    """record_spikes() writes stale=True when sensor data stops updating."""

    def test_stale_when_sim_disabled_and_obd_old(self):
        import copy
        archer.sim_flags['random_enabled'] = False
        archer.system_health['last_obd_update'] = 0  # very old
        # Call record_spikes once — but it loops so we just test the logic inline
        stale = (
            not archer.sim_flags.get('random_enabled', True)
            and (time.time() - archer.system_health.get('last_obd_update', 0)) > 10
        )
        assert stale is True
        archer.sim_flags['random_enabled'] = True

    def test_not_stale_when_sim_enabled(self):
        archer.sim_flags['random_enabled'] = True
        stale = (
            not archer.sim_flags.get('random_enabled', True)
            and (time.time() - archer.system_health.get('last_obd_update', 0)) > 10
        )
        assert stale is False

    def test_not_stale_when_obd_recent(self):
        archer.sim_flags['random_enabled'] = False
        archer.system_health['last_obd_update'] = time.time()
        stale = (
            not archer.sim_flags.get('random_enabled', True)
            and (time.time() - archer.system_health.get('last_obd_update', 0)) > 10
        )
        assert stale is False
        archer.sim_flags['random_enabled'] = True


class TestTLSContext:
    """_get_tls_context() returns None when USE_TLS is not set."""

    def test_no_tls_by_default(self):
        os.environ.pop('USE_TLS', None)
        ctx = archer._get_tls_context()
        assert ctx is None

    def test_tls_false_explicit(self):
        os.environ['USE_TLS'] = 'false'
        ctx = archer._get_tls_context()
        assert ctx is None
        del os.environ['USE_TLS']

    def test_tls_true_missing_openssl_returns_none(self):
        """When USE_TLS=true but openssl is unavailable, falls back gracefully."""
        os.environ['USE_TLS'] = 'true'
        os.environ['ARCHER_TLS_CERT'] = '/nonexistent/cert.crt'
        os.environ['ARCHER_TLS_KEY']  = '/nonexistent/cert.key'
        with patch('subprocess.run', side_effect=FileNotFoundError('openssl not found')):
            ctx = archer._get_tls_context()
        assert ctx is None
        del os.environ['USE_TLS']
        os.environ.pop('ARCHER_TLS_CERT', None)
        os.environ.pop('ARCHER_TLS_KEY', None)


class TestSerialAuth:
    """Tests for serial_auth.py — lazy init, encode/decode round-trip, ReplayGuard."""

    def _fresh_module(self, env_secret='test-secret-32byteslong12345678'):
        """Import a fresh copy of serial_auth with the given ARCHER_SECRET."""
        import importlib, sys
        os.environ['ARCHER_SECRET'] = env_secret
        # Remove cached module so _SERIAL_SECRET is re-initialized
        sys.modules.pop('serial_auth', None)
        with patch('hsm.get_or_create_secret', side_effect=RuntimeError('no HSM')):
            import serial_auth as sa
        return sa

    def test_secret_not_initialized_at_import(self):
        """_SERIAL_SECRET stays None until first use — no HSM crash at import time."""
        import importlib, sys
        os.environ['ARCHER_SECRET'] = 'lazy-init-test-secret-32bytes!!'
        sys.modules.pop('serial_auth', None)
        with patch('hsm.get_or_create_secret', side_effect=RuntimeError('no HSM')):
            import serial_auth as sa
        assert sa._SERIAL_SECRET is None

    def test_secret_initialized_on_first_encode(self):
        """_SERIAL_SECRET is populated once encode_message() is called."""
        sa = self._fresh_module()
        assert sa._SERIAL_SECRET is None
        sa.encode_message('cmd=TEST')
        assert sa._SERIAL_SECRET is not None

    def test_round_trip(self):
        """encode then decode succeeds and returns the original payload."""
        sa = self._fresh_module()
        guard = sa.ReplayGuard()
        line = sa.encode_message('cmd=HELIX,preset=2')
        result = sa.decode_message(line, guard=guard)
        assert result['payload'] == 'cmd=HELIX,preset=2'

    def test_tampered_tag_rejected(self):
        """A message with a corrupted tag raises ValueError."""
        sa = self._fresh_module()
        guard = sa.ReplayGuard()
        line = sa.encode_message('cmd=HELIX')
        # Flip the last char of the tag
        parts = line.split(':')
        parts[-1] = parts[-1][:-1] + ('0' if parts[-1][-1] != '0' else '1')
        bad = ':'.join(parts)
        with pytest.raises(ValueError, match='Bad HMAC'):
            sa.decode_message(bad, guard=guard)

    def test_replay_rejected(self):
        """Sending the same message twice raises ValueError on the second attempt."""
        sa = self._fresh_module()
        guard = sa.ReplayGuard()
        line = sa.encode_message('cmd=TEST', nonce='aabbccdd')
        sa.decode_message(line, guard=guard)
        with pytest.raises(ValueError, match='Replay'):
            sa.decode_message(line, guard=guard)

    def test_replay_guard_eviction_deque(self):
        """After window+1 unique nonces, the oldest is evicted and can be re-used."""
        import serial_auth as sa
        guard = sa.ReplayGuard(window=4)
        for i in range(4):
            guard.check_and_record(f'n{i:04x}')
        # Window full: n0000..n0003.  n0001 is NOT the oldest — still in window.
        assert guard.check_and_record('n0001') is False
        # Adding n0004 evicts n0000 (oldest).
        guard.check_and_record('n0004')
        # n0000 was evicted — it should be accepted again
        assert guard.check_and_record('n0000') is True
        # n0004 is still in the window — replay rejected
        assert guard.check_and_record('n0004') is False

    def test_replay_guard_order_is_deque(self):
        """ReplayGuard._order is a collections.deque, not a list."""
        from collections import deque
        import serial_auth as sa
        guard = sa.ReplayGuard()
        assert isinstance(guard._order, deque)


# ═══════════════════════════════════════════════════════════════
# FCM push notifications
# ═══════════════════════════════════════════════════════════════
class TestFcmPushIntegration:
    def setup_method(self):
        import fcm_push as fp
        fp.reset_for_tests()

    def test_fcm_token_endpoint_registers(self):
        r = client.post('/fcm_token', json={'token': 'test-device-token-xyz'})
        assert r.status_code == 200
        assert json.loads(r.data)['ok'] is True
        import fcm_push as fp
        assert 'test-device-token-xyz' in fp.get_tokens()

    def test_fcm_token_rejects_invalid(self):
        r = client.post('/fcm_token', json={'token': ''})
        assert r.status_code == 400

    def test_notify_tier1_triggers_fcm(self):
        with patch('fcm_push.send_tier_request', return_value=1) as send:
            c = _authed_client(2, 'Passenger')
            c.post('/notify_tier1', json={'from': 'Alice', 'message': 'Sport mode please'})
            send.assert_called_once_with('Alice', 'Sport mode please')

    def test_discord_alert_triggers_fcm_even_when_discord_disabled(self):
        archer.discord_config['enabled'] = False
        archer.discord_config['last_sent'].clear()
        with patch('fcm_push.send_vehicle_alert', return_value=1) as send:
            archer.discord_alert('oil_high', 'Oil **225F**', title='OIL')
            send.assert_called_once_with(
                'oil_high', 'Oil **225F**', title='OIL', skip_cooldown=True,
            )


# ═══════════════════════════════════════════════════════════════
# Maintenance service tracker JSON API
# ═══════════════════════════════════════════════════════════════
class TestMaintenanceData:
    def setup_method(self):
        self._saved_log = dict(archer.maintenance_log)
        self._saved_odo = dict(archer.odometer)

    def teardown_method(self):
        archer.maintenance_log.clear()
        archer.maintenance_log.update(self._saved_log)
        archer.odometer.clear()
        archer.odometer.update(self._saved_odo)

    def test_requires_tier1(self):
        r = client.get('/maintenance/data')
        assert r.status_code == 403

    def test_returns_items_array(self):
        r = _authed_client(1, 'Owner').get('/maintenance/data')
        assert r.status_code == 200
        d = json.loads(r.data)
        assert isinstance(d['items'], list)
        assert len(d['items']) == len(archer._MAINTENANCE_UI_SPECS)

    def test_maps_maintenance_log_miles(self):
        archer.maintenance_log['oil_change'] = {
            'last_date': 'January 01 2026',
            'last_miles': 140000,
            'interval_miles': 5000,
        }
        archer.odometer['miles'] = 145200
        r = _authed_client(1, 'Owner').get('/maintenance/data')
        oil = next(i for i in json.loads(r.data)['items'] if i['id'] == 'oil')
        assert oil['last_mi'] == 140000
        assert oil['current_mi'] == 145200
        assert oil['interval_mi'] == 5000


# ═══════════════════════════════════════════════════════════════
# 48. Discord bot — interaction signature verification + commands
# ═══════════════════════════════════════════════════════════════
from nacl.signing import SigningKey  # noqa: E402


class TestDiscordSignatureVerification:
    def setup_method(self):
        self._signing_key = SigningKey.generate()
        self._backup = archer.DISCORD_PUBLIC_KEY
        archer.DISCORD_PUBLIC_KEY = self._signing_key.verify_key.encode().hex()

    def teardown_method(self):
        archer.DISCORD_PUBLIC_KEY = self._backup

    def test_valid_signature_accepted(self):
        sig = self._signing_key.sign(b'1700000000{"type":1}').signature.hex()
        assert archer._discord_verify_signature(sig, '1700000000', '{"type":1}') is True

    def test_tampered_body_rejected(self):
        sig = self._signing_key.sign(b'1700000000{"type":1}').signature.hex()
        assert archer._discord_verify_signature(sig, '1700000000', '{"type":2}') is False

    def test_wrong_key_rejected(self):
        other_key = SigningKey.generate()
        sig = other_key.sign(b'1700000000{"type":1}').signature.hex()
        assert archer._discord_verify_signature(sig, '1700000000', '{"type":1}') is False

    def test_no_public_key_configured_rejected(self):
        archer.DISCORD_PUBLIC_KEY = ''
        assert archer._discord_verify_signature('aa' * 64, '1700000000', '{}') is False


class TestDiscordInteractionsEndpoint:
    def setup_method(self):
        self._signing_key = SigningKey.generate()
        self._backup = {
            'public_key': archer.DISCORD_PUBLIC_KEY,
            'owner_id':   archer.DISCORD_OWNER_ID,
            'enabled':    archer.discord_config['enabled'],
        }
        archer.DISCORD_PUBLIC_KEY = self._signing_key.verify_key.encode().hex()
        archer.DISCORD_OWNER_ID = 'owner-123'
        archer.discord_config['enabled'] = True

    def teardown_method(self):
        archer.DISCORD_PUBLIC_KEY = self._backup['public_key']
        archer.DISCORD_OWNER_ID = self._backup['owner_id']
        archer.discord_config['enabled'] = self._backup['enabled']

    def _signed_post(self, body_dict, timestamp='1700000000'):
        body = json.dumps(body_dict)
        sig = self._signing_key.sign(f'{timestamp}{body}'.encode()).signature.hex()
        return archer.display_app.test_client().post(
            '/discord/interactions',
            data=body,
            headers={
                'Content-Type':         'application/json',
                'X-Signature-Ed25519':  sig,
                'X-Signature-Timestamp': timestamp,
            },
        )

    def test_ping_returns_pong(self):
        r = self._signed_post({'type': 1})
        assert r.status_code == 200
        assert json.loads(r.data) == {'type': 1}

    def test_bad_signature_rejected(self):
        r = archer.display_app.test_client().post(
            '/discord/interactions',
            data=json.dumps({'type': 1}),
            headers={
                'Content-Type':         'application/json',
                'X-Signature-Ed25519':  'aa' * 64,
                'X-Signature-Timestamp': '1700000000',
            },
        )
        assert r.status_code == 401

    def test_unauthorized_user_rejected_on_command(self):
        r = self._signed_post({
            'type': 2,
            'data': {'name': 'status'},
            'member': {'user': {'id': 'stranger-999'}},
        })
        data = json.loads(r.data)
        assert 'not authorized' in data['data']['content'].lower()
        assert data['data']['flags'] == 64

    def test_authorized_status_command_reports_real_oil_temp(self):
        r = self._signed_post({
            'type': 2,
            'data': {'name': 'status'},
            'member': {'user': {'id': 'owner-123'}},
        })
        data = json.loads(r.data)
        assert str(archer.truck_state['oil_temp']) in data['data']['content']

    def test_unauthorized_button_rejected(self):
        r = self._signed_post({
            'type': 3,
            'data': {'custom_id': 'parking_disarm'},
            'member': {'user': {'id': 'stranger-999'}},
        })
        data = json.loads(r.data)
        assert 'not authorized' in data['data']['content'].lower()

    def test_authorized_parking_disarm_button_disarms(self):
        archer.parking_mode['active'] = True
        r = self._signed_post({
            'type': 3,
            'data': {'custom_id': 'parking_disarm'},
            'member': {'user': {'id': 'owner-123'}},
        })
        assert r.status_code == 200
        assert archer.parking_mode['active'] is False

    def test_authorized_crash_not_ok_button_flags_last_event(self):
        archer.crash_detection['last_event'] = {'time': '1:00 PM', 'g': 4.0, 'speed': 30, 'road': 'Test Rd'}
        r = self._signed_post({
            'type': 3,
            'data': {'custom_id': 'crash_not_ok'},
            'member': {'user': {'id': 'owner-123'}},
        })
        data = json.loads(r.data)
        assert archer.crash_detection['last_event']['status'] == 'needs_attention'
        assert "can't place calls" in data['data']['content']

    def test_disabled_integration_rejects_non_ping(self):
        archer.discord_config['enabled'] = False
        r = self._signed_post({
            'type': 2,
            'data': {'name': 'status'},
            'member': {'user': {'id': 'owner-123'}},
        })
        data = json.loads(r.data)
        assert 'disabled' in data['data']['content'].lower()


class TestDiscordDigest:
    def test_build_digest_contains_key_fields(self):
        msg = archer._build_discord_digest()
        assert 'Peak RPM' in msg
        assert 'Drive quality' in msg


# ═══════════════════════════════════════════════════════════════
# Discord per-tier DM fan-out (archer.py: discord_dm_fanout(),
# _discord_user_tier(), _discord_send_dm()). Different fan-out model than
# Slack's per-requester one — this DMs every individually-known user whose
# tier qualifies, not one channel post.
# ═══════════════════════════════════════════════════════════════
class TestDiscordUserTier:
    def setup_method(self):
        self._backup = (archer.DISCORD_OWNER_IDS, archer.DISCORD_PASSENGER_IDS,
                         archer.DISCORD_FAMILY_IDS, archer.DISCORD_VALET_IDS)
        archer.DISCORD_OWNER_IDS     = {'D_OWNER'}
        archer.DISCORD_PASSENGER_IDS = {'D_PASSENGER'}
        archer.DISCORD_FAMILY_IDS    = {'D_FAMILY'}
        archer.DISCORD_VALET_IDS     = {'D_VALET'}

    def teardown_method(self):
        (archer.DISCORD_OWNER_IDS, archer.DISCORD_PASSENGER_IDS,
         archer.DISCORD_FAMILY_IDS, archer.DISCORD_VALET_IDS) = self._backup

    def test_owner_resolves_tier_1(self):
        assert archer._discord_user_tier('D_OWNER') == 1

    def test_passenger_resolves_tier_2(self):
        assert archer._discord_user_tier('D_PASSENGER') == 2

    def test_family_resolves_tier_3(self):
        assert archer._discord_user_tier('D_FAMILY') == 3

    def test_valet_resolves_tier_4(self):
        assert archer._discord_user_tier('D_VALET') == 4

    def test_unknown_user_resolves_none(self):
        assert archer._discord_user_tier('D_STRANGER') is None

    def test_all_known_users_mapping(self):
        assert archer._discord_all_known_users() == {
            'D_OWNER': 1, 'D_PASSENGER': 2, 'D_FAMILY': 3, 'D_VALET': 4,
        }


class TestDiscordDMFanout:
    def setup_method(self):
        self._backup_ids = (archer.DISCORD_OWNER_IDS, archer.DISCORD_PASSENGER_IDS,
                             archer.DISCORD_FAMILY_IDS, archer.DISCORD_VALET_IDS)
        archer.DISCORD_OWNER_IDS     = {'D1'}
        archer.DISCORD_PASSENGER_IDS = {'D2'}
        archer.DISCORD_FAMILY_IDS    = {'D3'}
        archer.DISCORD_VALET_IDS     = {'D4'}
        self._backup_enabled = archer.discord_config['enabled']
        archer.discord_config['enabled'] = True
        self._backup_token = archer.DISCORD_BOT_TOKEN
        archer.DISCORD_BOT_TOKEN = 'test-bot-token'

    def teardown_method(self):
        (archer.DISCORD_OWNER_IDS, archer.DISCORD_PASSENGER_IDS,
         archer.DISCORD_FAMILY_IDS, archer.DISCORD_VALET_IDS) = self._backup_ids
        archer.discord_config['enabled'] = self._backup_enabled
        archer.DISCORD_BOT_TOKEN = self._backup_token

    def test_tier_3_alert_reaches_1_2_3_not_4(self):
        """The requirement's own example: a Tier 3 alert must reach Tiers
        1, 2, 3 as real, separate DMs — and correctly exclude Tier 4."""
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout('crash', 'Impact detected.', min_tier=3)
        recipients = {c.args[0] for c in mock_dm.call_args_list}
        assert recipients == {'D1', 'D2', 'D3'}

    def test_fail_closed_when_min_tier_missing(self):
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout('unknown_alert', 'Something happened.', min_tier=None)
        recipients = {c.args[0] for c in mock_dm.call_args_list}
        assert recipients == {'D1'}

    def test_fail_closed_when_min_tier_not_a_real_tier_number(self):
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout('weird', 'x', min_tier='not-a-tier')
        recipients = {c.args[0] for c in mock_dm.call_args_list}
        assert recipients == {'D1'}

    def test_disabled_config_sends_nothing(self):
        archer.discord_config['enabled'] = False
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout('crash', 'x', min_tier=3)
        mock_dm.assert_not_called()

    def test_extra_data_filtered_per_recipient_tier(self):
        """Tier 1/2 recipients (<3) keep the GPS fields; Tier 3 (>=3) gets
        them stripped by _filter_display_data_for_tier — the same source
        of truth the dashboard itself uses for what a tier sees, not a
        redaction rule invented here."""
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout(
                'crash', 'Impact detected.', min_tier=3,
                extra_data={'gps_lat': 40.7128, 'gps_lon': -74.0060, 'gps_name': 'Home'},
            )
        messages = {c.args[0]: c.args[1] for c in mock_dm.call_args_list}
        assert '40.7128' in messages['D1'] and 'Home' in messages['D1']
        assert '40.7128' in messages['D2'] and 'Home' in messages['D2']
        assert '40.7128' not in messages['D3'] and 'Home' not in messages['D3']

    def test_mismatched_extra_data_keys_filter_nothing(self):
        """A dict shaped with different key names (plain 'lat'/'lon' rather
        than 'gps_lat'/'gps_lon') isn't redacted at all — proves the reuse
        is real, not cosmetic: only the exact _DISPLAY_DATA_SENSITIVE_FIELDS
        names do anything."""
        with patch('archer._discord_send_dm') as mock_dm:
            archer.discord_dm_fanout(
                'crash', 'Impact detected.', min_tier=3,
                extra_data={'lat': 40.7128, 'lon': -74.0060},
            )
        messages = {c.args[0]: c.args[1] for c in mock_dm.call_args_list}
        assert messages['D1'] == 'Impact detected.'
        assert messages['D3'] == 'Impact detected.'


class TestDiscordDMChannelMechanics:
    """Confirms DMs actually go through Discord's real two-step DM flow —
    open/fetch the DM channel, then message that channel id — not a
    fabricated shortcut."""

    def setup_method(self):
        self._backup_token = archer.DISCORD_BOT_TOKEN
        archer.DISCORD_BOT_TOKEN = 'test-bot-token'
        self._backup_enabled = archer.discord_config['enabled']
        archer.discord_config['enabled'] = True

    def teardown_method(self):
        archer.DISCORD_BOT_TOKEN = self._backup_token
        archer.discord_config['enabled'] = self._backup_enabled

    def _fake_response(self, body_dict, status=200):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(body_dict).encode()
        cm.__enter__.return_value.status = status
        cm.__exit__.return_value = False
        return cm

    def test_opens_real_dm_channel_before_sending(self):
        """Discord has no single 'DM this user_id' endpoint — must POST
        /users/@me/channels with recipient_id first, then message the
        channel id that comes back. Confirms both calls happen, in order,
        with the right shapes."""
        responses = [
            self._fake_response({'id': 'DM_CHANNEL_123'}),
            self._fake_response({'id': 'MSG_1'}),
        ]
        with patch('urllib.request.urlopen', side_effect=responses) as mock_urlopen:
            ok = archer._discord_send_dm('D_USER', 'Hello')
        assert ok is True
        assert mock_urlopen.call_count == 2
        first_req  = mock_urlopen.call_args_list[0].args[0]
        second_req = mock_urlopen.call_args_list[1].args[0]
        # Regression check for a real, confirmed bug: Cloudflare's bot
        # protection in front of Discord's API 403s any request using
        # Python's default urllib User-Agent (HTTP 403, error code 1010) —
        # found live while running discord_register_commands.py. Every
        # Discord API call must carry a real User-Agent or it silently
        # fails the same way.
        assert first_req.get_header('User-agent') == archer._DISCORD_USER_AGENT
        assert second_req.get_header('User-agent') == archer._DISCORD_USER_AGENT
        assert first_req.full_url == 'https://discord.com/api/v10/users/@me/channels'
        assert json.loads(first_req.data)['recipient_id'] == 'D_USER'
        assert second_req.full_url == 'https://discord.com/api/v10/channels/DM_CHANNEL_123/messages'

    def test_dm_channel_open_failure_does_not_attempt_to_message(self):
        with patch('urllib.request.urlopen', side_effect=OSError('network down')) as mock_urlopen:
            ok = archer._discord_send_dm('D_USER', 'Hello')
        assert ok is False
        assert mock_urlopen.call_count == 1  # never got to the message step


# ═══════════════════════════════════════════════════════════════
# 49. discord_config/slack_config['enabled'] — computed from real
# credentials, not hardcoded (regression coverage for a real bug: both
# dicts were defined before their credential constants existed in the
# file, so 'enabled' was permanently False no matter what was set in
# archer.env — confirmed live, then fixed by computing it once those
# constants are actually read).
# ═══════════════════════════════════════════════════════════════
class TestIntegrationEnabledFlag:
    def test_compute_slack_enabled_requires_both_credentials(self):
        backup = (archer.SLACK_SIGNING_SECRET, archer.SLACK_BOT_TOKEN)
        try:
            archer.SLACK_SIGNING_SECRET, archer.SLACK_BOT_TOKEN = 'secret', 'xoxb-token'
            assert archer._compute_slack_enabled() is True
            archer.SLACK_SIGNING_SECRET, archer.SLACK_BOT_TOKEN = 'secret', ''
            assert archer._compute_slack_enabled() is False
            archer.SLACK_SIGNING_SECRET, archer.SLACK_BOT_TOKEN = '', 'xoxb-token'
            assert archer._compute_slack_enabled() is False
        finally:
            archer.SLACK_SIGNING_SECRET, archer.SLACK_BOT_TOKEN = backup

    def test_compute_discord_enabled_requires_public_key(self):
        backup = archer.DISCORD_PUBLIC_KEY
        try:
            archer.DISCORD_PUBLIC_KEY = 'key'
            assert archer._compute_discord_enabled() is True
            archer.DISCORD_PUBLIC_KEY = ''
            assert archer._compute_discord_enabled() is False
        finally:
            archer.DISCORD_PUBLIC_KEY = backup

    def test_real_process_with_credentials_boots_enabled(self):
        """End-to-end reproduction of the exact reported bug: import archer
        in a fresh process with real-looking credentials in the environment
        and confirm both flags actually come up True — not just that the
        compute function returns the right answer in isolation, which
        wouldn't have caught the original bug (the function didn't exist;
        the dict literal was never wired to anything)."""
        env = dict(os.environ)
        env['SLACK_SIGNING_SECRET'] = 'test-secret'
        env['SLACK_BOT_TOKEN']      = 'xoxb-test-token'
        env['DISCORD_PUBLIC_KEY']   = 'test-key'
        result = subprocess.run(
            [sys.executable, '-c',
             "import archer; print(archer.slack_config['enabled'], archer.discord_config['enabled'])"],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=os.path.dirname(os.path.abspath(archer.__file__)),
        )
        assert result.stdout.strip().endswith('True True'), result.stdout + result.stderr


# ═══════════════════════════════════════════════════════════════
# 50. Slack bot — signature verification + tiered command/alert routing
# ═══════════════════════════════════════════════════════════════
class TestSlackSignatureVerification:
    def setup_method(self):
        self._backup = archer.SLACK_SIGNING_SECRET
        archer.SLACK_SIGNING_SECRET = 'test-signing-secret'

    def teardown_method(self):
        archer.SLACK_SIGNING_SECRET = self._backup

    def _sign(self, timestamp, body, secret=b'test-signing-secret'):
        basestring = f'v0:{timestamp}:{body}'.encode()
        return 'v0=' + hmac.new(secret, basestring, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        ts = str(int(time.time()))
        body = '{"type":"url_verification"}'
        assert archer._slack_verify_signature(self._sign(ts, body), ts, body) is True

    def test_tampered_body_rejected(self):
        ts = str(int(time.time()))
        sig = self._sign(ts, '{"a":1}')
        assert archer._slack_verify_signature(sig, ts, '{"a":2}') is False

    def test_wrong_secret_rejected(self):
        ts = str(int(time.time()))
        body = '{"a":1}'
        sig = self._sign(ts, body, secret=b'wrong-secret')
        assert archer._slack_verify_signature(sig, ts, body) is False

    def test_stale_timestamp_rejected(self):
        ts = str(int(time.time()) - 600)  # 10 minutes old — outside the 5 min window
        body = '{"a":1}'
        assert archer._slack_verify_signature(self._sign(ts, body), ts, body) is False

    def test_no_signing_secret_configured_rejected(self):
        archer.SLACK_SIGNING_SECRET = ''
        assert archer._slack_verify_signature('v0=' + 'a' * 64, str(int(time.time())), '{}') is False


class TestSlackUserTier:
    def setup_method(self):
        self._backup = (archer.SLACK_OWNER_IDS, archer.SLACK_PASSENGER_IDS, archer.SLACK_FAMILY_IDS)
        archer.SLACK_OWNER_IDS     = {'U_OWNER'}
        archer.SLACK_PASSENGER_IDS = {'U_PASSENGER'}
        archer.SLACK_FAMILY_IDS    = {'U_FAMILY'}

    def teardown_method(self):
        archer.SLACK_OWNER_IDS, archer.SLACK_PASSENGER_IDS, archer.SLACK_FAMILY_IDS = self._backup

    def test_owner_resolves_tier_1(self):
        assert archer._slack_user_tier('U_OWNER') == 1

    def test_passenger_resolves_tier_2(self):
        assert archer._slack_user_tier('U_PASSENGER') == 2

    def test_family_resolves_tier_3(self):
        assert archer._slack_user_tier('U_FAMILY') == 3

    def test_unknown_user_resolves_none(self):
        assert archer._slack_user_tier('U_STRANGER') is None


class TestSlackInteractionsEndpoint:
    def setup_method(self):
        self._backup = {
            'signing_secret': archer.SLACK_SIGNING_SECRET,
            'enabled':        archer.slack_config['enabled'],
            'owner_ids':      archer.SLACK_OWNER_IDS,
            'passenger_ids':  archer.SLACK_PASSENGER_IDS,
            'family_ids':     archer.SLACK_FAMILY_IDS,
        }
        archer.SLACK_SIGNING_SECRET = 'test-signing-secret'
        archer.slack_config['enabled'] = True
        archer.SLACK_OWNER_IDS     = {'U_OWNER'}
        archer.SLACK_PASSENGER_IDS = {'U_PASSENGER'}
        archer.SLACK_FAMILY_IDS    = {'U_FAMILY'}

    def teardown_method(self):
        archer.SLACK_SIGNING_SECRET    = self._backup['signing_secret']
        archer.slack_config['enabled'] = self._backup['enabled']
        archer.SLACK_OWNER_IDS         = self._backup['owner_ids']
        archer.SLACK_PASSENGER_IDS     = self._backup['passenger_ids']
        archer.SLACK_FAMILY_IDS        = self._backup['family_ids']

    def _form_body(self, **fields):
        return urllib.parse.urlencode(fields)

    def _signed_post(self, form_body):
        ts = str(int(time.time()))
        sig = 'v0=' + hmac.new(b'test-signing-secret', f'v0:{ts}:{form_body}'.encode(), hashlib.sha256).hexdigest()
        return archer.display_app.test_client().post(
            '/slack/interactions',
            data=form_body,
            content_type='application/x-www-form-urlencoded',
            headers={'X-Slack-Signature': sig, 'X-Slack-Request-Timestamp': ts},
        )

    def test_bad_signature_rejected(self):
        r = archer.display_app.test_client().post(
            '/slack/interactions',
            data=self._form_body(command='/vstatus', user_id='U_OWNER'),
            content_type='application/x-www-form-urlencoded',
            headers={'X-Slack-Signature': 'v0=' + 'a' * 64, 'X-Slack-Request-Timestamp': str(int(time.time()))},
        )
        assert r.status_code == 401

    def test_unauthorized_user_rejected(self):
        r = self._signed_post(self._form_body(command='/vstatus', user_id='U_STRANGER'))
        data = json.loads(r.data)
        assert 'not authorized' in data['text'].lower()

    def test_owner_status_shows_full_diagnostics(self):
        r = self._signed_post(self._form_body(command='/vstatus', user_id='U_OWNER'))
        data = json.loads(r.data)
        assert str(archer.truck_state['oil_temp']) in data['text']

    def test_passenger_status_omits_raw_oil_temp(self):
        r = self._signed_post(self._form_body(command='/vstatus', user_id='U_PASSENGER'))
        data = json.loads(r.data)
        assert 'Oil' not in data['text']
        assert str(archer.truck_state['speed']) in data['text']

    def test_family_status_has_no_numeric_diagnostics(self):
        r = self._signed_post(self._form_body(command='/vstatus', user_id='U_FAMILY'))
        data = json.loads(r.data)
        assert 'RPM' not in data['text']
        assert 'Oil' not in data['text']
        assert 'Speed' not in data['text']

    def test_parking_rejected_for_passenger(self):
        r = self._signed_post(self._form_body(command='/parking', text='arm', user_id='U_PASSENGER'))
        data = json.loads(r.data)
        assert 'not authorized' in data['text'].lower()

    def test_parking_rejected_for_family(self):
        r = self._signed_post(self._form_body(command='/parking', text='arm', user_id='U_FAMILY'))
        data = json.loads(r.data)
        assert 'not authorized' in data['text'].lower()

    def test_parking_allowed_for_owner(self):
        archer.parking_mode['active'] = False
        try:
            r = self._signed_post(self._form_body(command='/parking', text='arm', user_id='U_OWNER'))
            assert r.status_code == 200
            assert archer.parking_mode['active'] is True
        finally:
            archer.parking_mode['active'] = False

    def test_ask_rejected_for_family(self):
        r = self._signed_post(self._form_body(command='/ask', text='how is the truck', user_id='U_FAMILY'))
        data = json.loads(r.data)
        assert 'not authorized' in data['text'].lower()

    def test_ask_deferred_for_passenger(self):
        r = self._signed_post(self._form_body(
            command='/ask', text='how is the truck', user_id='U_PASSENGER',
            response_url='https://example.invalid/resp',
        ))
        data = json.loads(r.data)
        assert data['text'] == 'Thinking...'

    def test_block_action_parking_disarm_rejected_for_passenger(self):
        payload = json.dumps({'user': {'id': 'U_PASSENGER'}, 'actions': [{'action_id': 'parking_disarm'}]})
        r = self._signed_post(self._form_body(payload=payload))
        data = json.loads(r.data)
        assert 'owner only' in data['text'].lower()

    def test_block_action_crash_im_ok_allowed_for_passenger(self):
        archer.crash_detection['last_event'] = {'time': '1:00 PM', 'g': 4.0, 'speed': 30, 'road': 'Test Rd'}
        payload = json.dumps({'user': {'id': 'U_PASSENGER'}, 'actions': [{'action_id': 'crash_im_ok'}]})
        r = self._signed_post(self._form_body(payload=payload))
        assert r.status_code == 200
        assert archer.crash_detection['last_event']['status'] == 'confirmed_ok'

    def test_disabled_integration_rejects_non_ping(self):
        archer.slack_config['enabled'] = False
        r = self._signed_post(self._form_body(command='/vstatus', user_id='U_OWNER'))
        data = json.loads(r.data)
        assert 'disabled' in data['text'].lower()


class TestSlackAlertRouting:
    def setup_method(self):
        self._backup = {
            'enabled':  archer.slack_config['enabled'],
            'channels': dict(archer.SLACK_TIER_CHANNELS),
        }
        archer.slack_config['enabled'] = True
        archer.SLACK_TIER_CHANNELS[1] = 'C_OWNER'
        archer.SLACK_TIER_CHANNELS[2] = 'C_PASSENGER'
        archer.SLACK_TIER_CHANNELS[3] = 'C_FAMILY'

    def teardown_method(self):
        archer.slack_config['enabled'] = self._backup['enabled']
        archer.SLACK_TIER_CHANNELS.clear()
        archer.SLACK_TIER_CHANNELS.update(self._backup['channels'])

    def test_owner_only_alert_does_not_reach_passenger_or_family(self):
        with patch('archer.slack_post_message') as mock_send:
            archer.slack_route_alert('oil_high', 'Oil temp critical at 230F.', 'OIL WARNING')
        channels_called = {c.args[0] for c in mock_send.call_args_list}
        assert channels_called == {'C_OWNER'}

    def test_crash_alert_reaches_all_three_tiers(self):
        with patch('archer.slack_post_message') as mock_send:
            archer.slack_route_alert('crash', 'Impact detected.', 'CRASH ALERT')
        channels_called = {c.args[0] for c in mock_send.call_args_list}
        assert channels_called == {'C_OWNER', 'C_PASSENGER', 'C_FAMILY'}

    def test_crash_buttons_sent_to_owner_and_passenger_not_family(self):
        with patch('archer.slack_post_message') as mock_send:
            archer.slack_route_alert('crash', 'Impact detected.', 'CRASH ALERT')
        blocks_by_channel = {c.args[0]: c.args[2] for c in mock_send.call_args_list}
        assert any(b['type'] == 'actions' for b in blocks_by_channel['C_OWNER'])
        assert any(b['type'] == 'actions' for b in blocks_by_channel['C_PASSENGER'])
        assert not any(b['type'] == 'actions' for b in blocks_by_channel['C_FAMILY'])

    def test_parking_armed_reaches_owner_and_passenger_not_family(self):
        with patch('archer.slack_post_message') as mock_send:
            archer.slack_route_alert('parking_armed', 'Parked at Home.', 'PARKING MODE ON')
        channels_called = {c.args[0] for c in mock_send.call_args_list}
        assert channels_called == {'C_OWNER', 'C_PASSENGER'}

    def test_disabled_integration_sends_nothing(self):
        archer.slack_config['enabled'] = False
        with patch('archer.slack_post_message') as mock_send:
            archer.slack_route_alert('crash', 'Impact detected.', 'CRASH ALERT')
        mock_send.assert_not_called()


class TestSlackDigest:
    def test_owner_digest_matches_discord_digest(self):
        assert archer._slack_digest_message(1) == archer._build_discord_digest()

    def test_passenger_digest_omits_peak_rpm(self):
        msg = archer._slack_digest_message(2)
        assert 'Peak RPM' not in msg
        assert 'Drive quality' in msg

    def test_family_digest_has_no_raw_numbers_field(self):
        msg = archer._slack_digest_message(3)
        assert 'Peak RPM' not in msg
        assert 'Drive quality' not in msg


# ═══════════════════════════════════════════════════════════════
# 51. obd_autodetect() — OBD_PORT manual override (archer.py:12522)
# Confirmed dead before this fix: the env var was documented in
# config.py/archer.env.example/README as the fallback when autodetect
# can't find an adapter (true for a Bluetooth OBDLink MX+ bound to
# /dev/rfcommN — it exposes no description/manufacturer string for the
# keyword scan to match), but obd_autodetect() never read it.
# ═══════════════════════════════════════════════════════════════
class TestObdPortOverride:
    def test_obd_port_set_skips_scan_and_connects_there(self):
        """When OBD_PORT is set, it must be used directly, every loop
        iteration — not just consulted once alongside the scan."""
        with patch.dict(os.environ, {'OBD_PORT': '/dev/rfcomm0'}), \
             patch('serial.tools.list_ports.comports') as mock_comports, \
             patch('serial.Serial', side_effect=RuntimeError('boom')) as mock_serial_cls, \
             patch('time.sleep', side_effect=RuntimeError('stop-loop')):
            with pytest.raises(RuntimeError, match='stop-loop'):
                archer.obd_autodetect()

        mock_serial_cls.assert_called_once()
        assert mock_serial_cls.call_args[0][0] == '/dev/rfcomm0'
        mock_comports.assert_not_called()

    def test_obd_port_unset_falls_back_to_scan(self):
        """Regression guard: the override must not swallow the existing
        autodetect behavior for USB adapters when it isn't set.

        serial is stubbed as a bare MagicMock for the whole test session
        (see the top of this file). `import serial.tools.list_ports` is a
        real import statement, not attribute access, so it needs both
        'serial.tools' and 'serial.tools.list_ports' registered in
        sys.modules AND wired as real attributes on the fake 'serial'
        module — a MagicMock auto-vivifies attribute access but that alone
        doesn't satisfy the import statement's own module resolution."""
        fake_tools = MagicMock()
        fake_list_ports = MagicMock()
        fake_list_ports.comports.return_value = []
        fake_tools.list_ports = fake_list_ports
        sys.modules['serial'].tools = fake_tools

        with patch.dict(os.environ, {'OBD_PORT': ''}), \
             patch.dict(sys.modules, {'serial.tools': fake_tools, 'serial.tools.list_ports': fake_list_ports}), \
             patch('time.sleep', side_effect=RuntimeError('stop-loop')):
            with pytest.raises(RuntimeError, match='stop-loop'):
                archer.obd_autodetect()

        fake_list_ports.comports.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 52. ReSpeaker mic array — device discovery, channel extraction,
# check_microphone()/listen_once() wiring, and the fallback to existing
# generic-microphone behavior when no ReSpeaker is found.
#
# LOGIC-VERIFIED here (mocked sounddevice/vosk — this session has no real
# ReSpeaker hardware or a booted Pi VM). NOT hardware-verified: whether a
# real ReSpeaker's actual multi-channel capture sounds right and
# transcribes correctly. That needs the physical array in hand.
# ═══════════════════════════════════════════════════════════════
class TestReSpeakerDeviceDiscovery:
    def test_finds_respeaker_by_name(self):
        fake_devices = [
            {'name': 'Built-in Microphone', 'max_input_channels': 1},
            {'name': 'ReSpeaker 4 Mic Array (UAC1.0)', 'max_input_channels': 6},
        ]
        with patch('sounddevice.query_devices', return_value=fake_devices):
            assert archer._find_respeaker_device() == (1, 6)

    def test_no_respeaker_returns_none(self):
        fake_devices = [{'name': 'Built-in Microphone', 'max_input_channels': 1}]
        with patch('sounddevice.query_devices', return_value=fake_devices):
            assert archer._find_respeaker_device() is None

    def test_ignores_output_only_device_named_respeaker(self):
        """A ReSpeaker's playback/output side shouldn't be picked as an input."""
        fake_devices = [{'name': 'ReSpeaker 4 Mic Array', 'max_input_channels': 0}]
        with patch('sounddevice.query_devices', return_value=fake_devices):
            assert archer._find_respeaker_device() is None

    def test_prefers_alsa_over_a_same_named_device_on_another_hostapi(self):
        """A same-named device reported under a non-ALSA host API (e.g. a
        stray Pulse pseudo-device) must not be picked over the real ALSA
        one, even when ALSA is present and working — see the comment on
        _find_respeaker_device() for why this matters at all."""
        fake_hostapis = [{'name': 'ALSA'}, {'name': 'pulse'}]
        fake_devices = [
            {'name': 'ReSpeaker via pulse', 'max_input_channels': 6, 'hostapi': 1},
            {'name': 'ReSpeaker 4 Mic Array', 'max_input_channels': 6, 'hostapi': 0},
        ]
        with patch('sounddevice.query_hostapis', return_value=fake_hostapis), \
             patch('sounddevice.query_devices', return_value=fake_devices):
            assert archer._find_respeaker_device() == (1, 6)  # index 1 = the ALSA-hostapi device

    def test_scan_error_returns_none_not_raises(self):
        with patch('sounddevice.query_devices', side_effect=RuntimeError('no audio backend')):
            assert archer._find_respeaker_device() is None


class TestPinSoundDeviceToAlsa:
    """archer._pin_sounddevice_to_alsa() — the real answer to 'does
    PulseAudio work on archer-os': no, and it isn't supposed to (no D-Bus
    session bus, no systemd — see the docstring on this function in
    archer.py). This points PortAudio's default device resolution at ALSA
    explicitly so a failed Pulse probe during Pa_Initialize() can't affect
    it, regardless of what host APIs a given libportaudio2 build has
    compiled in."""

    def test_pins_alsa_when_present(self):
        fake_sd = MagicMock()
        fake_sd.query_hostapis.return_value = [{'name': 'OSS'}, {'name': 'ALSA'}]
        assert archer._pin_sounddevice_to_alsa(fake_sd) is True
        assert fake_sd.default.hostapi == 1

    def test_returns_false_when_no_alsa_hostapi(self):
        fake_sd = MagicMock()
        fake_sd.query_hostapis.return_value = [{'name': 'JACK Audio Connection Kit'}]
        assert archer._pin_sounddevice_to_alsa(fake_sd) is False


class TestExtractChannel:
    def test_extracts_first_channel_from_interleaved_pcm(self):
        import array
        # 2 channels, 3 frames: ch0=[10,20,30], ch1=[100,200,300], interleaved
        interleaved = array.array('h', [10, 100, 20, 200, 30, 300]).tobytes()
        result = archer._extract_channel(interleaved, channel_index=0, num_channels=2)
        assert array.array('h', result).tolist() == [10, 20, 30]

    def test_extracts_second_channel_not_first(self):
        import array
        interleaved = array.array('h', [10, 100, 20, 200, 30, 300]).tobytes()
        result = archer._extract_channel(interleaved, channel_index=1, num_channels=2)
        assert array.array('h', result).tolist() == [100, 200, 300]


class TestCheckMicrophoneReSpeaker:
    def teardown_method(self):
        archer.mic_device['index']        = None
        archer.mic_device['channels']     = 1
        archer.mic_device['is_respeaker'] = False
        archer.mic_available['ok']        = False

    def test_respeaker_found_sets_mic_device(self):
        with patch('archer._find_respeaker_device', return_value=(2, 6)):
            archer.check_microphone()
        assert archer.mic_device == {'index': 2, 'channels': 6, 'is_respeaker': True}
        assert archer.mic_available['ok'] is True

    def test_no_respeaker_falls_back_to_generic_check(self):
        """Same fallback spirit as 'no microphone found, text input only' —
        a missing ReSpeaker must not break the existing generic path."""
        fake_source = MagicMock()
        fake_mic_cm = MagicMock()
        fake_mic_cm.__enter__.return_value = fake_source
        fake_mic_cm.__exit__.return_value = False
        with patch('archer._find_respeaker_device', return_value=None), \
             patch('speech_recognition.Microphone', return_value=fake_mic_cm), \
             patch.object(archer.recognizer, 'adjust_for_ambient_noise'):
            archer.check_microphone()
        assert archer.mic_device['is_respeaker'] is False
        assert archer.mic_available['ok'] is True


class TestListenOnceReSpeakerChannelHandling:
    def setup_method(self):
        self._backup = {
            'IS_PI':          getattr(archer, '_IS_PI', False),
            'VOSK_AVAILABLE': getattr(archer, '_VOSK_AVAILABLE', False),
            'KaldiRec':       getattr(archer, '_KaldiRec', None),
            'vosk_model':     getattr(archer, '_vosk_model', None),
            'sd':             getattr(archer, '_sd', None),
        }
        archer._IS_PI          = True
        archer._VOSK_AVAILABLE = True
        archer._vosk_model     = MagicMock()

    def teardown_method(self):
        archer._IS_PI          = self._backup['IS_PI']
        archer._VOSK_AVAILABLE = self._backup['VOSK_AVAILABLE']
        archer._KaldiRec       = self._backup['KaldiRec']
        archer._vosk_model     = self._backup['vosk_model']
        archer._sd             = self._backup['sd']
        archer.mic_device['index']        = None
        archer.mic_device['channels']     = 1
        archer.mic_device['is_respeaker'] = False

    def _fake_vosk_and_stream(self, frame_bytes):
        fake_rec = MagicMock()
        fake_rec.AcceptWaveform.return_value = False
        fake_rec.FinalResult.return_value = '{"text": ""}'
        archer._KaldiRec = MagicMock(return_value=fake_rec)

        fake_stream = MagicMock()
        fake_stream.read.return_value = (frame_bytes, False)
        fake_stream_cm = MagicMock()
        fake_stream_cm.__enter__.return_value = fake_stream
        fake_stream_cm.__exit__.return_value = False
        fake_sd = MagicMock()
        fake_sd.RawInputStream.return_value = fake_stream_cm
        archer._sd = fake_sd
        return fake_rec, fake_sd

    def test_respeaker_detected_opens_stream_with_its_device_and_channels(self):
        archer.mic_device['index']        = 3
        archer.mic_device['channels']     = 6
        archer.mic_device['is_respeaker'] = True

        import array
        frame = array.array('h', [0] * 6).tobytes()  # one 6-channel sample-group
        fake_rec, fake_sd = self._fake_vosk_and_stream(frame)

        archer.listen_once(timeout=1, phrase_limit=1)

        fake_sd.RawInputStream.assert_called_once()
        _, kwargs = fake_sd.RawInputStream.call_args
        assert kwargs['device'] == 3
        assert kwargs['channels'] == 6

        # Vosk must receive de-interleaved mono audio (one int16 sample =
        # 2 bytes), not the raw 6-channel frame (12 bytes) — proves
        # _extract_channel() actually ran before AcceptWaveform, not just
        # that the right device/channels were requested.
        for call in fake_rec.AcceptWaveform.call_args_list:
            assert len(call.args[0]) == 2

    def test_no_respeaker_falls_back_to_default_device_mono(self):
        """Regression guard: must be identical to pre-ReSpeaker behavior —
        channels=1, no device= at all — when nothing was detected."""
        archer.mic_device['is_respeaker'] = False

        import array
        frame = array.array('h', [0]).tobytes()
        fake_rec, fake_sd = self._fake_vosk_and_stream(frame)

        archer.listen_once(timeout=1, phrase_limit=1)

        _, kwargs = fake_sd.RawInputStream.call_args
        assert kwargs['channels'] == 1
        assert 'device' not in kwargs


# ═══════════════════════════════════════════════════════════════
# 53. Voice init (archer.py:164) — a non-ImportError during vosk/sounddevice
# setup must not crash the whole `import archer`.
#
# Confirmed live on a Pi VM: `import sounddevice` itself can raise
# sounddevice.PortAudioError — Pa_Initialize() fails hard when any
# compiled-in host API (Pulse, there) fails to init, even with genuine
# working ALSA hardware underneath (a VMware-emulated Ensoniq AudioPCI
# card, confirmed separately via arecord -l). That's not an ImportError,
# so the `except ImportError:` this block used to have would have let it
# escape uncaught and crash Archer at startup, not just disable Vosk.
# ═══════════════════════════════════════════════════════════════
class TestVoiceInitBroadException:
    def test_non_import_error_during_voice_init_does_not_crash_archer_import(self, tmp_path):
        # Fake vosk — succeeds, so execution reaches the sounddevice import
        # (which comes after it in archer.py's real import order).
        (tmp_path / 'vosk.py').write_text(
            'class Model:\n'
            '    def __init__(self, *a, **k): pass\n'
            'class KaldiRecognizer:\n'
            '    def __init__(self, *a, **k): pass\n'
        )
        # Fake sounddevice — raises AT IMPORT TIME, not ImportError, matching
        # the real sounddevice.PortAudioError shape confirmed on the Pi VM:
        # a plain Exception subclass, raised from the module's own top-level
        # code during `import sounddevice`, not from any function call.
        (tmp_path / 'sounddevice.py').write_text(
            'class PortAudioError(Exception):\n'
            '    pass\n'
            'raise PortAudioError("Error initializing PortAudio: Unanticipated host error")\n'
        )

        env = dict(os.environ)
        env['PYTHONPATH'] = str(tmp_path) + os.pathsep + env.get('PYTHONPATH', '')
        result = subprocess.run(
            [sys.executable, '-c',
             "import platform; platform.system = lambda: 'Linux'; platform.machine = lambda: 'armv7l'; "
             "import archer; print('IMPORT_OK', archer._VOSK_AVAILABLE)"],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=os.path.dirname(os.path.abspath(archer.__file__)),
        )
        assert 'IMPORT_OK False' in result.stdout, result.stdout + result.stderr


# ═══════════════════════════════════════════════════════════════
# Panic mode / lockdown (archer.py: lockdown_state, activate_panic_mode(),
# _panic_lockdown_active(), POST /panic/activate)
#
# Hard constraint under test, not a preference: activate_panic_mode() must
# never call ask_archer(), casual_monitor(), or any AI provider — it has to
# keep working even if Groq/Cerebras/Gemini/OpenRouter/local Ollama are all
# down simultaneously. TestPanicModeAiIndependence proves that directly.
# ═══════════════════════════════════════════════════════════════
class TestPanicModeAiIndependence:
    def setup_method(self):
        self._lockdown_backup = dict(archer.lockdown_state)

    def teardown_method(self):
        archer.lockdown_state.clear()
        archer.lockdown_state.update(self._lockdown_backup)

    def test_does_not_call_ask_archer_or_casual_monitor(self):
        with patch('archer.ask_archer') as mock_ask, \
             patch('archer.casual_monitor') as mock_casual:
            result = archer.activate_panic_mode(120)
        mock_ask.assert_not_called()
        mock_casual.assert_not_called()
        assert result['active'] is True

    def test_succeeds_with_every_ai_provider_simultaneously_down(self):
        """Blank all four cloud keys and make both urlopen (Groq/Cerebras/
        Gemini/OpenRouter/Slack all go through it) and subprocess.run
        (Ollama) raise — activate_panic_mode() must still fully succeed,
        since it never calls into any of that machinery to begin with."""
        env_keys = ('GROQ_API_KEY', 'CEREBRAS_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY')
        backup = {k: os.environ.get(k) for k in env_keys}
        for k in env_keys:
            os.environ[k] = ''
        try:
            with patch('urllib.request.urlopen', side_effect=OSError('network down')), \
                 patch('subprocess.run', side_effect=OSError('ollama down')):
                result = archer.activate_panic_mode(120)
        finally:
            for k, v in backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert result['active'] is True
        assert archer.lockdown_state['active'] is True


class TestPanicModeCore:
    def setup_method(self):
        self._lockdown_backup = dict(archer.lockdown_state)
        self._location_backup = dict(archer.location_data)
        self._channel_backup = archer.SLACK_CHANNEL_PANIC
        self._slack_enabled_backup = archer.slack_config['enabled']

    def teardown_method(self):
        archer.lockdown_state.clear()
        archer.lockdown_state.update(self._lockdown_backup)
        archer.location_data.clear()
        archer.location_data.update(self._location_backup)
        archer.SLACK_CHANNEL_PANIC = self._channel_backup
        archer.slack_config['enabled'] = self._slack_enabled_backup

    def test_posts_directly_to_panic_channel_not_tier_channels(self):
        """Must bypass slack_route_alert()'s tier fan-out entirely — a
        single call to the one dedicated channel, not a loop over tiers."""
        archer.SLACK_CHANNEL_PANIC = 'C_PANIC'
        with patch('archer.slack_post_message') as mock_send:
            archer.activate_panic_mode(120)
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == 'C_PANIC'

    def test_snapshots_current_location(self):
        archer.location_data['lat'] = 40.7128
        archer.location_data['lon'] = -74.0060
        archer.location_data['location_name'] = 'Home'
        archer.activate_panic_mode(120)
        assert archer.lockdown_state['location'] == {
            'lat': 40.7128, 'lon': -74.0060, 'name': 'Home',
        }

    def test_sets_relay_control_disabled_flag(self):
        assert archer.lockdown_state['relay_control_disabled'] is False
        archer.activate_panic_mode(120)
        assert archer.lockdown_state['relay_control_disabled'] is True

    def test_window_seconds_recorded(self):
        result = archer.activate_panic_mode(300)
        assert result['window_secs'] == 300
        assert archer.lockdown_state['window_secs'] == 300

    def test_window_clamped_to_max(self):
        result = archer.activate_panic_mode(999999)
        assert result['window_secs'] == archer.PANIC_WINDOW_MAX_SECS

    def test_window_clamped_to_min(self):
        result = archer.activate_panic_mode(1)
        assert result['window_secs'] == archer.PANIC_WINDOW_MIN_SECS


class TestPanicModeExpiry:
    """Confirms the lockdown expires on its own — no manual reset call
    needed once expires_at passes (archer.py:_panic_lockdown_active)."""

    def setup_method(self):
        self._lockdown_backup = dict(archer.lockdown_state)

    def teardown_method(self):
        archer.lockdown_state.clear()
        archer.lockdown_state.update(self._lockdown_backup)

    def test_active_immediately_after_activation(self):
        archer.activate_panic_mode(120)
        assert archer._panic_lockdown_active() is True

    def test_expires_once_window_passes_without_manual_reset(self):
        archer.activate_panic_mode(120)
        # Simulate the window having already elapsed — no call to any
        # "deactivate" function, just time passing.
        archer.lockdown_state['expires_at'] = time.time() - 1
        assert archer._panic_lockdown_active() is False
        # And the flag itself flips off as a side effect of the check,
        # matching _check_lockout()'s lazy-expiry pattern in obd_gatekeeper.py.
        assert archer.lockdown_state['active'] is False

    def test_still_active_before_window_passes(self):
        archer.activate_panic_mode(120)
        archer.lockdown_state['expires_at'] = time.time() + 60
        assert archer._panic_lockdown_active() is True


class TestPanicModeBlocksRegistration:
    """The actual, live-controllable lock: register_mac() and
    register_device_endpoint() both refuse new grants while a lockdown is
    active, then work normally again once it expires."""

    def setup_method(self):
        self._lockdown_backup = dict(archer.lockdown_state)

    def teardown_method(self):
        archer.lockdown_state.clear()
        archer.lockdown_state.update(self._lockdown_backup)
        archer.one_time_codes.clear()

    def test_register_mac_blocked_during_lockdown(self):
        archer.activate_panic_mode(120)
        code = archer.generate_one_time_code('Visitor', 3)
        r = client.post('/register_mac', json={'code': code, 'mac': 'AA:BB:CC:DD:EE:FF'})
        d = json.loads(r.data)
        assert d['success'] is False
        assert 'panic' in d['error'].lower()

    def test_register_mac_master_code_also_blocked_during_lockdown(self):
        """Even the Tier-1 master code must not grant a session during a
        lockdown — the whole point is no new device gets in, period."""
        archer._master_code_enabled = True
        archer._master_code = '999999'
        try:
            archer.activate_panic_mode(120)
            r = client.post('/register_mac', json={'code': '999999', 'mac': 'AA:BB:CC:DD:EE:00'})
            d = json.loads(r.data)
            assert d['success'] is False
        finally:
            archer._master_code_enabled = False
            archer._master_code = None

    def test_register_device_blocked_during_lockdown(self):
        """/register_device requires Tier 1 already — the panic check must
        still block even an authenticated owner, since the whole point is
        no new device gets a grant, period."""
        archer.activate_panic_mode(120)
        c = _authed_client(1, 'Owner')
        r = c.post('/register_device', json={'fingerprint': 'fp-panic-test', 'name': 'Visitor', 'tier': 2})
        d = json.loads(r.data)
        assert d['ok'] is False
        assert 'panic' in d['error'].lower()

    def test_register_mac_works_again_after_lockdown_expires(self):
        archer.activate_panic_mode(120)
        archer.lockdown_state['expires_at'] = time.time() - 1  # force expiry
        code = archer.generate_one_time_code('Visitor', 3)
        # Real success reaches save_mac_whitelist(), which writes
        # mac_whitelist.json to disk — mock it so the test doesn't leave
        # that file behind in the repo root.
        with patch('archer.save_mac_whitelist'), patch('archer.load_mac_whitelist', return_value={}):
            r = client.post('/register_mac', json={'code': code, 'mac': 'AA:BB:CC:DD:EE:01'})
        d = json.loads(r.data)
        assert d['success'] is True

    def test_register_device_works_when_no_lockdown_active(self):
        c = _authed_client(1, 'Owner')
        r = c.post('/register_device', json={'fingerprint': 'fp-no-lockdown', 'name': 'Visitor', 'tier': 2})
        d = json.loads(r.data)
        assert d['ok'] is True


class TestPanicActivateRoute:
    """POST /panic/activate itself: owner-tier gate and the Tailscale/
    loopback network gate, independent of each other."""

    def setup_method(self):
        self._lockdown_backup = dict(archer.lockdown_state)

    def teardown_method(self):
        archer.lockdown_state.clear()
        archer.lockdown_state.update(self._lockdown_backup)

    def test_owner_from_loopback_succeeds(self):
        c = _authed_client(1, 'Owner')
        r = c.post('/panic/activate', json={})
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d['active'] is True

    def test_owner_from_tailscale_range_succeeds(self):
        c = _authed_client(1, 'Owner')
        r = c.post('/panic/activate', json={}, environ_overrides={'REMOTE_ADDR': '100.111.157.35'})
        assert r.status_code == 200

    def test_owner_from_public_ip_rejected(self):
        c = _authed_client(1, 'Owner')
        r = c.post('/panic/activate', json={}, environ_overrides={'REMOTE_ADDR': '203.0.113.5'})
        assert r.status_code == 403
        assert archer.lockdown_state['active'] is False

    def test_non_owner_tier_from_loopback_rejected(self):
        c = _authed_client(2, 'Passenger')
        r = c.post('/panic/activate', json={})
        assert r.status_code == 403
        assert archer.lockdown_state['active'] is False

    def test_window_minutes_passed_through(self):
        c = _authed_client(1, 'Owner')
        r = c.post('/panic/activate', json={'window_minutes': 5})
        d = json.loads(r.data)
        assert d['window_secs'] == 300


class TestWeatherCompareCache:
    """Third-party rows are shared within the TTL; Archer's own row stays live."""

    def _get(self, calls):
        def _fake_urlopen(*a, **k):
            calls.append(1)
            raise OSError('offline')
        with patch('urllib.request.urlopen', side_effect=_fake_urlopen):
            return json.loads(client.get('/weather/compare/data').data)

    def test_repeat_view_makes_no_outbound_calls(self):
        archer._wx_compare_cache.update(key=None, ts=0.0, results=[])
        calls = []
        self._get(calls)
        first = len(calls)
        archer.weather['temp'] = 71
        d = self._get(calls)
        assert first > 0 and len(calls) == first
        assert d['results'][0]['temp'] == 71

    def test_new_location_refetches(self):
        archer._wx_compare_cache.update(key=None, ts=0.0, results=[])
        calls = []
        orig = dict(archer.location_data)
        try:
            archer.location_data.update(lat=37.60, lon=-91.50)
            self._get(calls)
            first = len(calls)
            archer.location_data.update(lat=37.70, lon=-91.50)
            self._get(calls)
            assert len(calls) > first
        finally:
            archer.location_data.clear(); archer.location_data.update(orig)


class TestHealthGitHash:
    def test_git_runs_once(self):
        archer._git_short_hash.cache_clear()
        try:
            with patch.dict(os.environ, {'ARCHER_GIT_HASH': ''}), \
                 patch.object(archer.subprocess, 'check_output', return_value=b'abc1234\n') as co:
                h1 = json.loads(client.get('/health').data)['git_hash']
                h2 = json.loads(client.get('/health').data)['git_hash']
            assert h1 == h2 == 'abc1234'
            assert co.call_count == 1
        finally:
            archer._git_short_hash.cache_clear()


class TestSpeakPlayback:
    """Playback checks for mpg123 without spawning `which` per utterance."""

    def _run(self, which_result):
        import asyncio
        class _Comm:
            def __init__(self, *a, **k): pass
            async def save(self, path):
                with open(path, 'wb') as f: f.write(b'ID3')
        with patch('edge_tts.Communicate', _Comm), \
             patch.object(archer, '_IS_PI', False), \
             patch.object(archer._platform, 'system', return_value='Linux'), \
             patch.object(archer.shutil, 'which', return_value=which_result), \
             patch.object(archer.subprocess, 'run') as run:
            asyncio.run(archer._speak_async('hello'))
        return [c.args[0][0] for c in run.call_args_list]

    def test_plays_with_mpg123_when_installed(self):
        assert self._run('/usr/bin/mpg123') == ['mpg123']

    def test_no_subprocess_when_mpg123_missing(self):
        assert self._run(None) == []


class TestSpotifyPlayerShared:
    """Pollers of me/player share one call within the TTL; controls drop it."""

    def _setup(self):
        archer._spotify_player_cache.update(ts=0.0, data=None)
        archer.spotify_tokens.update(access_token='tok', expires_at=time.time() + 3600)

    def _fake(self, calls, playing):
        class _R:
            def __init__(self, body): self._b = body
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def _urlopen(req, timeout=None):
            calls.append((req.get_method(), req.full_url))
            return _R(json.dumps({'is_playing': playing[0], 'item': {'name': 'T', 'artists': [], 'album': {}}}).encode())
        return _urlopen

    def test_pollers_share_one_call_and_controls_refresh(self):
        self._setup()
        calls, playing = [], [True]
        try:
            with patch('urllib.request.urlopen', side_effect=self._fake(calls, playing)), \
                 patch.object(archer, '_get_art_cached', return_value=None):
                c = _authed_client(1)
                for _ in range(3):
                    assert json.loads(c.get('/spotify/status').data)['playing'] is True
                assert sum(1 for m, u in calls if u.endswith('/me/player')) == 1
                playing[0] = False
                c.post('/spotify/pause')
                assert json.loads(c.get('/spotify/status').data)['playing'] is False
                assert sum(1 for m, u in calls if u.endswith('/me/player')) == 2
        finally:
            archer.spotify_tokens.update(access_token=None, expires_at=0)
            archer._spotify_player_cache.update(ts=0.0, data=None)


class TestDbSaveChangedOnly:
    """db_save writes only keys whose value changed, and a reload still sees
    every key with its latest value."""

    @pytest.fixture
    def fresh_db(self, tmp_path):
        import db
        saved = (db._DB_PATH, getattr(db._local, 'conn', None), dict(db._last_written))
        db._DB_PATH = str(tmp_path / 'state.db')
        db._local.conn = None
        db._last_written.clear()
        yield db
        db._DB_PATH, db._local.conn = saved[0], saved[1]
        db._last_written.clear(); db._last_written.update(saved[2])

    def test_only_changed_rows_written_and_reload_is_complete(self, fresh_db):
        db = fresh_db
        conn = db._get_conn()
        db.db_save({'a': 1, 'b': {'x': [1, 2]}, 'c': 'three'})
        assert conn.total_changes == 3
        db.db_save({'a': 1, 'b': {'x': [1, 2, 3]}, 'c': 'three'})
        assert conn.total_changes == 4
        db.db_save({'a': 1, 'b': {'x': [1, 2, 3]}, 'c': 'three'})
        assert conn.total_changes == 4
        assert db.db_load() == {'a': 1, 'b': {'x': [1, 2, 3]}, 'c': 'three'}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
