"""
blueprints/auth.py — Authentication, device management, and tier notification routes.
Extracted from archer.py; all state and business logic remain in archer.py,
accessed via late import to avoid circular dependencies.
"""
import hmac as _hmac
import secrets as _secrets

from flask import Blueprint, jsonify, make_response, request

from archer_state import _limiter, csrf_required, make_auth_jwt, decode_auth_jwt

bp = Blueprint('auth', __name__)


def _a():
    import archer as _ar
    return _ar


# ── DEVICE FINGERPRINT REGISTRATION (legacy in-cabin) ────────────────────────

@bp.route('/register_device', methods=['POST'])
@csrf_required
def register_device_endpoint():
    a = _a()
    if a._panic_lockdown_active():
        return jsonify({'ok': False, 'error': 'New device registration is locked (panic mode active)'})
    # Tier 1 only — the public registration path is /register_mac with invite codes
    ok, _ = a.require_tier1(request)
    if not ok:
        return jsonify({'ok': False, 'error': 'Tier 1 required'}), 403
    data        = request.get_json()
    fingerprint = data.get('fingerprint', '')
    name        = data.get('name', 'Unknown')
    tier        = max(2, min(4, int(data.get('tier', 2))))  # max Tier 2 via this path
    if not fingerprint:
        return jsonify({'ok': False, 'error': 'No fingerprint'})
    a.register_device(fingerprint, name, tier)
    return jsonify({'ok': True, 'name': name, 'tier': tier})


@bp.route('/device_tier', methods=['POST'])
@csrf_required
def device_tier_endpoint():
    a = _a()
    data        = request.get_json()
    fingerprint = data.get('fingerprint', '')
    tier        = a.get_device_tier(fingerprint)
    registered  = fingerprint in a.trusted_devices
    name        = a.trusted_devices.get(fingerprint, {}).get('name', 'Unknown')
    return jsonify({'tier': tier, 'registered': registered, 'name': name})


# ── SESSION / LOGOUT ──────────────────────────────────────────────────────────

@bp.route('/logout', methods=['POST'])
@csrf_required
def logout():
    """Invalidate the current session cookie.

    The token/jti is added to _revoked_tokens so it is rejected immediately even
    if the client still holds the cookie. Safe to call without a valid session.
    """
    a = _a()
    cookie_val = request.cookies.get('archer_auth', '')
    if cookie_val:
        # Only the signed JWT format is trusted for revocation/logging — the
        # legacy tier:name:hmac format is unsigned and attacker-settable, so
        # (unlike a prior version of this route) it is not parsed at all here.
        try:
            payload = decode_auth_jwt(cookie_val)
            jti = payload.get('jti', '')
            if jti:
                a._revoke_token(jti)
            a.log_security('LOGOUT', name=payload.get('name', '?'))
        except ValueError:
            pass
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie('archer_auth')
    return resp


# ── VEHICLE SELECTION ─────────────────────────────────────────────────────────

@bp.route('/set_vehicle', methods=['POST'])
@csrf_required
def set_vehicle():
    """Record which truck was purchased (tier 1 only).

    Body: {"make": "GMC"|"Chevrolet", "model": "Sierra 2500HD"|"Silverado 2500HD"}
    Both trucks share the same GMT800 platform, LQ4 engine, 4L80E, and DTC database,
    so this is purely for display and voice personality — no functional change.
    """
    a = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'error': 'Owner only'}), 403
    body = request.get_json(silent=True) or {}
    make  = body.get('make',  '').strip()
    model = body.get('model', '').strip()
    valid_makes  = {'GMC', 'Chevrolet'}
    valid_models = {'Sierra 2500HD', 'Silverado 2500HD'}
    if make not in valid_makes or model not in valid_models:
        return jsonify({'error': f'make must be one of {valid_makes}; model one of {valid_models}'}), 400
    a.vehicle_config['make']  = make
    a.vehicle_config['model'] = model
    a.save_state()
    a.log_security('VEHICLE_SET', name=f'{make} {model}')
    return jsonify({'ok': True, 'vehicle': a.get_vehicle_name()})


# ── MAC WHITELIST REGISTRATION ────────────────────────────────────────────────

