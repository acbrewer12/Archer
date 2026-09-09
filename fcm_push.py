"""Firebase Cloud Messaging push delivery for the Archer Android app."""
from __future__ import annotations

import json
import os
import re
import threading
import time

from config import FIREBASE_SERVICE_ACCOUNT_JSON

_lock = threading.Lock()
_tokens: set[str] = set()
_last_sent: dict[str, float] = {}
_cooldown_secs = 60
_firebase_ready = False
_init_attempted = False
_token_batch_failure_count: dict[str, int] = {}
_BATCH_FAILURE_WARN_THRESHOLD = 3

# Firebase error codes that always mean this registration target is dead.
_STALE_TOKEN_CODES = frozenset({
    'NOT_FOUND',
    'UNREGISTERED',
    'SENDER_ID_MISMATCH',
})

# Substrings in error text that tie INVALID_ARGUMENT (or SDK quirks) to the token.
_TOKEN_ERROR_MARKERS = (
    'registration token',
    'not registered',
    'invalid registration token',
    'registration-token-not-registered',
    'invalid-registration-token',
    'unregistered',
    'sender id mismatch',
)


def tokens_path() -> str:
    return os.path.join(os.path.dirname(__file__), 'fcm_tokens.json')


def load_tokens() -> None:
    """Load persisted device tokens into memory."""
    path = tokens_path()
    loaded: list[str] = []
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                loaded = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    except Exception as exc:
        print(f'[FCM] Failed to load tokens: {str(exc)[:60]}')
    with _lock:
        _tokens.update(loaded)


def _persist_tokens_locked() -> None:
    path = tokens_path()
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(sorted(_tokens), fh)
    except Exception as exc:
        print(f'[FCM] Failed to save tokens: {str(exc)[:60]}')


def register_token(token: str) -> bool:
    """Register a device token. Returns True if newly added."""
    token = (token or '').strip()
    if not token or len(token) > 512:
        return False
    with _lock:
        added = token not in _tokens
        _tokens.add(token)
        _persist_tokens_locked()
    return added


def get_tokens() -> list[str]:
    with _lock:
        return sorted(_tokens)


def _remove_tokens(stale: set[str]) -> None:
    if not stale:
        return
    with _lock:
        before = len(_tokens)
        _tokens.difference_update(stale)
        if len(_tokens) != before:
            _persist_tokens_locked()
            print(f'[FCM] Removed {before - len(_tokens)} invalid token(s)')


def _plain_text(text: str) -> str:
    """Strip Discord-style markdown for mobile notification bodies."""
    if not text:
        return ''
    out = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    out = re.sub(r'\*\*([^*]+)\*\*', r'\1', out)
    out = re.sub(r'\*([^*]+)\*', r'\1', out)
    return out.strip()


def _load_service_account_credentials():
    """Parse FIREBASE_SERVICE_ACCOUNT_JSON as inline JSON or a file path."""
    from firebase_admin import credentials

    raw = (FIREBASE_SERVICE_ACCOUNT_JSON or '').strip()
    if not raw:
        raise ValueError('FIREBASE_SERVICE_ACCOUNT_JSON not set')
    if os.path.isfile(raw):
        return credentials.Certificate(raw)
    return credentials.Certificate(json.loads(raw))


def _ensure_firebase() -> bool:
    global _firebase_ready, _init_attempted
    if _firebase_ready:
        return True
    if _init_attempted:
        return False
    _init_attempted = True
    if not (FIREBASE_SERVICE_ACCOUNT_JSON or '').strip():
        return False
    try:
        import firebase_admin

        if firebase_admin._apps:
            _firebase_ready = True
            return True

        cred = _load_service_account_credentials()
        firebase_admin.initialize_app(cred)
        _firebase_ready = True
        print('[FCM] Firebase initialized')
        return True
    except Exception as exc:
        print(f'[FCM] Firebase init failed: {str(exc)[:80]}')
        return False


def _token_log_suffix(token: str) -> str:
    return token[-8:] if len(token) > 8 else token


def _message_indicates_token_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TOKEN_ERROR_MARKERS)


def _is_stale_token_error(exc: Exception) -> bool:
    code = getattr(exc, 'code', None)
    if code in _STALE_TOKEN_CODES:
        return True
    name = type(exc).__name__
    if name in ('UnregisteredError', 'SenderIdMismatchError'):
        return True
    if code == 'INVALID_ARGUMENT':
        return _message_indicates_token_error(exc)
    return _message_indicates_token_error(exc)


