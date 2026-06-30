"""
blueprints/terminal.py — Terminal, log stream, Pi tunnel, and system health routes.
Requires Tier 1 auth for all exec/stream endpoints.
"""
import os
import re as _re
import json
import shlex
import subprocess
import platform as _plt
import time

# Pi registration token — must be set explicitly; no random fallback so the Pi
# always knows the token and it doesn't silently change on server restart.
_ARCHER_PI_TOKEN: str = os.environ.get('ARCHER_PI_TOKEN', '')
if not _ARCHER_PI_TOKEN:
    print('[SECURITY] WARNING: ARCHER_PI_TOKEN not set — Pi tunnel registration will be rejected. '
          'Set ARCHER_PI_TOKEN in archer.env and export it in pi_connect.sh.')

from datetime import datetime
from flask import Blueprint, jsonify, Response, request

from archer_state import _limiter, csrf_required

if _plt.system() != 'Windows':
    try:
        import pty
    except ImportError:
        pty = None
else:
    pty = None

bp = Blueprint('terminal', __name__)

# Pi tunnel state lives here so both pi_register and pi_status share it.
pi_tunnel_url = {'url': None, 'online': False, 'last_seen': None}

# ── ACCESS CONTROL ────────────────────────────────────────────────────────────
TERMINAL_ALLOWED_TIERS = [1]


def _get_request_tier(req):
    """Delegate to archer.get_request_tier (supports JWT and legacy hmac format)."""
    import archer as _a
    return _a.get_request_tier(req)


def _terminal_access_check(req):
    tier = _get_request_tier(req)
    return tier in TERMINAL_ALLOWED_TIERS, tier


# ── DANGEROUS COMMAND PATTERN ─────────────────────────────────────────────────
_DANGEROUS = _re.compile(
    r'\brm\s+(-[a-z]*f[a-z]*\s+)?/'      # rm -rf / or rm /
    r'|\bmkfs\b'
    r'|\bdd\s+if=/dev/zero\b'
    r'|\b(shutdown|reboot|poweroff|halt)\b'
    r'|:\(\)\s*\{.*\|.*&'                 # fork bomb
    r'|\bpasswd\b|\buseradd\b|\buserdel\b'
    r'|\|\s*(sh|bash|zsh|dash)\b'         # pipe to shell
    r'|>\s*/dev/sd'                        # overwrite disk
    r'|\bchmod\s+[0-7]*7\s+/'             # chmod on root
    r'|\bcrontab\s+-r\b'
)

# ── TERMINAL PAGE ─────────────────────────────────────────────────────────────

@bp.route('/terminal')
def terminal_page():
    allowed, tier = _terminal_access_check(request)
    if not allowed:
        return Response(
            f"""<!DOCTYPE html><html><body style="background:#000;color:#cc0000;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center"><div style="font-size:32px;margin-bottom:16px">&#128274;</div>
            <div style="font-size:14px;letter-spacing:3px">ACCESS DENIED</div>
            <div style="font-size:10px;color:#333;margin-top:8px;letter-spacing:2px">TIER {tier} — TERMINAL REQUIRES TIER 1</div></div>
            </body></html>""",
            mimetype='text/html',
        )

    terminal_html = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Archer Terminal</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:#0a0a0a;color:#ff3333;font-family:'Courier New',monospace;display:flex;flex-direction:column;overflow:hidden}