@bp.route('/register_mac', methods=['POST'])
@_limiter.limit('5 per minute; 20 per hour')
@csrf_required
def register_mac():
    """Register a new device using a one-time code."""
    import re as _re
    from datetime import datetime
    a = _a()
    if a._panic_lockdown_active():
        return jsonify({'success': False, 'error': 'New device registration is locked (panic mode active)'})
    data = request.json or {}
    code = data.get('code', '').strip()
    mac  = data.get('mac', '').upper()
    # Reject anything that doesn't look like a real MAC address rather than
    # storing it verbatim — the whitelist's mac value is later rendered on
    # the Tier-1 /devices page, so this also closes an XSS vector at the
    # source in addition to the output-escaping fix on that page.
    if mac and mac != 'UNKNOWN' and not _re.fullmatch(r'([0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
        return jsonify({'success': False, 'error': 'Invalid MAC address format'})

    if not code:
        return jsonify({'success': False, 'error': 'Missing code'})

    # Same failure budget as the owner PIN — checked before the code is looked
    # at, so a locked-out attempt can't use up a real invite.
    locked = a._login_lockout_error()
    if locked:
        return jsonify({'success': False, 'error': locked})

    # Check master Tier 1 code first
    entry = None
    if a._master_code_enabled and a._master_code and _hmac.compare_digest(code, a._master_code):
        entry = {'name': 'Ayden', 'tier': 1}

    # Fall back to one-time code
    if not entry:
        entry = a.validate_one_time_code(code)
    if not entry:
        return jsonify({'success': False, 'error': a._login_failed() or 'Invalid or expired code'})
    a._login_succeeded()

    tier = entry['tier']
    name = entry['name']

    # Save MAC if we have one
    if mac and mac != 'UNKNOWN':
        whitelist = a.load_mac_whitelist()
        whitelist[mac] = {
            'tier':          tier,
            'name':          name,
            'registered_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_seen':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        a.save_mac_whitelist(whitelist)
        print(f'[AUTH] Registered MAC {mac} as {name} (Tier {tier})')

    # Set auth cookie (HS256 JWT)
    redirects = {1: '/', 2: '/passenger', 3: '/family', 4: '/valet'}
    resp = make_response(jsonify({'success': True, 'redirect': redirects.get(tier, '/'), 'name': name, 'tier': tier}))
    resp.set_cookie('archer_auth', make_auth_jwt(tier, name), max_age=86400*30, httponly=True, samesite='Lax')
    return resp


@bp.route('/deregister_mac', methods=['POST'])
@_limiter.limit('5 per minute; 20 per hour')
@csrf_required
def deregister_mac():
    """Remove a MAC from the whitelist (Tier 1 only).

    Also revokes the session token for the removed device so their browser
    session is invalidated immediately without waiting for cookie expiry.
    """
    a = _a()
    ok, _ = a.require_tier1(request)
    if not ok:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    data = request.json or {}
    mac  = data.get('mac', '').upper()
    whitelist = a.load_mac_whitelist()
    if mac in whitelist and whitelist[mac]['tier'] != 1:
        entry = whitelist[mac]
        del whitelist[mac]
        a.save_mac_whitelist(whitelist)
        # Revoke any active session cookie for this device
        a._revoke_by_name(entry['name'], entry['tier'])
        a.log_security('MAC_DEREGISTERED', mac=mac, name=entry['name'], tier=entry['tier'])
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Not found or protected'})


@bp.route('/registered_devices')
def registered_devices():
    """List all registered devices (Tier 1 only)."""
    a = _a()
    ok, tier = a.require_tier1(request)
    if not ok:
        return jsonify({'error': 'Tier 1 required', 'tier': tier}), 403
    whitelist = a.load_mac_whitelist()
    devices = [{'mac': mac, 'tier': info['tier'], 'name': info['name']}
               for mac, info in whitelist.items()]
    return jsonify({'devices': devices})


@bp.route('/devices')
def devices_page():
    """Tier 1 only — manage registered devices and generate codes."""
    import time as _time
    import html as _html
    a = _a()
    ok, tier = a.require_tier1(request)
    if not ok:
        return f'<html><body style="background:#000;color:#cc0000;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center"><div style="font-size:32px">🔒</div><div style="font-size:14px;letter-spacing:3px;margin-top:12px">ACCESS DENIED — TIER 1 ONLY</div></div></body></html>', 403
    whitelist = a.load_mac_whitelist()
    a.cleanup_expired_codes()
    active_codes = [(c, e) for c, e in a.one_time_codes.items() if not e['used']]

    # Values go into data-* attributes (plain HTML-attribute escaping is
    # sufficient there) and are read back via event delegation in JS below —
    # deliberately NOT interpolated into an inline onclick="fn('...')" JS
    # string. HTML-entity decoding of an attribute value happens before the
    # browser parses an inline handler's JS, so escaping a quote character
    # as &#x27; does not actually neutralize it there; a value routed through
    # that pattern would still be able to break out of the JS string once
    # decoded. data-* + addEventListener has no such gap.
    devices_html = ''.join(f"""
        <div class="device-row">
          <div>
            <div class="d-name">{_html.escape(info['name'])}</div>
            <div class="d-meta">Tier {info['tier']} — {_html.escape(mac)}</div>
          </div>
          <button data-action="remove-device" data-mac="{_html.escape(mac, quote=True)}" class="d-remove">REMOVE</button>
        </div>""" for mac, info in whitelist.items() if info['tier'] != 1)

    codes_html = ''.join(f"""
        <div class="code-row">
          <div>
            <div class="c-name">{_html.escape(entry['name'])} — Tier {entry['tier']}</div>
            <div class="c-code">{_html.escape(code)}</div>
            <div class="c-meta">Expires in {max(0,int((entry['expires']-_time.time())/3600))}h</div>
          </div>
          <button data-action="revoke-code" data-code="{_html.escape(code, quote=True)}" class="d-remove">REVOKE</button>
        </div>""" for code, entry in active_codes)

    return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Archer — Devices</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Bebas+Neue&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#000;color:#fff;font-family:'Share Tech Mono',monospace;padding:16px;max-width:420px;margin:0 auto}}
h1{{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:5px;color:#cc0000;margin-bottom:4px}}
.sub{{font-size:10px;color:#444;letter-spacing:2px;margin-bottom:20px}}
.section{{background:#0a0a0a;border:1px solid #1a1a1a;border-radius:8px;padding:14px;margin-bottom:12px}}
.section-title{{font-size:9px;color:#555;letter-spacing:3px;border-bottom:1px solid #1a1a1a;padding-bottom:8px;margin-bottom:10px}}
.device-row,.code-row{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #111}}
.device-row:last-child,.code-row:last-child{{border-bottom:none}}
.d-name,.c-name{{font-size:13px;color:#fff}}
.d-meta,.c-meta{{font-size:10px;color:#444;margin-top:2px}}
.c-code{{font-size:20px;color:#cc0000;letter-spacing:4px;margin:3px 0}}
.d-remove{{background:#1a0000;border:1px solid #330000;color:#cc0000;border-radius:4px;padding:5px 10px;cursor:pointer;font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:1px}}
.new-form{{display:flex;flex-direction:column;gap:10px}}
.input{{background:#0d0d0d;border:1px solid #333;border-radius:6px;padding:10px;color:#fff;font-family:'Share Tech Mono',monospace;font-size:13px;outline:none;width:100%}}
.input:focus{{border-color:#cc0000}}
select.input{{cursor:pointer}}
.gen-btn{{background:#cc0000;border:none;border-radius:6px;padding:12px;color:#fff;font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:4px;cursor:pointer;width:100%}}
.result{{background:#001a00;border:1px solid #003300;border-radius:6px;padding:14px;text-align:center;display:none}}
.result.on{{display:block}}
.result-code{{font-size:36px;color:#00cc44;letter-spacing:8px;font-weight:bold;margin:6px 0}}
.result-name{{font-size:11px;color:#00cc44;letter-spacing:2px}}
.result-exp{{font-size:10px;color:#444;margin-top:4px}}
.back{{color:#555;text-decoration:none;font-size:10px;letter-spacing:2px;display:inline-block;margin-bottom:16px}}
.empty{{font-size:11px;color:#333;text-align:center;padding:8px}}
</style>
</head><body>
<a href="/" class="back">← BACK TO ARCHER</a>
<h1>DEVICES</h1>
<div class="sub">MANAGE ACCESS — TIER 1 ONLY</div>

<div class="section">
  <div class="section-title">REGISTERED DEVICES</div>
  {devices_html if devices_html else '<div class="empty">No devices registered yet</div>'}
</div>

<div class="section">
  <div class="section-title">ACTIVE INVITE CODES</div>
  {codes_html if codes_html else '<div class="empty">No active codes</div>'}
</div>

<div class="section">
  <div class="section-title">GENERATE NEW INVITE CODE</div>
  <div class="new-form">
    <input class="input" id="new-name" placeholder="Person's name (e.g. Jake)" maxlength="30">
    <select class="input" id="new-tier">
      <option value="2">Tier 2 — Passenger</option>
      <option value="3">Tier 3 — Family</option>
      <option value="4">Tier 4 — Valet</option>
    </select>
    <button class="gen-btn" onclick="generateCode()">GENERATE CODE</button>
    <div class="result" id="result">
      <div class="result-name" id="result-name"></div>
      <div class="result-code" id="result-code"></div>
      <div class="result-exp">Valid for 24 hours — single use</div>
      <div style="font-size:10px;color:#444;margin-top:6px">Share this code with them</div>
    </div>
  </div>
</div>

<script>
async function generateCode() {{
  const name = document.getElementById('new-name').value.trim();
  const tier = document.getElementById('new-tier').value;
  if (!name) {{ alert('Enter a name first'); return; }}
  const r = await fetch('/generate_code', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{name, tier: parseInt(tier)}})
  }});
  const d = await r.json();
  if (d.code) {{
    document.getElementById('result-name').textContent = name + ' — Tier ' + tier;
    document.getElementById('result-code').textContent = d.code;
    document.getElementById('result').classList.add('on');
    document.getElementById('new-name').value = '';
  }}
}}
async function removeDevice(mac) {{
  if (!confirm('Remove ' + mac + '?')) return;
  await fetch('/deregister_mac', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{mac}})}});
  location.reload();
}}
async function revokeCode(code) {{
  await fetch('/revoke_code', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code}})}});
  location.reload();
}}
// Event delegation reading data-* attributes — values never touch an inline
// JS string, so no HTML/JS double-decoding escape-breakout is possible.
document.body.addEventListener('click', (e) => {{
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  if (btn.dataset.action === 'remove-device') removeDevice(btn.dataset.mac);
  else if (btn.dataset.action === 'revoke-code') revokeCode(btn.dataset.code);
}});
</script>
</body></html>"""


# ── ONE-TIME INVITE CODES ─────────────────────────────────────────────────────

@bp.route('/generate_code', methods=['POST'])
@_limiter.limit('5 per minute; 20 per hour')
@csrf_required
def generate_code_route():
    """Generate a one-time invite code (Tier 1 only)."""
    a = _a()
    ok, tier = a.require_tier1(request)
    if not ok:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    data = request.json or {}
    name = data.get('name', '').strip()
    inv_tier = data.get('tier', 2)
    if not name:
        return jsonify({'success': False, 'error': 'Name required'})
    code = a.generate_one_time_code(name, inv_tier)
    return jsonify({'success': True, 'code': code, 'name': name, 'tier': inv_tier})


@bp.route('/revoke_code', methods=['POST'])
@_limiter.limit('10 per minute')
@csrf_required
def revoke_code():
    """Revoke an unused invite code."""
    a = _a()
    ok, _ = a.require_tier1(request)
    if not ok:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    data = request.json or {}
    code = data.get('code', '')
    if code in a.one_time_codes:
        del a.one_time_codes[code]
    return jsonify({'success': True})


# ── MASTER SIGN-IN CODE API ───────────────────────────────────────────────────

@bp.route('/sign_in_code/status')
def sign_in_code_status():
    """Return master code status and registered devices — Tier 1 only."""
    from datetime import datetime
    a = _a()
    a._check_master_auto_enable()
    if a.get_request_tier(request) != 1:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    try:
        wl = a.load_mac_whitelist()
        has_tier1 = any(v.get('tier') == 1 for v in wl.values())
    except Exception:
        wl = {}
        has_tier1 = False

    # Update last_seen for the requesting device's MAC (if known)
    try:
        req_mac = a.get_client_mac(request)
        if req_mac and req_mac in wl:
            wl[req_mac]['last_seen'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            a.save_mac_whitelist(wl)
    except Exception:
        pass

    # Build per-tier device counts and devices list
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    devices_list = []
    for mac, info in wl.items():
        t = info.get('tier', 0)
        if t in tier_counts:
            tier_counts[t] += 1
        devices_list.append({
            'mac':           mac,
            'name':          info.get('name', 'Unknown'),
            'tier':          t,
            'registered_at': info.get('registered_at', 'Unknown'),
            'last_seen':     info.get('last_seen', 'Never'),
        })

    return jsonify({
        'success':      True,
        'enabled':      a._master_code_enabled,
        'code':         a._master_code if a._master_code_enabled else None,
        'auto_on':      not has_tier1,
        'device_count': len(wl),
        'tier_counts':  tier_counts,
        'devices':      devices_list,
    })


@bp.route('/sign_in_code/toggle', methods=['POST'])
@_limiter.limit('10 per minute')
@csrf_required
def sign_in_code_toggle():
    """Toggle master sign-in code on or off — Tier 1 only."""
    a = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    a._master_code_enabled = not a._master_code_enabled
    return jsonify({'success': True, 'enabled': a._master_code_enabled})


@bp.route('/sign_in_code/refresh', methods=['POST'])
@_limiter.limit('5 per minute')
@csrf_required
def sign_in_code_refresh():
    """Generate a new master sign-in code — Tier 1 only."""
    a = _a()
    if a.get_request_tier(request) != 1:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    a._master_code = str(_secrets.randbelow(900000) + 100000)
    return jsonify({'success': True, 'code': a._master_code})


# ── TIER NOTIFICATION SYSTEM ──────────────────────────────────────────────────

@bp.route('/notify_tier1', methods=['POST'])
@_limiter.limit('20 per minute')
@csrf_required
def notify_tier1():
    from archer_state import _validate_csrf
    a = _a()
    # Require at least tier 2 (passenger) — reject unauthenticated senders
    ok, tier = a.require_tier1(request)
    if not ok and tier > 2:
        return jsonify({'error': 'Not authorized'}), 403
    if not _validate_csrf(request):
        return jsonify({'error': 'CSRF validation failed'}), 403
    data      = request.json or {}
    from_name = data.get('from', 'Passenger')
    message   = data.get('message', '')
    speed     = data.get('speed', 0)
    nid       = a.add_tier_notification(from_name, message, speed)
    return jsonify({'ok': True, 'id': nid})


@bp.route('/tier_notifications')
def get_tier_notifications():
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Not authorized'}), 403
    return jsonify({'notifications': list(a.tier_notifications)})


@bp.route('/tier_cancel', methods=['POST'])
@csrf_required
def tier_cancel():
    """Tier 2 cancels a pending request — removes it from queue."""
    from archer_state import _validate_csrf
    a = _a()
    if a.get_request_tier(request) > 2:
        return jsonify({'error': 'Not authorized'}), 403
    if not _validate_csrf(request):
        return jsonify({'error': 'CSRF validation failed'}), 403
    data = request.json or {}
    nid  = data.get('id')
    if nid:
        a.tier_responses[nid] = 'cancelled'
        for n in a.tier_notifications:
            if n['id'] == nid:
                n['status'] = 'cancelled'
                break
        print(f'[TIER CANCEL] {nid} cancelled by passenger')
    return jsonify({'ok': True})


@bp.route('/tier_respond', methods=['POST'])
@csrf_required
def tier_respond():
    from archer_state import _validate_csrf
    a = _a()
    ok, tier = a.require_tier1(request)
    if not ok:
        return jsonify({'error': 'Tier 1 required'}), 403
    if not _validate_csrf(request):
        return jsonify({'error': 'CSRF validation failed'}), 403
    data     = request.json or {}
    nid      = data.get('id')
    response = data.get('response')  # 'approved' or 'denied'
    action   = data.get('action', '')
    if nid and response:
        a.tier_responses[nid] = response
        for n in a.tier_notifications:
            if n['id'] == nid:
                n['status'] = response
                break
        # If approved, execute the action
        if response == 'approved' and action:
            if 'sport' in action.lower():
                a.truck_state['drive_mode'] = 'sport'
            elif 'comfort' in action.lower():
                a.truck_state['drive_mode'] = 'comfort'
            elif 'eco' in action.lower():
                a.truck_state['drive_mode'] = 'eco'
            elif 'tow' in action.lower():
                a.truck_state['drive_mode'] = 'tow'
            print(f'[TIER RESPOND] {nid} -> {response} ({action})')
    return jsonify({'ok': True})


@bp.route('/tier_response_status')
def tier_response_status():
    a = _a()
    nid = request.args.get('id')
    if not nid:
        return jsonify({'status': 'unknown'})
    status = a.tier_responses.get(nid, 'unknown')
    # Find the notification for context
    for n in a.tier_notifications:
        if n['id'] == nid:
            return jsonify({'status': status, 'message': n['message'], 'from': n['from']})
    return jsonify({'status': status})