def _record_token_delivery(token: str, *, success: bool) -> None:
    """Track consecutive batch failures per token; warn when a device may be silent."""
    with _lock:
        if success:
            _token_batch_failure_count.pop(token, None)
            return
        count = _token_batch_failure_count.get(token, 0) + 1
        _token_batch_failure_count[token] = count
        if count == _BATCH_FAILURE_WARN_THRESHOLD or (
            count > _BATCH_FAILURE_WARN_THRESHOLD
            and count % _BATCH_FAILURE_WARN_THRESHOLD == 0
        ):
            print(
                f'[FCM] SUSPECT TOKEN ...{_token_log_suffix(token)}: '
                f'{count} consecutive batches failed to deliver — '
                f're-open Archer app on that device to re-register'
            )


def _record_batch_catastrophic_failure(tokens: list[str], exc: Exception) -> None:
    print(
        f'[FCM] BATCH SEND FAILED (all {len(tokens)} device(s) affected): '
        f'{str(exc)[:120]}'
    )
    for token in tokens:
        _record_token_delivery(token, success=False)


def send_push(title: str, body: str, data: dict | None = None) -> int:
    """Send a push notification to all registered tokens.

    Returns the number of successful deliveries. Invalid tokens are removed.
    Never raises — failures are logged and skipped.
    """
    title = (title or 'Archer').strip() or 'Archer'
    body = _plain_text(body or '')
    tokens = get_tokens()
    if not tokens:
        return 0
    if not _ensure_firebase():
        return 0

    try:
        from firebase_admin import messaging
    except Exception as exc:
        print(f'[FCM] messaging import failed: {str(exc)[:60]}')
        return 0

    payload = {'title': title, 'body': body}
    if data:
        payload.update({k: str(v) for k, v in data.items()})

    stale: set[str] = set()
    sent = 0
    notification = messaging.Notification(title=title, body=body)
    try:
        if len(tokens) == 1:
            token = tokens[0]
            try:
                messaging.send(messaging.Message(
                    notification=notification,
                    data=payload,
                    token=token,
                ))
                sent = 1
                _record_token_delivery(token, success=True)
            except Exception as exc:
                if _is_stale_token_error(exc):
                    stale.add(token)
                else:
                    print(f'[FCM] Send failed: {str(exc)[:80]}')
                    _record_token_delivery(token, success=False)
        else:
            message = messaging.MulticastMessage(
                notification=notification,
                data=payload,
                tokens=tokens,
            )
            response = messaging.send_each_for_multicast(message)
            for idx, resp in enumerate(response.responses):
                token = tokens[idx]
                if resp.success:
                    sent += 1
                    _record_token_delivery(token, success=True)
                elif resp.exception and _is_stale_token_error(resp.exception):
                    stale.add(token)
                elif resp.exception:
                    print(f'[FCM] Send failed: {str(resp.exception)[:80]}')
                    _record_token_delivery(token, success=False)
    except Exception as exc:
        _record_batch_catastrophic_failure(tokens, exc)
        return 0

    _remove_tokens(stale)
    with _lock:
        for token in stale:
            _token_batch_failure_count.pop(token, None)
    if sent:
        print(f'[FCM] Delivered to {sent}/{len(tokens)} device(s)')
    return sent


def send_vehicle_alert(
    alert_type: str,
    message: str,
    title: str = 'ARCHER',
    *,
    skip_cooldown: bool = False,
    cooldown_secs: int | None = None,
) -> int:
    """Send a vehicle alert push with optional cooldown (mirrors discord_alert)."""
    if not alert_type:
        return 0
    cd = cooldown_secs if cooldown_secs is not None else _cooldown_secs
    if not skip_cooldown:
        now = time.time()
        with _lock:
            last = _last_sent.get(alert_type, 0)
            if now - last < cd:
                return 0
            _last_sent[alert_type] = now
    return send_push(title or 'ARCHER', message, data={'type': alert_type})


def send_tier_request(from_name: str, message: str) -> int:
    """Push a Tier 2+ request to Tier 1 (owner) devices."""
    title = f'Request from {from_name or "Passenger"}'
    body = (message or 'Attention requested').strip()
    return send_push(title, body, data={'type': 'tier_request', 'from': from_name or 'Passenger'})


def reset_for_tests() -> None:
    """Clear in-memory state — test helper only."""
    global _firebase_ready, _init_attempted
    with _lock:
        _tokens.clear()
        _last_sent.clear()
        _token_batch_failure_count.clear()
    _firebase_ready = False
    _init_attempted = False


load_tokens()
