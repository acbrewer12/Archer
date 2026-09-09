"""Tests for Firebase Cloud Messaging push delivery."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('ARCHER_SECRET', 'test_secret_xyz')
os.environ.setdefault('FIREBASE_SERVICE_ACCOUNT_JSON', '')

import fcm_push


@pytest.fixture(autouse=True)
def _reset_fcm_state(tmp_path, monkeypatch):
    """Isolate token storage and in-memory state per test."""
    tok_file = tmp_path / 'fcm_tokens.json'
    monkeypatch.setattr(fcm_push, 'tokens_path', lambda: str(tok_file))
    fcm_push.reset_for_tests()
    yield
    fcm_push.reset_for_tests()


class TestTokenStorage:
    def test_register_token_persists(self):
        assert fcm_push.register_token('abc123') is True
        assert fcm_push.get_tokens() == ['abc123']
        fcm_push.reset_for_tests()
        fcm_push.load_tokens()
        assert fcm_push.get_tokens() == ['abc123']

    def test_register_duplicate_returns_false(self):
        assert fcm_push.register_token('tok-a') is True
        assert fcm_push.register_token('tok-a') is False
        assert fcm_push.get_tokens() == ['tok-a']

    def test_register_rejects_empty_and_oversized(self):
        assert fcm_push.register_token('') is False
        assert fcm_push.register_token('x' * 513) is False
        assert fcm_push.get_tokens() == []

    def test_load_tokens_ignores_bad_file(self):
        path = fcm_push.tokens_path()
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('not json')
        fcm_push.load_tokens()
        assert fcm_push.get_tokens() == []


class TestPlainText:
    def test_strips_markdown(self):
        raw = 'Oil temp critical at **225F**. See [Map](https://maps.example.com)'
        assert fcm_push._plain_text(raw) == 'Oil temp critical at 225F. See Map'


class TestStaleTokenDetection:
    def test_invalid_argument_payload_error_is_not_stale(self):
        exc = Exception('Invalid data message key: foo must be string')
        exc.code = 'INVALID_ARGUMENT'
        assert fcm_push._is_stale_token_error(exc) is False

    def test_invalid_argument_registration_token_is_stale(self):
        exc = Exception('Invalid registration token provided')
        exc.code = 'INVALID_ARGUMENT'
        assert fcm_push._is_stale_token_error(exc) is True

    def test_unregistered_code_is_stale_without_message(self):
        exc = Exception('gone')
        exc.code = 'UNREGISTERED'
        assert fcm_push._is_stale_token_error(exc) is True


class TestSendPush:
    def test_send_push_no_tokens_is_noop(self):
        assert fcm_push.send_push('Title', 'Body') == 0

    def test_send_push_without_firebase_config_is_noop(self):
        fcm_push.register_token('device-1')
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', ''):
            fcm_push._init_attempted = False
            fcm_push._firebase_ready = False
            assert fcm_push.send_push('Alert', 'Hello') == 0

    def test_send_push_delivers_and_cleans_stale_tokens(self):
        good_resp = MagicMock(success=True, exception=None)
        bad_exc = MagicMock()
        bad_exc.code = 'NOT_FOUND'
        type(bad_exc).__name__ = 'UnregisteredError'
        bad_resp = MagicMock(success=False, exception=bad_exc)

        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.MulticastMessage = MagicMock(side_effect=lambda **kw: kw)

        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app') as init_app:
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('good-token')
                            fcm_push.register_token('bad-token')
                            tokens = fcm_push.get_tokens()
                            bad_idx = tokens.index('bad-token')
                            good_idx = tokens.index('good-token')
                            responses = [None, None]
                            responses[bad_idx] = bad_resp
                            responses[good_idx] = good_resp
                            fake_messaging.send_each_for_multicast.return_value = MagicMock(
                                responses=responses,
                            )
                            sent = fcm_push.send_push('Oil', '**225F** critical')
                            assert sent == 1
                            init_app.assert_called_once()
                            assert fcm_push.get_tokens() == ['good-token']
                            call_kw = fake_messaging.MulticastMessage.call_args.kwargs
                            assert call_kw['notification']['title'] == 'Oil'
                            assert call_kw['notification']['body'] == '225F critical'

    def test_send_push_single_token_uses_messaging_send(self):
        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.Message = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.send.return_value = 'projects/archer/messages/abc'

        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('solo-token')
                            sent = fcm_push.send_push('Radar', 'Ka band ahead')
                            assert sent == 1
                            fake_messaging.send.assert_called_once()
                            fake_messaging.send_each_for_multicast.assert_not_called()
                            msg_kw = fake_messaging.Message.call_args.kwargs
                            assert msg_kw['token'] == 'solo-token'
                            assert msg_kw['notification']['title'] == 'Radar'

    def test_send_push_single_token_cleans_stale(self):
        UnregisteredError = type('UnregisteredError', (Exception,), {})

        def _raise_unregistered(*_a, **_kw):
            exc = UnregisteredError('not registered')
            exc.code = 'UNREGISTERED'
            raise exc

        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.Message = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.send.side_effect = _raise_unregistered

        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('expired-token')
                            assert fcm_push.send_push('T', 'B') == 0
                            assert fcm_push.get_tokens() == []

    def test_credentials_from_file_path(self, tmp_path):
        cred_file = tmp_path / 'firebase-sa.json'
        cred_file.write_text(json.dumps({'type': 'service_account', 'project_id': 'archer-test'}))
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', str(cred_file)):
            with patch('firebase_admin.credentials.Certificate') as cert_ctor:
                cred = fcm_push._load_service_account_credentials()
                cert_ctor.assert_called_once_with(str(cred_file))
                assert cred is cert_ctor.return_value

    def test_send_push_never_raises_on_multicast_failure(self):
        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.MulticastMessage = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.send_each_for_multicast.side_effect = RuntimeError('network down')

        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('device-1')
                            fcm_push.register_token('device-2')
                            assert fcm_push.send_push('T', 'B') == 0
                            assert fcm_push.get_tokens() == ['device-1', 'device-2']

    def test_invalid_argument_payload_error_keeps_token(self):
        InvalidArgumentError = type('InvalidArgumentError', (Exception,), {})

        def _raise_payload_error(*_a, **_kw):
            exc = InvalidArgumentError('Invalid data message key must be string')
            exc.code = 'INVALID_ARGUMENT'
            raise exc

        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.Message = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.send.side_effect = _raise_payload_error

        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('good-device-token')
                            assert fcm_push.send_push('T', 'B') == 0
                            assert fcm_push.get_tokens() == ['good-device-token']

    def test_batch_catastrophic_failure_warns_after_threshold(self, capsys):
        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.MulticastMessage = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.send_each_for_multicast.side_effect = RuntimeError('network down')

        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('device-aaaa-bbbb')
                            fcm_push.register_token('device-cccc-dddd')
                            for _ in range(3):
                                assert fcm_push.send_push('T', 'B') == 0
                            out = capsys.readouterr().out
                            assert '[FCM] BATCH SEND FAILED' in out
                            assert 'SUSPECT TOKEN ...aaa-bbbb' in out
                            assert 'SUSPECT TOKEN ...ccc-dddd' in out
                            assert fcm_push.get_tokens() == ['device-aaaa-bbbb', 'device-cccc-dddd']

    def test_successful_delivery_resets_batch_failure_streak(self, capsys):
        cred = {'type': 'service_account', 'project_id': 'archer-test'}
        fake_messaging = MagicMock()
        fake_messaging.Notification = MagicMock(side_effect=lambda **kw: kw)
        fake_messaging.Message = MagicMock(side_effect=lambda **kw: kw)

        def _fail_then_succeed(*_a, **_kw):
            if fake_messaging.send.call_count <= 2:
                raise RuntimeError('network down')
            return 'projects/archer/messages/ok'

        fake_messaging.send.side_effect = _fail_then_succeed

        with patch.object(fcm_push, 'FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(cred)):
            with patch('firebase_admin._apps', {}):
                with patch('firebase_admin.initialize_app'):
                    with patch('firebase_admin.credentials.Certificate'):
                        with patch.dict(sys.modules, {'firebase_admin.messaging': fake_messaging}):
                            fcm_push.reset_for_tests()
                            fcm_push._init_attempted = False
                            fcm_push._firebase_ready = False
                            fcm_push.register_token('solo-device-token')
                            assert fcm_push.send_push('T', 'B') == 0
                            assert fcm_push.send_push('T', 'B') == 0
                            assert fcm_push.send_push('T', 'B') == 1
                            out = capsys.readouterr().out
                            assert 'SUSPECT TOKEN' not in out


class TestVehicleAndTierAlerts:
    def test_vehicle_alert_respects_cooldown(self):
        with patch.object(fcm_push, 'send_push', return_value=1) as send:
            assert fcm_push.send_vehicle_alert('oil_high', 'Hot') == 1
            assert fcm_push.send_vehicle_alert('oil_high', 'Hot again') == 0
            assert send.call_count == 1

    def test_tier_request_push(self):
        with patch.object(fcm_push, 'send_push', return_value=1) as send:
            assert fcm_push.send_tier_request('Alice', 'Unlock sport mode') == 1
            send.assert_called_once_with(
                'Request from Alice',
                'Unlock sport mode',
                data={'type': 'tier_request', 'from': 'Alice'},
            )