#header{background:#0d0d0d;border-bottom:1px solid #1a1a1a;padding:8px 12px;display:flex;align-items:center;gap:12px;flex-shrink:0}
#header-title{font-size:11px;letter-spacing:3px;color:#cc0000;flex:1}
.tab-btn{background:none;border:1px solid #222;color:#444;font-family:monospace;font-size:10px;letter-spacing:2px;padding:4px 10px;border-radius:3px;cursor:pointer;transition:all 0.2s}
.tab-btn.active{border-color:#cc0000;color:#cc0000;background:#1a0000}
#pi-status{font-size:9px;letter-spacing:1px;padding:3px 8px;border-radius:3px}
.pi-online{background:#001a00;color:#00ff00;border:1px solid #00ff00}
.pi-offline{background:#1a0000;color:#cc0000;border:1px solid #330000}
#terminal-container{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
#output{flex:1;min-height:0;padding:10px 12px 70px;overflow-y:auto;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all}
#log-output{flex:1;min-height:0;padding:10px 12px 70px;overflow-y:auto;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-all;display:none}
.log-spotify{color:#1db954}.log-archer{color:#cc4444}.log-display{color:#cc8800}
.log-auth{color:#4488ff}.log-voice{color:#44cccc}.log-arduino{color:#ff8800}
.log-you{color:#ffffff}
.log-err{color:#ff4444}.log-default{color:#888}
#log-filter{background:#000;border:1px solid #222;color:#888;font-family:'Courier New',monospace;font-size:10px;padding:4px 8px;outline:none;width:160px;border-radius:3px}
#log-filter:focus{border-color:#cc0000}
#log-toolbar{display:none;padding:6px 8px;border-bottom:1px solid #1a1a1a;background:#050505;flex-shrink:0;gap:8px;align-items:center}
.log-dot{width:8px;height:8px;border-radius:50%;background:#00ff00;animation:blink 1.5s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}
#input-row{position:fixed;bottom:0;left:0;right:0;display:flex;padding:8px;padding-bottom:calc(8px + env(safe-area-inset-bottom,0px));border-top:2px solid #1a1a1a;background:#050505;align-items:center;z-index:100}
.prompt-label{color:#cc0000;padding:6px 8px;font-size:13px;flex-shrink:0}
#cmd{flex:1;background:#111;color:#ff3333;border:1px solid #333;border-radius:4px;padding:8px 10px;font-family:'Courier New',monospace;font-size:16px;outline:none;caret-color:#ff3333;-webkit-user-select:text;user-select:text;touch-action:manipulation}
#cmd:focus{border-color:#cc0000;background:#0d0000}
#send-btn{background:#1a0000;border:1px solid #cc0000;color:#cc0000;font-family:monospace;font-size:11px;letter-spacing:1px;padding:8px 16px;border-radius:4px;cursor:pointer;margin-left:6px;flex-shrink:0;touch-action:manipulation;min-width:52px;min-height:40px}
#send-btn:active{background:#330000}
.line-prompt{color:#cc0000}
.line-out{color:#ff6666}
.line-err{color:#ff4444}
.line-info{color:#333}
.line-success{color:#00ff00}
.line-system{color:#888}
#pi-iframe{width:100%;height:100%;border:none;display:none}
</style>
</head>
<body>
<div id="header">
  <div id="header-title">&#9889; ARCHER TERMINAL</div>
  <button class="tab-btn active" id="tab-server" onclick="switchTab('server')">SERVER</button>
  <button class="tab-btn" id="tab-logs" onclick="switchTab('logs')">LOGS</button>
  <button class="tab-btn" id="tab-pi" onclick="switchTab('pi')">PI</button>
  <span id="pi-status" class="pi-offline">PI OFFLINE</span>
</div>
<div id="terminal-container">
  <div id="server-terminal" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div id="output"></div>
    <div id="input-row">
      <span class="prompt-label">&#9654;</span>
      <input id="cmd" type="text" placeholder="enter command..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" inputmode="text"/>
      <button id="send-btn" onclick="sendCmd()">RUN</button>
    </div>
  </div>
  <div id="logs-panel" style="display:none;flex-direction:column;flex:1;min-height:0">
    <div id="log-toolbar">
      <div class="log-dot"></div>
      <span style="font-size:9px;color:#444;letter-spacing:2px">LIVE</span>
      <input id="log-filter" placeholder="filter..." oninput="filterLogs()">
      <button onclick="clearLogs()" style="background:none;border:1px solid #222;color:#444;font-family:monospace;font-size:9px;padding:3px 8px;border-radius:3px;cursor:pointer;letter-spacing:1px">CLEAR</button>
    </div>
    <div id="log-output"></div>
  </div>
  <iframe id="pi-iframe" src="about:blank"></iframe>
</div>

<script>
const out = document.getElementById('output');
const inp = document.getElementById('cmd');
let currentTab = 'server';
let cmdHistory = [];
let histIdx = -1;

function append(text, cls) {
    const s = document.createElement('div');
    s.className = 'line-' + (cls || 'out');
    s.textContent = text;
    out.appendChild(s);
    out.scrollTop = out.scrollHeight;
}

function switchTab(tab) {
    currentTab = tab;
    ['server','logs','pi'].forEach(t => document.getElementById('tab-'+t)?.classList.toggle('active', t === tab));
    document.getElementById('server-terminal').style.display = tab === 'server' ? 'flex' : 'none';
    document.getElementById('logs-panel').style.display     = tab === 'logs'   ? 'flex' : 'none';
    document.getElementById('log-toolbar').style.display    = tab === 'logs'   ? 'flex' : 'none';
    document.getElementById('log-output').style.display     = tab === 'logs'   ? 'block' : 'none';
    document.getElementById('pi-iframe').style.display      = tab === 'pi'     ? 'block' : 'none';
    if (tab === 'pi')   checkPiStatus();
    if (tab === 'logs') startLogStream();
}

// ── LOG STREAM ──────────────────────────────────
let _logLines = [];
let _logFilter = '';
let _logEs = null;

function logClass(line) {
    if (line.includes('[SPOTIFY]')) return 'log-spotify';
    if (line.includes('[ARCHER]'))  return 'log-archer';
    if (line.includes('[DISPLAY]')) return 'log-display';
    if (line.includes('[AUTH]'))    return 'log-auth';
    if (line.includes('[VOICE]'))   return 'log-voice';
    if (line.includes('[ARDUINO]')) return 'log-arduino';
    if (line.includes('[YOU'))      return 'log-you';
    if (line.toLowerCase().includes('error') || line.includes('Traceback')) return 'log-err';
    return 'log-default';
}

function renderLogs() {
    const el = document.getElementById('log-output');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    const frag = document.createDocumentFragment();
    _logLines.forEach(line => {
        if (_logFilter && !line.toLowerCase().includes(_logFilter)) return;
        const d = document.createElement('div');
        d.className = logClass(line);
        d.textContent = line;
        frag.appendChild(d);
    });
    el.innerHTML = '';
    el.appendChild(frag);
    if (atBottom) el.scrollTop = el.scrollHeight;
}

function appendLog(line) {
    _logLines.push(line);
    if (_logLines.length > 2000) _logLines.shift();
    if (_logFilter && !line.toLowerCase().includes(_logFilter)) return;
    const el = document.getElementById('log-output');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    const d = document.createElement('div');
    d.className = logClass(line);
    d.textContent = line;
    el.appendChild(d);
    if (atBottom) el.scrollTop = el.scrollHeight;
}

function filterLogs() {
    _logFilter = document.getElementById('log-filter').value.toLowerCase();
    renderLogs();
}

function clearLogs() { _logLines = []; document.getElementById('log-output').innerHTML = ''; }

function startLogStream() {
    if (_logEs) return;
    _logEs = new EventSource('/terminal/log_stream');
    _logEs.onmessage = e => {
        const d = JSON.parse(e.data);
        if (d.snapshot) { _logLines = d.snapshot; renderLogs(); }
        else if (d.line) appendLog(d.line);
    };
    _logEs.onerror = () => { _logEs.close(); _logEs = null; setTimeout(startLogStream, 3000); };
}

function checkPiStatus() {
    fetch('/terminal/pi_status')
    .then(r=>r.json()).then(d=>{
        const el = document.getElementById('pi-status');
        if (d.online) {
            el.className = 'pi-status pi-online';
            el.textContent = 'PI ONLINE';
            document.getElementById('pi-iframe').src = d.url || 'about:blank';
        } else {
            el.className = 'pi-status pi-offline';
            el.textContent = 'PI OFFLINE';
            document.getElementById('pi-iframe').src = 'about:blank';
        }
    }).catch(()=>{});
}

function sendCmd() {
    const cmd = inp.value.trim();
    if (!cmd) return;
    cmdHistory.unshift(cmd);
    histIdx = -1;
    append('$ ' + cmd, 'prompt');
    inp.value = '';
    fetch('/terminal/exec', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cmd: cmd})
    })
    .then(r=>r.json())
    .then(d=>{
        if (d.stdout) d.stdout.split('\\n').forEach(l => { if(l) append(l, 'out'); });
        if (d.stderr) d.stderr.split('\\n').forEach(l => { if(l) append(l, 'err'); });
        if (d.error)  append(d.error, 'err');
        append('', 'info');
    })
    .catch(e => append('Connection error', 'err'));
}

inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { sendCmd(); return; }
    if (e.key === 'ArrowUp') {
        histIdx = Math.min(histIdx + 1, cmdHistory.length - 1);
        inp.value = cmdHistory[histIdx] || '';
        e.preventDefault();
    }
    if (e.key === 'ArrowDown') {
        histIdx = Math.max(histIdx - 1, -1);
        inp.value = histIdx >= 0 ? cmdHistory[histIdx] : '';
        e.preventDefault();
    }
});

// startup
append('ARCHER SERVER TERMINAL', 'system');
append('Connected to: ' + location.host, 'info');
append('Tier 1 access granted.', 'success');
append('─────────────────────────────────', 'info');
append('', 'info');

// poll Pi status every 15s
checkPiStatus();
setInterval(checkPiStatus, 15000);

// Keep body height = visual viewport so input stays above keyboard on mobile
function syncViewport() {
  const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.body.style.height = h + 'px';
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', syncViewport);
  window.visualViewport.addEventListener('scroll', syncViewport);
}
window.addEventListener('resize', syncViewport);
syncViewport();
</script>
</body>
</html>"""
    return Response(terminal_html, mimetype='text/html')


# ── TERMINAL EXEC ─────────────────────────────────────────────────────────────

@bp.route('/terminal/exec', methods=['POST'])
@_limiter.limit('15 per minute; 60 per hour')
@csrf_required
def terminal_exec():
    import archer as _a
    allowed, tier = _terminal_access_check(request)
    if not allowed:
        _a.log_security('TERMINAL_ACCESS_DENIED', ip=request.remote_addr, tier=tier)
        return jsonify({'error': 'Access denied — Tier 1 only'})
    data = request.get_json() or {}
    cmd  = data.get('cmd', '').strip()
    if not cmd:
        return jsonify({'stdout': '', 'stderr': ''})
    _a.log_info('TERMINAL_CMD', cmd=cmd[:200], ip=request.remote_addr)
    if cmd.strip() in ('/help', 'help'):
        maint_state = 'ON' if _a.system_health['maintenance'] else 'OFF'
        help_text = (
            "ARCHER SERVER COMMANDS\n"
            "──────────────────────────────────────────\n"
            "Maintenance\n"
            f"  maintenance on           — redirect all visitors to maintenance page (currently {maint_state})\n"
            "  maintenance off          — restore normal access\n"
            "  maintenance status       — show current state\n"
            "\nSystem\n"
            "  ps aux | grep archer     — check if archer.py is running\n"
            "  cat /tmp/ollama.log      — view Ollama logs\n"
            "  free -h                  — memory usage\n"
            "  df -h                    — disk usage\n"
            "  uptime                   — system load\n"
            "\nArcher State\n"
            "  curl -s http://localhost:7860/system_health | python3 -m json.tool\n"
            "  curl -s http://localhost:7860/display_data  | python3 -m json.tool\n"
            "  curl -s http://localhost:7860/build/part/search?q=engine | python3 -m json.tool\n"
            "\nLogs\n"
            "  (Logs tab above streams live server output)\n"
            "\nGPS / Location  (single-line, paste as-is)\n"
            "  curl -s -X POST http://localhost:7860/location/update -H 'Content-Type: application/json' -d '{\"lat\":37.64,\"lon\":-91.53}'\n"
            "\nSecurity\n"
            "  rotate-secrets           — rotate HSM master key, invalidate all sessions\n"
            "\nType any shell command to run it on the server.\n"
            "For pipes or redirects, use: bash -c 'cmd | pipe'\n"
        )
        return jsonify({'stdout': help_text, 'stderr': '', 'returncode': 0})

    cmd_lower = cmd.strip().lower()
    if cmd_lower in ('maintenance on', 'maintenance mode on', 'maint on'):
        _a.system_health['maintenance'] = True
        return jsonify({'stdout': 'MAINTENANCE MODE ON — all visitors redirected to maintenance page.', 'stderr': '', 'returncode': 0})
    if cmd_lower in ('maintenance off', 'maintenance mode off', 'maint off'):
        _a.system_health['maintenance'] = False
        return jsonify({'stdout': 'MAINTENANCE MODE OFF — normal access restored.', 'stderr': '', 'returncode': 0})
    if cmd_lower in ('maintenance status', 'maint status', 'maintenance'):
        state = 'ON' if _a.system_health['maintenance'] else 'OFF'
        return jsonify({'stdout': f'Maintenance mode: {state}', 'stderr': '', 'returncode': 0})
    if cmd_lower in ('rotate-secrets', 'nuke-sessions', 'rotate secrets'):
        try:
            from hsm import rotate_master_key
            new_secret = rotate_master_key()
            import archer_state as _as
            import os as _os
            _os.environ['ARCHER_SECRET'] = new_secret
            _as._ARCHER_SECRET  = new_secret
            _as._csrf_secret    = new_secret.encode()
            _a._revoked_tokens.clear()
            _a._revoked_names.clear()
            return jsonify({'stdout': '[HSM] Master key rotated — all active sessions invalidated. Users must sign in again.', 'stderr': '', 'returncode': 0})
        except Exception as _e:
            return jsonify({'stdout': '', 'stderr': f'rotate-secrets failed: {_e}', 'returncode': 1})
    if _DANGEROUS.search(cmd):
        return jsonify({'error': 'Blocked: command matches a dangerous pattern'})
    try:
        cmd_list = shlex.split(cmd)
    except ValueError as e:
        return jsonify({'error': f'Invalid command syntax: {e}'})
    if not cmd_list:
        return jsonify({'stdout': '', 'stderr': ''})
    try:
        result = subprocess.run(
            cmd_list, shell=False, capture_output=True, text=True, timeout=15,
        )
        return jsonify({'stdout': result.stdout, 'stderr': result.stderr, 'returncode': result.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timed out (15s limit)'})
    except FileNotFoundError:
        return jsonify({'error': f'Command not found: {cmd_list[0]}'})
    except Exception as e:
        return jsonify({'error': str(e)})


# ── LOG STREAM — one-time key gate ───────────────────────────────────────────
# EventSource (SSE) is a GET request in browsers; browsers cannot send custom
# headers on EventSource, so CSRF headers can't protect it.  Instead, the
# client first POSTs (with CSRF) to get a short-lived one-time stream key,
# then opens the SSE connection with ?key=<key>.  Keys expire in 30 seconds.

import threading as _threading

_stream_keys: dict = {}   # key → expiry timestamp
_stream_keys_lock = _threading.Lock()


@bp.route('/terminal/log_stream_key', methods=['POST'])
@csrf_required
def log_stream_key():
    """Issue a 30-second one-time key for opening the SSE log stream."""
    import archer as _a
    allowed, _ = _terminal_access_check(request)
    if not allowed:
        return jsonify({'error': 'Access denied'}), 403
    import secrets as _s
    key = _s.token_hex(24)
    with _stream_keys_lock:
        _stream_keys[key] = time.time() + 30
    return jsonify({'key': key})


# ── LOG STREAM (SSE) ──────────────────────────────────────────────────────────

@bp.route('/terminal/log_stream')
def terminal_log_stream():
    import archer as _a
    # Validate one-time stream key (replaces CSRF — EventSource can't send headers)
    key = request.args.get('key', '')
    now = time.time()
    with _stream_keys_lock:
        # Prune expired keys
        for k in [k for k, exp in list(_stream_keys.items()) if exp < now]:
            del _stream_keys[k]
        if key not in _stream_keys:
            return Response('', status=403)
        del _stream_keys[key]  # one-time use

    allowed, _ = _terminal_access_check(request)
    if not allowed:
        return Response('', status=403)

    def generate():
        with _a._log_lock:
            snapshot = list(_a._log_buffer)
        yield f"data: {json.dumps({'snapshot': snapshot})}\n\n"
        last_len = len(snapshot)
        while True:
            time.sleep(0.4)
            with _a._log_lock:
                current = list(_a._log_buffer)
            cur_len = len(current)
            if cur_len > last_len:
                for line in current[last_len:]:
                    yield f"data: {json.dumps({'line': line})}\n\n"
            elif cur_len < last_len:
                yield f"data: {json.dumps({'snapshot': current})}\n\n"
            last_len = cur_len

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── PI TUNNEL ─────────────────────────────────────────────────────────────────

@bp.route('/terminal/pi_status')
def pi_status():
    return jsonify(pi_tunnel_url)


@bp.route('/terminal/pi_register', methods=['POST'])
def pi_register():
    """Pi calls this on connect to register its tunnel URL."""
    data  = request.get_json() or {}
    token = data.get('token', '')
    if token != _ARCHER_PI_TOKEN:
        return jsonify({'error': 'Invalid token'}), 403
    pi_tunnel_url['url']       = data.get('url')
    pi_tunnel_url['online']    = True
    pi_tunnel_url['last_seen'] = datetime.now().strftime('%I:%M %p')
    print(f"[PI] Connected — tunnel: {pi_tunnel_url['url']}")
    return jsonify({'status': 'registered'})


@bp.route('/terminal/pi_disconnect', methods=['POST'])
def pi_disconnect():
    data  = request.get_json() or {}
    token = data.get('token', '')
    if not _ARCHER_PI_TOKEN or token != _ARCHER_PI_TOKEN:
        return jsonify({'error': 'Invalid token'}), 403
    pi_tunnel_url['online'] = False
    pi_tunnel_url['url']    = None
    print('[PI] Disconnected')
    return jsonify({'status': 'ok'})
