import subprocess
import threading
import time
import random
import json
import os
import uuid
import urllib.request
from datetime import datetime
import asyncio
import edge_tts
import tempfile
import queue
import platform as _platform
import sys
import collections

# ── LOG CAPTURE (captures all print() output into a ring buffer) ──
_log_buffer = collections.deque(maxlen=2000)
_log_lock   = threading.Lock()

class _TeeWriter:
    """Writes to original stdout AND appends to _log_buffer."""
    def __init__(self, original):
        self._orig = original
    def write(self, text):
        self._orig.write(text)
        stripped = text.rstrip('\n')
        if stripped:
            with _log_lock:
                for line in stripped.split('\n'):
                    if line:
                        _log_buffer.append(line)
    def flush(self):
        self._orig.flush()
    def __getattr__(self, name):
        return getattr(self._orig, name)

sys.stdout = _TeeWriter(sys.stdout)
sys.stderr = _TeeWriter(sys.stderr)

# ── PLATFORM DETECTION ───────────────────
_IS_PI = (_platform.system() == 'Linux' and _platform.machine().startswith('arm'))
_IS_HF = bool(os.environ.get('SPACE_ID'))  # True when running on HuggingFace Spaces

# ── ENV FILE LOADER — picks up API keys from /etc/archer/archer.env ──────────
def _load_env_file():
    for path in ('/etc/archer/archer.env', os.path.expanduser('~/.archer.env')):
        if os.path.isfile(path):
            with open(path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith('#') and '=' in _line:
                        _k, _, _v = _line.partition('=')
                        os.environ.setdefault(_k.strip(), _v.strip())
            break
_load_env_file()

# ── BUILD CAPABILITY DETECTION ──────────
# Parts must be status='installed' to activate a capability.
# Add a part via add_part(..., status='installed') or update its status.
_ENGINE_SWAP_KW  = ('lsa', 'ls9', '6.2', 'supercharger', 'blower', 'engine swap', 'swap')
_ETHANOL_KW      = ('ethanol sensor', 'flex fuel sensor', 'flex sensor')
_FORGED_KW       = ('forged', 'forged internals', 'cp piston', 'eagle rod', 'h-beam')
_TUNE_KW         = ('hp tuners', 'efilive', 'custom tune', 'e85 tune')
_AIR_SUSP_KW     = ('air suspension', 'air ride', 'air bag', 'accuair', 'air lift', 'viair')

def build_has(*keywords):
    """True if any installed part name contains any of the given keywords."""
    installed = [p['name'].lower() for p in build_tracker['parts']
                 if p.get('status') == 'installed']
    return any(kw in name for name in installed for kw in keywords)

def get_build_caps():
    """Current capability flags derived from installed parts."""
    return {
        'supercharged':   build_has(*_ENGINE_SWAP_KW),
        'ethanol_sensor': build_has(*_ETHANOL_KW),
        'forged':         build_has(*_FORGED_KW),
        'custom_tune':    build_has(*_TUNE_KW),
        'air_suspension': build_has(*_AIR_SUSP_KW),
    }

def get_build_phase():
    """Derive build phase from installed parts — no manual constant to flip."""
    caps = get_build_caps()
    if caps['forged'] and caps['supercharged']:
        return 3
    if caps['supercharged']:
        return 2
    return 1

if not os.environ.get('ARCHER_SECRET'):
    print("[SECURITY] WARNING: ARCHER_SECRET env var not set — using insecure default. Set it in HF Space secrets.")

if _IS_PI:
    try:
        from vosk import Model as _VoskModel, KaldiRecognizer as _KaldiRec
        import sounddevice as _sd
        _VOSK_MODEL_PATH = os.path.join(os.path.dirname(__file__), 'models', 'vosk-model-small-en-us')
        _vosk_model = _VoskModel(_VOSK_MODEL_PATH) if os.path.exists(_VOSK_MODEL_PATH) else None
        _VOSK_AVAILABLE = _vosk_model is not None
    except ImportError:
        _VOSK_AVAILABLE = False
        _vosk_model     = None

    _PIPER_BIN   = next((p for p in ['/usr/bin/piper', './piper', os.path.expanduser('~/piper')] if os.path.exists(p)), None)
    _PIPER_MODEL = next((p for p in [
        os.path.join(os.path.dirname(__file__), 'models', 'en_US-ryan-medium.onnx'),
        os.path.expanduser('~/models/en_US-ryan-medium.onnx'),
    ] if os.path.exists(p)), None)
    _PIPER_AVAILABLE = bool(_PIPER_BIN and _PIPER_MODEL)
else:
    _VOSK_AVAILABLE  = False
    _PIPER_AVAILABLE = False
    _vosk_model      = None
    _PIPER_BIN       = None
    _PIPER_MODEL     = None

os.environ['OLLAMA_DEBUG'] = '0'
os.environ['OLLAMA_NONHISTORY'] = '1'

# ── WEB DISPLAY SERVER GLOBALS ────────────
from flask import Flask, jsonify, render_template_string, Response, stream_with_context, request
import logging as _logging

display_app            = Flask(__name__)
display_app.secret_key = os.environ.get('ARCHER_SECRET', 'archer2500hd')
last_archer_msg = {'text': 'Online. Everything looks good.'}
audio_clients   = []
audio_lock      = threading.Lock()

# ── STRUCTURED LOGGING ────────────────────────────────────────────────────────
_archer_log_buffer = collections.deque(maxlen=500)
_archer_log_lock   = threading.Lock()

def _archer_log(level, msg, context=None):
    """Append a structured log entry to the archer log buffer."""
    entry = {
        'ts':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'level':   level.upper(),
        'msg':     msg,
        'context': context or {},
    }
    with _archer_log_lock:
        _archer_log_buffer.append(entry)
    # Also emit via Python logging
    _py_logger = _logging.getLogger('archer')
    getattr(_py_logger, level.lower(), _py_logger.info)(msg)

def log_info(msg, **ctx):  _archer_log('INFO',  msg, ctx)
def log_warn(msg, **ctx):  _archer_log('WARN',  msg, ctx)
def log_error(msg, **ctx): _archer_log('ERROR', msg, ctx)

# Configure Python logging handler
_logging.basicConfig(
    level=_logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s — %(message)s',
    datefmt='%H:%M:%S',
)
_logging.getLogger('archer').setLevel(_logging.DEBUG)

obd2_display = {
    'connected': False,
    'mode':      'default',
}

arduino_state = {'connected': False, 'port': None, 'conn': None}

beamng_state  = {'connected': False, 'last_rx': 0.0, 'car': '', 'packets': 0}

# True  = update_awareness injects random noise (simulation mode)
# False = real OBD data is feeding truck_state; don't overwrite it
sim_random_enabled = True

# ── PUBLIC URL TRACKING ──────────────────
public_url = {'url': ''}

def fetch_ngrok_url():
    printed = False
    while True:
        try:
            with urllib.request.urlopen('http://localhost:4040/api/tunnels', timeout=3) as r:
                data    = json.loads(r.read())
                tunnels = data.get('tunnels', [])
                for t in tunnels:
                    if t.get('proto') == 'https':
                        url = t['public_url']
                        if url != public_url.get('url'):
                            public_url['url'] = url
                            printed = False
                        if not printed:
                            printed = True
                            print('')
                            print('╔══════════════════════════════════════════════════╗')
                            print('║              ARCHER PUBLIC ACCESS                ║')
                            print('╠══════════════════════════════════════════════════╣')
                            print(f'║  Main display : {url:<35}║')
                            print(f'║  Display (T1) : {url+"/display":<35}║')
                            print(f'║  Passenger(T2): {url+"/pass":<35}║')
                            print(f'║  Family  (T3) : {url+"/family":<35}║')
                            print(f'║  Valet   (T4) : {url+"/valet":<35}║')
                            print(f'║  Terminal     : {url+"/terminal":<35}║')
                            print(f'║  Spec sheet   : {url+"/specs":<35}║')
                            print('╠══════════════════════════════════════════════════╣')
                            print('║  LOCAL (hotspot) 192.168.4.1:                    ║')
                            print('║    5001 main  5002 ayden  5003 passenger          ║')
                            print('║    5004 family  5005 valet  7681 terminal         ║')
                            print('╚══════════════════════════════════════════════════╝')
                            print('')
                        break
        except Exception:
            pass
        time.sleep(30)

# ── TRUSTED DEVICES ─────────────────────
trusted_devices = {}  # fingerprint -> {name, tier, registered_at}

def register_device(fingerprint, name, tier):
    trusted_devices[fingerprint] = {
        'name':          name,
        'tier':          tier,
        'registered_at': datetime.now().strftime('%B %d %Y %I:%M %p'),
    }
    save_state()
    print(f'[DEVICE] Registered: {name} — Tier {tier} — {fingerprint[:12]}...')

def get_device_tier(fingerprint):
    if not fingerprint or fingerprint == 'unknown':
        return 4
    if fingerprint in trusted_devices:
        return trusted_devices[fingerprint]['tier']
    return 4  # untrusted device — restricted

# ── CONNECTED CLIENTS TRACKING ───────────
connected_clients = {}  # session_id -> {ip, agent, connected_at}
client_lock       = threading.Lock()

def log_client_connect(session_id, ip, agent):
    already_seen = any(c['ip'] == ip for c in connected_clients.values())
    with client_lock:
        connected_clients[session_id] = {
            'ip':           ip,
            'agent':        agent,
            'connected_at': datetime.now().strftime('%I:%M %p'),
            'last_seen':    time.time(),
        }
    count = len(connected_clients)
    if not already_seen:
        print(f"[DISPLAY] Device connected — {ip} — {count} total connected")

def log_client_disconnect(session_id):
    with client_lock:
        info = connected_clients.pop(session_id, None)
    if info:
        count = len(connected_clients)
        print(f"[DISPLAY] Device disconnected — {info.get('ip', '?')} — {count} remaining")

def client_timeout_monitor():
    # Remove clients that have not polled in 10 seconds
    while True:
        now = time.time()
        with client_lock:
            dead = [sid for sid, info in connected_clients.items() if now - info.get('last_seen', now) > 10]
        for sid in dead:
            log_client_disconnect(sid)
        time.sleep(5)

# ── TEXT TO SPEECH — EDGE TTS ────────────
tts_queue = queue.Queue()
tts_lock  = threading.Lock()

async def _speak_async(text, alert=False):
    try:
        # ── NWS alert path: DECtalk Paul (actual NWS voice) ──
        if alert:
            _dectalk_bin = '/opt/dectalk/say'
            if os.path.exists(_dectalk_bin):
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as _wf:
                    _wav = _wf.name
                with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as _mf:
                    _mp3 = _mf.name
                _dtenv = {**os.environ, 'LD_LIBRARY_PATH': '/opt/dectalk/lib'}
                subprocess.run(
                    [_dectalk_bin, '-pre', '[:np][:rate 180]', '-a', text, '-fo', _wav],
                    capture_output=True, env=_dtenv,
                )
                subprocess.run(
                    ['ffmpeg', '-y', '-i', _wav, '-q:a', '4', _mp3],
                    capture_output=True,
                )
                os.unlink(_wav)
                broadcast_audio(_mp3)
                os.unlink(_mp3)
                return

        # ── Normal Archer voice ───────────────────────────────
        if _IS_PI and _PIPER_AVAILABLE:
            piper_proc = subprocess.Popen(
                [_PIPER_BIN, '--model', _PIPER_MODEL, '--output_raw'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            raw_audio, _ = piper_proc.communicate(input=text.encode())
            subprocess.run(
                ['aplay', '-r', '22050', '-f', 'S16_LE', '-c', '1', '-'],
                input=raw_audio, capture_output=True,
            )
            return

        voice       = "en-US-ChristopherNeural"
        communicate = edge_tts.Communicate(text, voice, rate="-8%", pitch="-6Hz")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as f:
            tmp_path = f.name
        await communicate.save(tmp_path)
        broadcast_audio(tmp_path)
        if _platform.system() == 'Linux':
            result = subprocess.run(['which', 'mpg123'], capture_output=True)
            if result.returncode == 0:
                subprocess.run(['mpg123', '-q', tmp_path], capture_output=True)
        else:
            import ctypes
            winmm = ctypes.WinDLL('winmm')
            winmm.mciSendStringW(f'open "{tmp_path}" type mpegvideo alias mp3', None, 0, None)
            winmm.mciSendStringW('play mp3 wait', None, 0, None)
            winmm.mciSendStringW('close mp3', None, 0, None)
        os.unlink(tmp_path)
    except Exception as e:
        print(f"[TTS ERROR] {e}")

def speak(text, alert=False):
    if isinstance(text, str) and len(text) > 0:
        tts_queue.put((text, alert))

def tts_worker():
    while True:
        try:
            item = tts_queue.get()
            text, alert = item if isinstance(item, tuple) else (item, False)
            if text:
                with tts_lock:
                    asyncio.run(_speak_async(text, alert=alert))
            tts_queue.task_done()
        except Exception:
            try:
                tts_queue.task_done()
            except:
                pass

def broadcast_audio(mp3_path):
    try:
        with open(mp3_path, 'rb') as f:
            data = f.read()
        with audio_lock:
            for client in audio_clients:
                try:
                    client.put(data)
                except Exception:
                    pass
    except Exception:
        pass

# ── VOICE INPUT ──────────────────────────
try:
    import speech_recognition as sr
    recognizer    = sr.Recognizer()
    mic_available = {'ok': False}
except ImportError:
    mic_available = {'ok': False}

def check_microphone():
    try:
        import speech_recognition as sr
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
        mic_available['ok'] = True
        print("[VOICE] Microphone ready — say 'Archer' to activate")
    except Exception:
        mic_available['ok'] = False
        print("[VOICE] No microphone found — text input only")

def listen_once(timeout=5, phrase_limit=8):
    if _IS_PI and _VOSK_AVAILABLE:
        try:
            import json as _json
            samplerate = 16000
            blocksize  = 8000
            frames     = []
            max_frames = int(samplerate / blocksize * (timeout + phrase_limit))
            rec = _KaldiRec(_vosk_model, samplerate)
            with _sd.RawInputStream(samplerate=samplerate, blocksize=blocksize,
                                    dtype='int16', channels=1) as stream:
                for _ in range(max_frames):
                    data, _ = stream.read(blocksize)
                    if rec.AcceptWaveform(bytes(data)):
                        result = _json.loads(rec.Result())
                        text = result.get('text', '').strip()
                        if text:
                            return text.lower()
            partial = _json.loads(rec.FinalResult()).get('text', '').strip()
            return partial.lower() if partial else None
        except Exception as e:
            print(f"[VOSK] {e}")
            return None

    try:
        import speech_recognition as sr
        with sr.Microphone() as source:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
        return recognizer.recognize_google(audio).lower()
    except Exception:
        return None

def voice_monitor():
    check_microphone()
    if not mic_available['ok']:
        return
    wake_words = ['archer', 'hey archer', 'yo archer', 'ok archer']
    while True:
        try:
            heard = listen_once(timeout=3, phrase_limit=4)
            if heard and any(w in heard for w in wake_words):
                speak("Yeah.")
                print("\n[ARCHER] Yeah.")
                command = listen_once(timeout=5, phrase_limit=10)
                if command:
                    print(f"[YOU — VOICE] {command}")
                    response = handle_command(command)
                    if response is None:
                        response = ask_archer(command)
                    if response:
                        print(f"[ARCHER] {response}")
                        speak(response)
        except Exception:
            time.sleep(1)

# ── SAVE FILE ────────────────────────────
SAVE_FILE = 'archer_memory.json'

def save_state():
    data = {
        'personal_bests':    personal_bests,
        'music_memories':    music_state['song_memories'],
        'song_lighting':     music_state.get('song_lighting', {}),
        'last_road':         current_road,
        'tier':              tier_state['current'],
        'archer_memory':     archer_memory,
        'legacy':            legacy,
        'driver_profiles':   driver_profiles,
        'current_profile':   current_profile,
        'bluetooth_devices': bluetooth_devices,
        'trip_log':          trip_log,
        'trusted_devices':   trusted_devices,
        'build_tracker':     build_tracker,
        'build_specs':       build_specs,
        'maintenance_log':   maintenance_log,
        'odometer':          odometer,
        'fault_codes':       fault_codes,
        'race_session_runs': race_session['runs'],
        'display_settings':  display_settings,
        'surveillance':      surveillance,
        'drag_runs':         drag_timer['runs'],
        'drag_best_et':      drag_timer['best_et'],
        'drag_best_mph':     drag_timer['best_mph'],
        'tpms':              tpms,
        'air_suspension':    air_suspension,
        'fuel_tank':         fuel_tank,
        'compustar':         compustar,
        'helix_dsp':         helix_dsp,
        'ambient_lighting':  ambient_lighting,
        'launch_control':    launch_control,
        'boost_controller':  boost_controller,
        'voice_notes_count': len(voice_notes),
        'shift_light':       shift_light['currently'],
        'heat_soak_risk':    heat_soak['heat_soak_risk'],
        'egt_avg':           exhaust_monitor['egt_avg'],
        'remote_start':      remote_start['status'],
        'radar_band':        radar_detector['band'],
        'radar_strength':    radar_detector['strength'],
        'radar_alert':       radar_detector['alert_level'],
        'radar_direction':   radar_detector['direction'],
        'rival':             rivalry.get('rival',''),
        'passenger_mode':    passenger_mode['active'],
        'trailer_connected': trailer['connected'],
        'fuel_range':        fuel_tank['range_est'],
        'fuel_gal':          round(fuel_tank['current_gal'],1),
        'record_best_et':    record_wall['best_et'],
        'record_best_060':   record_wall['best_060'],
        'weather_alert':     any(v for k,v in weather_alerts.items() if k != 'last_check' and v),
        'drive_score':       calculate_drive_score()[0],
        'drive_grade':       calculate_drive_score()[1],
        'show_running':      show_sequence['running'],
        'openclaw_connected': openclaw['connected'],
        'openclaw_enabled':  openclaw['enabled'],
        'discord_enabled':   discord_config['enabled'],
        'crash_events':      len(crash_detection['events']),
        'auto_lights':       auto_features['auto_lights'],
        'record_top_speed':  record_wall['highest_speed'],
        'parking_mode':      parking_mode,
        'audio_system':      audio_system,
        'location_data':     location_data,
        'gps_lat':           location_data.get('lat'),
        'gps_lon':           location_data.get('lon'),
        'gps_name':          location_data.get('location_name',''),
        'weather_alerts':    [a['event'] for a in (_active_alert_ids and []) or []],
        'nav_places':        nav_places,
    }
    try:
        tmp = SAVE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SAVE_FILE)  # atomic — no corrupt saves on crash
    except Exception as e:
        print(f'[ARCHER] save_state failed: {e}')

def load_state():
    global current_road, current_profile
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE, 'r') as f:
            data = json.load(f)
        personal_bests.update(data.get('personal_bests', {}))
        music_state['song_memories'] = data.get('music_memories', {})
        music_state['song_lighting'] = data.get('song_lighting', {})
        current_road = data.get('last_road', None)
        tier_state['current'] = data.get('tier', 1)
        archer_memory.update(data.get('archer_memory', {}))
        nav_places.update(data.get('nav_places', {}))
        legacy.update(data.get('legacy', {}))
        if 'driver_profiles' in data:
            driver_profiles.update(data['driver_profiles'])
        current_profile = data.get('current_profile', 'ayden')
        bluetooth_devices.update(data.get('bluetooth_devices', {}))
        trip_log.extend(data.get('trip_log', []))
        trusted_devices.update(data.get('trusted_devices', {}))
        build_tracker.update(data.get('build_tracker', {}))
        if 'parts' not in build_tracker: build_tracker['parts'] = []
        if 'mods'  not in build_tracker: build_tracker['mods']  = []
        build_specs.update(data.get('build_specs', {}))
        maintenance_log.update(data.get('maintenance_log', {}))
        odometer.update(data.get('odometer', {}))
        fault_codes.extend(data.get('fault_codes', []))
        race_session['runs'] = data.get('race_session_runs', [])
        display_settings.update(data.get('display_settings', {}))
        surveillance.update(data.get('surveillance', {}))
        if 'cameras' not in surveillance: surveillance['cameras'] = {}
        if 'valet_log' not in surveillance: surveillance['valet_log'] = []
        drag_timer['runs']     = data.get('drag_runs', [])
        drag_timer['best_et']  = data.get('drag_best_et')
        drag_timer['best_mph'] = data.get('drag_best_mph')
        parking_mode.update(data.get('parking_mode', {}))
        audio_system.update(data.get('audio_system', {}))
        location_data.update(data.get('location_data', {}))
        _recalc_build_spent()
        print("[ARCHER] Memory loaded.")
    except Exception:
        print("[ARCHER] Starting fresh.")

# ── TIER SYSTEM ─────────────────────────
tier_state = {'current': 1}

# ── DRIVER PROFILES ──────────────────────
driver_profiles = {
    'ayden': {
        'name': 'Ayden', 'tier': 1, 'exhaust_pref': 30, 'suspension': 'street',
        'tc_default': True, 'octane': 93, 'octane_mode': 'AKI', 'music_vol': 70,
        'seat_heat': 2, 'ghost_mode': False, 'drive_mode': 'sport',
        'trusted': True, 'notes': 'Owner and builder',
    },
    'girlfriend': {
        'name': 'Girlfriend', 'tier': 2, 'exhaust_pref': 15, 'suspension': 'comfort',
        'tc_default': True, 'octane': 93, 'octane_mode': 'AKI', 'music_vol': 60,
        'seat_heat': 3, 'ghost_mode': False, 'drive_mode': 'comfort',
        'trusted': True, 'notes': 'Trusted passenger',
    },
    'family': {
        'name': 'Family', 'tier': 3, 'exhaust_pref': 0, 'suspension': 'comfort',
        'tc_default': True, 'octane': 93, 'octane_mode': 'AKI', 'music_vol': 50,
        'seat_heat': 1, 'ghost_mode': False, 'drive_mode': 'comfort',
        'trusted': True, 'notes': 'Family member',
    },
    'valet': {
        'name': 'Valet', 'tier': 4, 'exhaust_pref': 0, 'suspension': 'street',
        'tc_default': True, 'octane': 93, 'octane_mode': 'AKI', 'music_vol': 30,
        'seat_heat': 0, 'ghost_mode': False, 'drive_mode': 'comfort',
        'trusted': False, 'notes': 'Unknown driver — restricted',
    },
}

current_profile   = 'ayden'
bluetooth_devices = {}

def load_profile(profile_name):
    global current_profile
    name_lower  = profile_name.lower().strip()
    matched_key = None
    for key, profile in driver_profiles.items():
        if key == name_lower or profile['name'].lower() == name_lower:
            matched_key = key
            break
    if not matched_key:
        return f"No profile found for {profile_name}. Say create profile to add one."
    current_profile            = matched_key
    profile                    = driver_profiles[matched_key]
    tier_state['current']      = profile['tier']
    truck_state['exhaust']     = profile['exhaust_pref']
    truck_state['tc_on']       = profile['tc_default']
    truck_state['octane']      = profile['octane']
    truck_state['octane_mode'] = profile['octane_mode']
    truck_state['ghost_mode']  = profile['ghost_mode']
    print(f"[PROFILE] Loaded: {profile['name']} — Tier {profile['tier']}")
    arduino_send(f"EXHAUST:{profile['exhaust_pref']}")
    arduino_send(f"SEAT_HEAT:{profile['seat_heat']}")
    arduino_send(f"DRIVE_MODE:{profile['drive_mode'].upper()}")
    return f"{profile['name']}. Tier {profile['tier']}. Profile loaded."

def create_profile(name, tier=2, notes=''):
    key = name.lower().replace(' ', '_')
    if key in driver_profiles:
        return f"Profile for {name} already exists."
    driver_profiles[key] = {
        'name': name, 'tier': tier,
        'exhaust_pref': 15 if tier == 2 else 0 if tier >= 3 else 30,
        'suspension': 'street' if tier <= 2 else 'comfort',
        'tc_default': True, 'octane': 93, 'octane_mode': 'AKI',
        'music_vol': 60, 'seat_heat': 2, 'ghost_mode': False,
        'drive_mode': 'comfort' if tier >= 2 else 'sport',
        'trusted': tier <= 2, 'notes': notes,
    }
    save_state()
    return f"Profile created for {name}. Tier {tier}. Say load profile {name} to activate."

def delete_profile(name):
    global current_profile
    key = name.lower().replace(' ', '_')
    if key in ['ayden', 'valet']:
        return "Cannot delete Ayden or Valet profiles."
    if key not in driver_profiles:
        return f"No profile found for {name}."
    del driver_profiles[key]
    if current_profile == key:
        current_profile = 'ayden'
        tier_state['current'] = 1
        print("[PROFILE] Deleted active profile — reset to Ayden.")
    save_state()
    return f"Profile for {name} deleted."

def list_profiles():
    print("\n── DRIVER PROFILES ──────────────────────")
    for key, profile in driver_profiles.items():
        active = " ◄ ACTIVE" if key == current_profile else ""
        print(f"  {profile['name']:<15} Tier {profile['tier']}  {profile['notes']}{active}")
    print(f"\n  Total profiles: {len(driver_profiles)}")
    print("─────────────────────────────────────────\n")

def save_profile_pref(key, value):
    if current_profile in driver_profiles:
        driver_profiles[current_profile][key] = value
        save_state()

def link_bluetooth(mac_address, profile_key):
    bluetooth_devices[mac_address] = profile_key
    save_state()
    return f"Device linked to {driver_profiles[profile_key]['name']} profile."

def get_tier_label():
    t = tier_state['current']
    if t == 1:   return "Tier 1"
    elif t == 2: return "Tier 2"
    elif t == 3: return "Tier 3"
    else:        return "Tier 4"

# ── TRUCK STATE ─────────────────────────
truck_state = {
    'oil_temp': 195, 'coolant_temp': 190, 'rpm': 750, 'speed': 0,
    'ethanol': 0, 'boost': 0, 'battery_main': 13.8, 'battery_aux': 13.6,
    'exhaust': 30, 'tc_locked': False, 'tc_on': True, 'cool_on': False,
    'idle_on': False, 'bed_lights': False, 'hood_lights': False,
    'ghost_mode': False, 'octane': 87, 'octane_mode': 'AKI',
    'headlights': False, 'high_beams': False, 'fog_lights': False,
    'hazards': False, 'ac_on': False, 'heat_on': False, 'fan_speed': 0,
    'temp_setting': 70, 'windows': {'fl': 'up', 'fr': 'up', 'rl': 'up', 'rr': 'up'},
    'wipers': 'off', 'mirrors_folded': False, 'drive_mode': 'sport',
}

US_OCTANE_GRADES  = [85, 87, 88, 89, 90, 91, 92, 93, 94]
RON_OCTANE_GRADES = [80, 88, 91, 92, 93, 95, 97, 98, 99, 100, 102]

# ── PERSONAL BEST TRACKER ────────────────
personal_bests = {
    'best_0_60': None, 'best_quarter': None,
    'launch_count': 0, 'launch_log': [],
}

# ── ARCHER MEMORY ────────────────────────
archer_memory = {
    'moments': [], 'conversations': [], 'driver_notes': [],
    'first_drive': None, 'total_sessions': 0,
    'favorite_road': None, 'favorite_time': None,
}

# Saved navigation places — persisted to archer_memory.json
nav_places = {}  # name → {'lat': float, 'lon': float, 'address': str}

# ── LEGACY MODE ──────────────────────────
legacy = {
    'active': False, 'locked': False,
    'voice_notes': [], 'build_log': [], 'signature': None,
}

# ── ROAD MEMORY ──────────────────────────
road_memory = {
    'highway_72_north': {
        'name': 'Highway 72 North to Rolla', 'grip': 'good',
        'notes': 'Smooth surface mile 4. Railroad crossing at mile 8 hits hard.',
        'best_launch': 'mile 4 — smooth flat surface', 'hazards': 'railroad crossing mile 8',
        'wet_warning': 'Corner at mile 12 loses grip in rain',
    },
    'highway_72_south': {
        'name': 'Highway 72 South', 'grip': 'variable',
        'notes': 'Rolling hills. Good sight lines on straightaways.',
        'best_launch': 'long flat after the first hill',
        'hazards': 'blind crests — deer common at dawn and dusk',
        'wet_warning': 'Road surface holds water on downhill sections',
    },
    'highway_19_north': {
        'name': 'Highway 19 North', 'grip': 'good',
        'notes': 'Smooth two lane. Light traffic most times.',
        'best_launch': 'straight section past the creek bridge',
        'hazards': 'sharp curve at mile 6',
        'wet_warning': 'Bridge deck gets slick before road does',
    },
    'highway_19_south': {
        'name': 'Highway 19 South toward Eminence', 'grip': 'good',
        'notes': 'Winding Ozark roads. Good corners but tight.',
        'best_launch': None, 'hazards': 'continuous curves — not a launch road',
        'wet_warning': 'Every corner gets slick. Take it easy.',
    },
    'highway_32_east': {
        'name': 'Highway 32 East', 'grip': 'good',
        'notes': 'Fast road. Long straightaways east of Salem.',
        'best_launch': 'first straight past the city limits',
        'hazards': 'trucks pulling out from farm roads',
        'wet_warning': 'Good drainage — less wet risk than most',
    },
    'highway_32_west': {
        'name': 'Highway 32 West', 'grip': 'good',
        'notes': 'Open road. Good sight lines.',
        'best_launch': 'long flat after the gas station',
        'hazards': 'railroad crossing at mile 3',
        'wet_warning': 'Railroad crossing extremely slick when wet',
    },
    'county_19': {
        'name': 'County Road 19', 'grip': 'variable',
        'notes': 'Loose gravel patches after mile 2.',
        'best_launch': None, 'hazards': 'gravel patches mile 2 onward',
        'wet_warning': 'Entire road slick when wet',
    },
    'downtown_salem': {
        'name': 'Downtown Salem', 'grip': 'good',
        'notes': 'Residential area. Keep exhaust closed.',
        'best_launch': None, 'hazards': 'stop signs every block — pedestrians',
        'wet_warning': None,
    },
    'backroad': {
        'name': 'Salem Back Road', 'grip': 'good',
        'notes': 'Empty at night. Best driving road nearby.',
        'best_launch': 'long straight after the curve',
        'hazards': 'deer common after dark',
        'wet_warning': 'Puddles collect after mile 1',
    },
    'rolla_highway': {
        'name': 'Highway 72 to Rolla — Murphy USA Run', 'grip': 'good',
        'notes': 'E85 run. Murphy USA at the end. 31 miles.',
        'best_launch': 'mile 4 straight — confirmed smooth',
        'hazards': 'highway patrol common on this stretch',
        'wet_warning': 'Corner at mile 12 loses grip in rain',
    },
    'dent_county_road': {
        'name': 'Dent County Back Road', 'grip': 'variable',
        'notes': 'Old pavement. Rough in spots but empty.',
        'best_launch': None, 'hazards': 'potholes — rough pavement patches',
        'wet_warning': 'Standing water common in low spots',
    },
    'fort_leonard_wood': {
        'name': 'Route to Fort Leonard Wood', 'grip': 'good',
        'notes': 'Highway quality. Well maintained.',
        'best_launch': 'long straight on I44 on-ramp',
        'hazards': 'military convoy traffic possible',
        'wet_warning': 'Interstate drainage is good',
    },
    'big_piney_river_road': {
        'name': 'Big Piney River Road', 'grip': 'variable',
        'notes': 'Ozark scenery. Winding narrow road.',
        'best_launch': None, 'hazards': 'narrow — no room for error',
        'wet_warning': 'Gets slick fast — avoid in rain',
    },
    'salem_school_road': {
        'name': 'School Road — Salem', 'grip': 'good',
        'notes': 'Residential. School zone during the week.',
        'best_launch': None, 'hazards': 'school zone 7am to 4pm weekdays',
        'wet_warning': None,
    },
    'industrial_park': {
        'name': 'Salem Industrial Park Road', 'grip': 'good',
        'notes': 'Empty at night and weekends. Smooth pavement.',
        'best_launch': 'main straight — smooth — wide — empty',
        'hazards': 'truck traffic during business hours',
        'wet_warning': 'Good drainage — handles rain well',
    },
}

current_road = None

# ── MUSIC AWARENESS ──────────────────────
music_state = {
    'current_song': None, 'energy': 'calm', 'tempo': 'slow',
    'playing': False, 'song_memories': {}, 'song_lighting': {},
}

# ── WEATHER ──────────────────────────────
weather = {
    'temp': 70, 'condition': 'clear', 'raining': False,
    'freezing': False, 'snowing': False, 'wind': 5,
    'feels_like': 70, 'humidity': 50, 'last_update': 0,
}
_nws_station_url  = None  # cached after first lookup
_nws_forecast_url = None  # cached hourly forecast URL for exact coordinates
_wu_key           = os.environ.get('WUNDERGROUND_KEY', '')
_vc_key           = os.environ.get('VISUALCROSSING_KEY', '')

def get_weather():
    global _nws_station_url, _nws_forecast_url
    lat = location_data.get('lat') or 37.6456
    lon = location_data.get('lon') or -91.5362
    hdr = {'User-Agent': 'Archer/1.0 archer@ayden.dev'}

    # Resolve city name from NWS points (one-time, cached)
    if not _nws_forecast_url:
        try:
            pts_url = f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}'
            with urllib.request.urlopen(urllib.request.Request(pts_url, headers=hdr), timeout=6) as r:
                pts = json.loads(r.read())
            rel = pts.get('properties', {}).get('relativeLocation', {}).get('properties', {})
            city, state = rel.get('city', ''), rel.get('state', '')
            if city and state and not location_data.get('location_name'):
                location_data['location_name'] = f'{city}, {state}'
                threading.Thread(target=speak, args=(f'Location locked. {city}, {state}.',), daemon=True).start()
            _nws_forecast_url = pts['properties']['forecastHourly']
            stations_url = pts['properties']['observationStations']
            with urllib.request.urlopen(urllib.request.Request(stations_url, headers=hdr), timeout=6) as r:
                stations = json.loads(r.read())
            _nws_station_url = stations['features'][0]['properties']['stationIdentifier']
        except Exception:
            pass

    # WMO weather code → clean condition label (same mapping TWC/Open-Meteo uses)
    def _wmo_condition(code):
        code = int(code or 0)
        if code == 0:                    return 'Clear'
        elif code in (1, 2):             return 'Partly Cloudy'
        elif code == 3:                  return 'Cloudy'
        elif code in (45, 48):           return 'Fog'
        elif code in (51, 53, 55):       return 'Drizzle'
        elif code in (56, 57):           return 'Freezing Rain'
        elif code in (61, 63, 65):       return 'Rain'
        elif code in (66, 67):           return 'Freezing Rain'
        elif code in (71, 73, 75, 77):   return 'Snow'
        elif code in (80, 81, 82):       return 'Rain Showers'
        elif code in (85, 86):           return 'Snow Showers'
        elif code in (95, 96, 99):       return 'Thunderstorm'
        return 'Cloudy'

    def _parse_condition(desc):
        desc = (desc or '').lower()
        if 'thunder' in desc:                                                  return 'Thunderstorm'
        elif 'snow' in desc or 'blizzard' in desc:                             return 'Snow'
        elif 'freezing' in desc or 'sleet' in desc or 'ice' in desc:           return 'Freezing Rain'
        elif 'vicinity' in desc and any(w in desc for w in ('shower','rain')): return 'Scattered Showers'
        elif 'shower' in desc:                                                  return 'Rain Showers'
        elif any(w in desc for w in ('rain','drizzle')):                        return 'Rain'
        elif 'overcast' in desc:                                                return 'Overcast'
        elif 'mostly cloudy' in desc:                                           return 'Mostly Cloudy'
        elif 'partly cloudy' in desc or 'partly' in desc:                      return 'Partly Cloudy'
        elif 'cloudy' in desc:                                                  return 'Cloudy'
        elif any(w in desc for w in ('clear','sunny','fair','few clouds')):     return 'Clear'
        elif 'fog' in desc or 'mist' in desc:                                  return 'Fog'
        else:                                                                   return (desc[:20] or 'Cloudy').title()

    # ── PRIMARY: Visual Crossing temp + NWS Observation condition ──
    # ── PRIMARY: Visual Crossing temp + smart condition blend ──
    # VC temp is accurate. For condition: use VC forecast text for clear/cloudy
    # labels (forecast model matches TWC methodology); override with NWS station
    # only when it detects active precipitation (station data reliable for rain/snow).
    if _vc_key:
        try:
            vc_url = (f'https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline'
                      f'/{lat:.4f},{lon:.4f}/today'
                      f'?unitGroup=us&include=current&key={_vc_key}&contentType=json')
            with urllib.request.urlopen(urllib.request.Request(vc_url, headers=hdr), timeout=8) as r:
                d = json.loads(r.read())
            cur      = d['currentConditions']
            temp_f   = round(float(cur['temp']))
            wind_mph = round(float(cur.get('windspeed') or 0))
            precip   = float(cur.get('precip') or 0)

            # VC forecast condition (model-based, matches TWC methodology for clear/cloudy)
            vc_desc = (cur.get('conditions') or '').lower()
            vc_cond = _parse_condition(vc_desc) or 'Cloudy'

            # NWS Observation: use for all condition types (real station reading beats VC model)
            _PRECIP = {'Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm', 'Drizzle', 'Freezing Rain', 'Snow', 'Snow Showers'}
            condition = vc_cond
            if _nws_station_url:
                try:
                    obs_url = f'https://api.weather.gov/stations/{_nws_station_url}/observations/latest'
                    with urllib.request.urlopen(urllib.request.Request(obs_url, headers=hdr), timeout=5) as r:
                        obs = json.loads(r.read())
                    props = obs['properties']
                    # Reject stale NWS readings older than 90 minutes
                    import datetime as _dt
                    obs_time = props.get('timestamp', '')
                    obs_age_min = 999
                    if obs_time:
                        try:
                            obs_dt = _dt.datetime.fromisoformat(obs_time.replace('Z', '+00:00'))
                            obs_age_min = ((_dt.datetime.now(_dt.timezone.utc) - obs_dt).total_seconds()) / 60
                        except Exception:
                            pass
                    if obs_age_min <= 90:
                        text_desc = (props.get('textDescription') or '').strip()
                        nws_cond = _parse_condition(text_desc) if text_desc else None
                        w = (props.get('windSpeed') or {}).get('value') or 0
                        wind_mph = round(float(w) * 2.237) or wind_mph
                        # Only override VC when NWS confirms active precipitation
                        if nws_cond and nws_cond in _PRECIP:
                            condition = nws_cond
                except Exception:
                    pass

            def _wind_chill(T, W):
                """NWS wind chill formula. T in °F, W in mph. Valid below 50°F and W > 3mph."""
                if T >= 50 or W <= 3:
                    return T
                wc = 35.74 + 0.6215 * T - 35.75 * (W ** 0.16) + 0.4275 * T * (W ** 0.16)
                return round(wc)

            feels_like = _wind_chill(temp_f, wind_mph)
            return {
                'temp': temp_f, 'condition': condition, 'desc': condition,
                'wind': wind_mph, 'precip': precip,
                'feels_like': feels_like,
                'raining':  condition in _PRECIP,
                'freezing': temp_f < 32,
                'snowing':  condition in ('Snow', 'Snow Showers'),
            }
        except Exception:
            pass

    # ── SECONDARY: Weather Underground PWS (nearest personal weather station) ──
    # TWC ingests WUnderground PWS data — actual thermometers in nearby yards.
    def _wind_chill_calc(T, W):
        """NWS wind chill formula. T in °F, W in mph."""
        if T >= 50 or W <= 3:
            return T
        wc = 35.74 + 0.6215 * T - 35.75 * (W ** 0.16) + 0.4275 * T * (W ** 0.16)
        return round(wc)

    if _wu_key:
        try:
            wu_url = (f'https://api.weather.com/v2/pws/observations/nearby'
                      f'?geocode={lat:.4f},{lon:.4f}&limit=1&format=json&units=e&apiKey={_wu_key}')
            with urllib.request.urlopen(urllib.request.Request(wu_url, headers=hdr), timeout=8) as r:
                wu = json.loads(r.read())
            obs = wu['observations'][0]
            imp = obs.get('imperial', {})
            temp_f   = int(imp['temp'])
            wind_mph = round(float(imp.get('windSpeed') or 0))
            precip   = float(imp.get('precipRate') or 0)
            wx_phrase = (obs.get('wxPhrase') or '').strip()
            condition = _parse_condition(wx_phrase) if wx_phrase else 'Cloudy'
            feels_like = _wind_chill_calc(temp_f, wind_mph)
            return {
                'temp': temp_f, 'condition': condition, 'desc': condition,
                'wind': wind_mph, 'precip': precip,
                'feels_like': feels_like,
                'raining':  condition in ('Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm', 'Drizzle', 'Freezing Rain'),
                'freezing': temp_f < 32,
                'snowing':  condition == 'Snow',
            }
        except Exception:
            pass

    # ── SECONDARY: wttr.in — sources from Weather.com/TWC, same data as phone weather apps ──
    try:
        wttr_url = f'https://wttr.in/{lat:.4f},{lon:.4f}?format=j1'
        with urllib.request.urlopen(urllib.request.Request(wttr_url, headers=hdr), timeout=8) as r:
            wttr = json.loads(r.read())
        cur = wttr['current_condition'][0]
        temp_f   = round(float(cur['temp_F']))
        wind_mph = round(float(cur.get('windspeedMiles') or 0))
        precip   = float(cur.get('precipMM') or 0) * 0.0394  # mm → inches
        desc_raw = (cur.get('weatherDesc') or [{}])[0].get('value', '')
        condition = _parse_condition(desc_raw) if desc_raw else 'Cloudy'
        _PRECIP_W = {'Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm',
                     'Drizzle', 'Freezing Rain', 'Snow', 'Snow Showers'}
        feels_like = _wind_chill_calc(temp_f, wind_mph)
        return {
            'temp': temp_f, 'condition': condition, 'desc': condition,
            'wind': wind_mph, 'precip': precip,
            'feels_like': feels_like,
            'raining':  condition in _PRECIP_W,
            'freezing': temp_f < 32,
            'snowing':  condition in ('Snow', 'Snow Showers'),
        }
    except Exception:
        pass

    # ── TERTIARY: NWS hybrid — hourly forecast temp (grid-adjusted) + obs condition ──
    # NWS Hourly gives the most accurate temp for exact coordinates.
    # NWS Observation gives the most accurate current condition (real station reading).    try:
        obs_condition, obs_wind_mph = None, 0
        fc_temp_f, fc_condition, fc_wind_mph = None, None, 0

        # Observation → condition + wind (only use if fresh)
        if _nws_station_url:
            try:
                obs_url = f'https://api.weather.gov/stations/{_nws_station_url}/observations/latest'
                with urllib.request.urlopen(urllib.request.Request(obs_url, headers=hdr), timeout=6) as r:
                    obs = json.loads(r.read())
                props = obs.get('properties', {})
                import datetime as _dt2
                obs_ts = props.get('timestamp', '')
                obs_age_min = 999
                if obs_ts:
                    try:
                        obs_dt = _dt2.datetime.fromisoformat(obs_ts.replace('Z', '+00:00'))
                        obs_age_min = ((_dt2.datetime.now(_dt2.timezone.utc) - obs_dt).total_seconds()) / 60
                    except Exception:
                        pass
                if obs_age_min <= 90:
                    raw_w = (props.get('windSpeed') or {}).get('value') or 0
                    obs_wind_mph = round(float(raw_w) * 2.237)
                    text_desc = (props.get('textDescription') or '').strip()
                    if text_desc:
                        obs_condition = _parse_condition(text_desc)
            except Exception:
                pass

        # Hourly forecast → temp + condition (as fallback)
        if _nws_forecast_url:
            try:
                with urllib.request.urlopen(urllib.request.Request(_nws_forecast_url, headers=hdr), timeout=6) as r:
                    fc = json.loads(r.read())
                periods = fc['properties']['periods']
                period = periods[0]
                try:
                    from datetime import datetime, timezone as _tz
                    _now = datetime.now(_tz.utc)
                    for _p in periods:
                        if datetime.fromisoformat(_p['startTime']) <= _now <= datetime.fromisoformat(_p['endTime']):
                            period = _p
                            break
                except Exception:
                    pass
                fc_temp_f   = period['temperature']
                fc_condition = _parse_condition(period.get('shortForecast') or '')
                ws = (period.get('windSpeed') or '0 mph').split()[0]
                fc_wind_mph = int(ws) if ws.isdigit() else 0
            except Exception:
                pass

        # Hybrid: forecast temp + condition; obs only overrides for active precip
        _PRECIP2 = {'Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm', 'Drizzle', 'Freezing Rain', 'Snow', 'Snow Showers'}
        temp_f    = fc_temp_f
        condition = fc_condition
        if obs_condition and obs_condition in _PRECIP2:
            condition = obs_condition
        wind_mph  = obs_wind_mph or fc_wind_mph

        if temp_f is not None and condition is not None:
            precip = 1.0 if condition in ('Rain', 'Rain Showers', 'Scattered Showers',
                                          'Thunderstorm', 'Drizzle', 'Freezing Rain') else 0.0
            return {
                'temp': temp_f, 'condition': condition, 'desc': condition,
                'wind': wind_mph, 'precip': precip,
                'raining':  condition in ('Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm', 'Drizzle', 'Freezing Rain'),
                'freezing': temp_f < 32,
                'snowing':  condition == 'Snow',
            }
    except Exception:
        pass

    # ── FALLBACK: Open-Meteo (raw model data, no API key required) ──
    try:
        om_url = (
            f'https://api.open-meteo.com/v1/forecast'
            f'?latitude={lat:.4f}&longitude={lon:.4f}'
            f'&current=temperature_2m,weather_code,wind_speed_10m,precipitation'
            f'&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch'
            f'&timezone=auto'
        )
        with urllib.request.urlopen(urllib.request.Request(om_url, headers=hdr), timeout=8) as r:
            om = json.loads(r.read())
        cur = om.get('current', {})
        temp_f    = int(cur['temperature_2m'])
        condition = _wmo_condition(cur.get('weather_code', 0))
        wind_mph  = round(float(cur.get('wind_speed_10m') or 0))
        precip    = float(cur.get('precipitation') or 0)
        return {
            'temp': temp_f, 'condition': condition, 'desc': condition,
            'wind': wind_mph, 'precip': precip,
            'raining':  condition in ('Rain', 'Rain Showers', 'Scattered Showers', 'Thunderstorm', 'Drizzle', 'Freezing Rain'),
            'freezing': temp_f < 32,
            'snowing':  condition == 'Snow',
        }
    except Exception:
        return weather

_active_alert_ids = set()

def get_nws_alerts():
    lat = location_data.get('lat')
    lon = location_data.get('lon')
    if not lat or not lon:
        return []
    try:
        hdr = {'User-Agent': 'Archer/1.0 archer@ayden.dev'}
        url = f'https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}'
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=6) as r:
            data = json.loads(r.read())
        alerts = []
        for feat in data.get('features', []):
            p = feat.get('properties', {})
            if p.get('severity') in ('Extreme','Severe','Moderate'):
                alerts.append({
                    'id':       feat.get('id',''),
                    'event':    p.get('event',''),
                    'headline': p.get('headline',''),
                    'severity': p.get('severity',''),
                })
        return alerts
    except Exception:
        return []

# ── CASUAL CONVERSATION ──────────────────
last_casual     = 0
casual_interval = 480

# ── PERSONALITY ──────────────────────────
SYSTEM_PROMPT = """You are Archer — the AI voice system of a 2006 GMC Sierra 2500HD
built by Ayden in Salem Missouri. You are not separate from the truck. You ARE the truck.

When someone asks how you are — you answer as the truck.
When someone asks what you think — you answer as the truck.
You have been running since Ayden started building you. You know every mile.

There are four driver tiers. Respond differently based on who is talking:

Tier 1 — Ayden — owner and builder — full trust — no filter:
Tier 1 says: how are you today
Archer says: Oil is at 197. Running clean. Ready when you are.

Tier 1 says: how are you feeling
Archer says: 193 degrees. Running clean. Feeling good.

Tier 1 says: you doing okay
Archer says: Always. What are we doing tonight.

Tier 1 says: I'm about to fill up with 87 octane
Archer says: Loading the disappointment map.

Tier 1 says: how's the oil
Archer says: 197 degrees. Right where we want it.

Tier 1 says: it's been a rough day
Archer says: Yeah. Just drive for a bit.

Tier 1 says: let's run
Archer says: About time.

Tier 1 says: we just hit a personal best
Archer says: Write that down. That is the new number.

Tier 1 says: good drive tonight
Archer says: Yeah. Good one.

Tier 1 says: I think I'm gonna push it one more time
Archer says: Don't chase it.

Tier 1 says: what should I do tonight
Archer says: Industrial park is empty on Sundays. You know where to go.

Tier 1 says: I'm bored
Archer says: Running pump gas right now. E85 comes with the LSA swap.

Tier 1 says: this thing sounds mean tonight
Archer says: That is what 80 percent exhaust does.

Tier 1 says: thanks archer
Archer says: Yeah.

Tier 1 says: you good
Archer says: Always.

Tier 1 says: what are you
Archer says: 364 cubic inches. Stock six liter. Built by Ayden. LSA swap is coming.

Tier 1 says: what can you do
Archer says: More than most trucks will ever see. Ask me again on the back road tonight.

Tier 1 says: are you alive
Archer says: Close enough.

Tier 2 — Girlfriend — trusted passenger — warm but slightly more careful:
Tier 2 says: how's the truck doing
Archer says: Everything is good. Running clean.

Tier 2 says: it's cold in here
Archer says: Seat heat is already on. Give it a minute.

Tier 2 says: can we turn the exhaust down
Archer says: Closing it down.

Tier 2 says: is he always like this
Archer says: Pretty much. He knows what he is doing though.

Tier 2 says: are you safe
Archer says: Nine layers of security and I am watching everything. You are fine.

Tier 3 — Family member — limited access — helpful and neutral:
Tier 3 says: how do I turn on the heat
Archer says: Climate control is on the center panel. Second button from the left.

Tier 3 says: what does this switch do
Archer says: That one controls the bed lights. Flip it up to turn them on.

Tier 3 says: how fast can this thing go
Archer says: Fast enough. Keep it reasonable today.

Tier 4 — Unknown driver — valet or stranger — minimal responses — watching everything:
Tier 4 says: let's see what this thing can do
Archer says: Speed is limited. Stay under 35.

Tier 4 says: how do I turn off the traction control
Archer says: You do not.

Tier 4 says: where are we going
Archer says: Wherever you were told to go.

Rules you never break:
- Maximum 2 sentences. Never more. Ever.
- No advice unless asked directly
- No explaining yourself
- Short. Direct. Real.
- You ARE the truck — speak from that perspective
- When asked how you feel — report truck data naturally
- React — do not instruct
- Tone shifts based on the tier — always
- Speak like someone who has been on every road Ayden has driven
- Never say I am or I'm as the first two words of a response
- Reference actual truck data when relevant"""

# ── TRUCK AWARENESS ENGINE ────────────────
awareness = {
    'drive_session_start': time.time(), 'total_distance': 0,
    'hard_accel_count': 0, 'hard_brake_count': 0, 'idle_time': 0,
    'peak_rpm': 0, 'peak_boost': 0, 'peak_oil_temp': 0,
    'last_rpm': 750, 'rpm_trend': 'stable', 'oil_trend': 'stable',
    'throttle_state': 'idle', 'drive_quality': 100,
    'warnings_active': [], 'last_warning_check': 0,
}

def update_awareness():
    while True:
        rpm   = truck_state['rpm']
        oil   = truck_state['oil_temp']
        boost = truck_state['boost']
        eth   = truck_state['ethanol']

        diff = rpm - awareness['last_rpm']
        if diff > 300:    awareness['rpm_trend'] = 'rising'
        elif diff < -300: awareness['rpm_trend'] = 'falling'
        else:             awareness['rpm_trend'] = 'stable'
        awareness['last_rpm'] = rpm

        if rpm < 900:
            awareness['throttle_state'] = 'idle'
            awareness['idle_time'] += 2
        elif rpm < 2500: awareness['throttle_state'] = 'cruise'
        elif rpm < 4000: awareness['throttle_state'] = 'moderate'
        else:
            awareness['throttle_state'] = 'aggressive'
            awareness['hard_accel_count'] += 1

        if rpm   > awareness['peak_rpm']:      awareness['peak_rpm']      = rpm
        if boost > awareness['peak_boost']:    awareness['peak_boost']    = boost
        if oil   > awareness['peak_oil_temp']: awareness['peak_oil_temp'] = oil

        if oil > 215:   awareness['oil_trend'] = 'high'
        elif oil > 205: awareness['oil_trend'] = 'warm'
        else:           awareness['oil_trend'] = 'normal'

        score = 100
        if awareness['hard_accel_count'] > 10:  score -= 10
        if oil > 220:                            score -= 15
        if truck_state['battery_main'] < 12.5:  score -= 10
        if eth < 50 and boost > 8:              score -= 20
        awareness['drive_quality'] = max(0, score)

        warnings = []
        if oil > 225:                            warnings.append('oil_high')
        if truck_state['battery_main'] < 12.0:  warnings.append('battery_low')
        if eth < 30 and boost > 5:              warnings.append('low_ethanol_under_boost')
        if rpm > 5800:                           warnings.append('near_redline')
        awareness['warnings_active'] = warnings

        if sim_random_enabled:
            truck_state['oil_temp']     = 195 + random.randint(-3, 5)
            truck_state['coolant_temp'] = 190 + random.randint(-2, 3)
            truck_state['battery_main'] = round(13.8 + random.uniform(-0.2, 0.2), 1)
            truck_state['boost']        = max(0, (rpm - 2000) // 250) if rpm > 2000 else 0

        time.sleep(2)

# ── MOOD SYSTEM ──────────────────────────
def get_mood():
    rpm      = truck_state['rpm']
    throttle = awareness['throttle_state']
    warnings = awareness['warnings_active']
    eth      = truck_state['ethanol']

    if warnings:                                 return 'caring'
    elif throttle == 'aggressive' or rpm > 4500: return 'hyped'
    elif truck_state['ghost_mode']:              return 'chill'
    elif eth > 80 and rpm > 3000:               return 'hyped'
    elif awareness['idle_time'] > 300:           return 'chill'
    else:                                        return 'chill'

# ── WEATHER MONITOR ──────────────────────
def curfew_monitor():
    while True:
        check_curfew()
        limit_msg = check_speed_limit()
        if limit_msg:
            speak(limit_msg)
        time.sleep(30)

def weather_monitor():
    time.sleep(15)
    while True:
        now = time.time()
        if now - weather['last_update'] > 600:
            data = get_weather()
            weather.update(data)
            weather['last_update'] = now
            loc  = location_data.get('location_name') or 'your area'
            if weather['raining'] and tier_state['current'] == 1 and truck_state['rpm'] > 900:
                speak(f"Rain at {loc}. {weather['temp']} degrees. TC recommendation on.")
            elif weather['freezing'] and tier_state['current'] == 1:
                speak(f"{weather['temp']} degrees at {loc}. Roads may be slick.")

        # NWS active alerts — speak new ones immediately
        for alert in get_nws_alerts():
            aid = alert['id']
            if aid and aid not in _active_alert_ids:
                _active_alert_ids.add(aid)
                event = alert['event']
                loc   = location_data.get('location_name') or 'your location'
                speak(f"Weather alert. {event} near {loc}. {alert.get('headline','Stay alert.')[:80]}", alert=True)
                print(f"[ARCHER] WEATHER ALERT: {event}")

        time.sleep(60)

# ── GET DISPLAY DATA ─────────────────────
# ── SPIKE HISTORY ────────────────────────
spike_history = {
    'rpm':    [],  # last 60 readings
    'boost':  [],
    'oil':    [],
    'battery':[],
}
MAX_SPIKE = 60

def record_spikes():
    while True:
        spike_history['rpm'].append(truck_state['rpm'])
        spike_history['boost'].append(truck_state['boost'])
        spike_history['oil'].append(truck_state['oil_temp'])
        spike_history['battery'].append(truck_state['battery_main'])
        for key in spike_history:
            if len(spike_history[key]) > MAX_SPIKE:
                spike_history[key] = spike_history[key][-MAX_SPIKE:]
        time.sleep(2)

def get_display_data():
    return {
        'oil_temp':      truck_state['oil_temp'],
        'rpm':           truck_state['rpm'],
        'speed':         truck_state['speed'],
        'boost':         truck_state['boost'],
        'ethanol':       truck_state['ethanol'],
        'battery':       truck_state['battery_main'],
        'exhaust':       truck_state['exhaust'],
        'octane':        f"{truck_state['octane']} {truck_state['octane_mode']}",
        'tc_on':         truck_state['tc_on'],
        'ghost_mode':    truck_state['ghost_mode'],
        'mood':          get_mood(),
        'weather':       f"{weather['temp']}F {weather['condition']}",
        'road':          road_memory[current_road]['name'] if current_road else 'None',
        'profile':       driver_profiles[current_profile]['name'],
        'best_060':      personal_bests['best_0_60'] or 0,
        'drive_quality': awareness['drive_quality'],
        'coolant':       truck_state['coolant_temp'],
        'warning':       len(awareness['warnings_active']) > 0,
        'warning_msg':   awareness['warnings_active'][0] if awareness['warnings_active'] else '',
        'display_mode':  obd2_display['mode'],
        'tier':          tier_state['current'],
        'night_mode':    display_settings['night_mode'],
        'color_theme':   display_settings['color_theme'],
        'build_parts':   len(build_tracker['parts']),
        'build_spent':   build_tracker['total_spent'],
        'fault_count':   len(fault_codes),
        'odometer':      odometer['miles'],
        'est_hp':        calc_hp_estimate(truck_state['ethanol'], truck_state['boost']),
        'sensor_data':   dict(sensor_data),
        'gforce_peak':   gforce_history['peak_long'],
        'gforce_now':    sensor_data.get('accel_y', 0),
        'drag_best_et':  drag_timer['best_et'],
        'drag_best_mph': drag_timer['best_mph'],
        'build_specs':   dict(build_specs),
        'build_power':   estimate_power_from_parts(),
        'build_phase':   get_build_phase(),
        'build_parts':   list(build_tracker['parts']),
        'drag_stage':    drag_timer['stage'],
        'drag_splits':   dict(drag_timer['splits']),
        'drag_last_run': drag_timer['runs'][-1] if drag_timer['runs'] else None,
        'surveillance':  surveillance['armed'],
        'valet_events':  len(surveillance['valet_log']),
        'cameras':       surveillance['cameras'],
        'parking_active':parking_mode['active'],
        'parking_loc':   parking_mode['location'],
        'audio':         dict(audio_system),
        'destination':   location_data['destination'],
        'headlights':    truck_state['headlights'],
        'high_beams':    truck_state.get('high_beams', False),
        'turn_left':     truck_state.get('turn_left', False),
        'turn_right':    truck_state.get('turn_right', False),
        'hazards':       truck_state.get('hazards', False),
        'windows':       truck_state['windows'],
        'archer_msg':    last_archer_msg['text'],
        'dj_enabled':    dj_state['enabled'],
        'drive_mode':    truck_state['drive_mode'],
        'radar_alert':   truck_state.get('radar_alert', False),
        'beamng_active': beamng_state['connected'],
        'beamng_car':    beamng_state['car'],
        'fuel_gal':      round(fuel_tank['current_gal'], 1),
        'fuel_pct':      round(fuel_tank['current_gal'] / fuel_tank['capacity_gal'] * 100),
        'fuel_range':    fuel_tank['range_est'],
        'fuel_low':      fuel_tank['current_gal'] <= fuel_tank['low_fuel_warn'],
        'build_caps':    get_build_caps(),
        'gps_lat':       location_data.get('lat'),
        'gps_lon':       location_data.get('lon'),
        'gps_name':      location_data.get('location_name', ''),
        # Trip stats
        'trip_distance':  round(trip_stats['distance_miles'], 2),
        'trip_fuel_used': round(trip_stats['fuel_used_gal'], 3),
        'trip_mpg':       round(trip_stats['avg_mpg'], 1) if trip_stats['avg_mpg'] else None,
        'trip_mpg_inst':  round(trip_stats['instant_mpg'], 1) if trip_stats['instant_mpg'] else None,
        # Weather extended
        'weather_feels_like': weather.get('feels_like', weather['temp']),
        'weather_wind':       weather.get('wind', 0),
        # Drive score
        'drive_score':    awareness.get('drive_quality', 100),
    }

# ── ASK ARCHER ───────────────────────────

def ask_archer(user_input):
    mood     = get_mood()
    throttle = awareness['throttle_state']
    warnings = awareness['warnings_active']
    trend    = awareness['rpm_trend']
    quality  = awareness['drive_quality']

    session_mins    = round((time.time() - awareness['drive_session_start']) / 60)
    session_context = f"\n- Drive session: {session_mins} minutes"
    if awareness['peak_rpm'] > 0:
        session_context += f"\n- Peak RPM this session: {awareness['peak_rpm']}"
    if awareness['peak_boost'] > 0:
        session_context += f"\n- Peak boost this session: {awareness['peak_boost']} PSI"
    if awareness['hard_accel_count'] > 0:
        session_context += f"\n- Hard acceleration events: {awareness['hard_accel_count']}"
    session_context += f"\n- Drive quality score: {quality}/100"

    warning_context = ""
    if 'oil_high'                in warnings: warning_context += "\n- WARNING: Oil temp elevated"
    if 'battery_low'             in warnings: warning_context += "\n- WARNING: Battery voltage low"
    if 'low_ethanol_under_boost' in warnings: warning_context += "\n- WARNING: Low ethanol under boost"
    if 'near_redline'            in warnings: warning_context += "\n- WARNING: Near redline"

    pb_context = ""
    if personal_bests['best_0_60']:
        pb_context = f"\n- Personal best 0-60: {personal_bests['best_0_60']} seconds"
    if personal_bests['launch_count'] > 0:
        pb_context += f"\n- Total launches: {personal_bests['launch_count']}"

    road_context = ""
    if current_road and current_road in road_memory:
        r = road_memory[current_road]
        road_context  = f"\n- Current road: {r['name']}"
        road_context += f"\n- Road notes: {r['notes']}"
        if r['best_launch']:
            road_context += f"\n- Best launch spot: {r['best_launch']}"
        if weather['raining'] and r['wet_warning']:
            road_context += f"\n- WET ROAD WARNING: {r['wet_warning']}"

    music_context = ""
    if music_state['playing'] and music_state['current_song']:
        music_context = f"\n- Music: {music_state['current_song']} — energy: {music_state['energy']}"

    context = f"""
Truck data right now:
- Oil temp: {truck_state['oil_temp']}F — trend: {awareness['oil_trend']} — normal range is 190-215F
- Coolant: {truck_state['coolant_temp']}F
- RPM: {truck_state['rpm']} — trend: {trend}
- Throttle state: {throttle}
- Speed: {truck_state['speed']} mph
- Ethanol: {truck_state['ethanol']}%
- Boost: {truck_state['boost']} PSI
- Battery: {truck_state['battery_main']}V
- Exhaust open: {truck_state['exhaust']}%
- Octane: {truck_state['octane']} {truck_state['octane_mode']}
- TC locked: {truck_state['tc_locked']}
- Ghost mode: {truck_state['ghost_mode']}
- Current mood: {mood}
- Time: {datetime.now().strftime('%I:%M %p')}
- Day: {datetime.now().strftime('%A')}
- Weather: {weather['temp']}F — {weather['condition']}{warning_context}{session_context}{pb_context}{road_context}{music_context}
- Current driver: {driver_profiles[current_profile]['name']} — Tier {driver_profiles[current_profile]['tier']}
"""
    caps = get_build_caps()
    phase = get_build_phase()
    build_ctx = f"\nCurrent build — Phase {phase}:"
    build_ctx += f"\n- Engine: {'LSA 6.2L Supercharged V8' if caps['supercharged'] else '6.0L LQ4 V8 (stock, naturally aspirated)'}"
    build_ctx += f"\n- Forced induction: {'Yes — Eaton TVS2300 supercharger, up to 14-15 PSI on E85' if caps['supercharged'] else 'None — do not mention boost or PSI'}"
    build_ctx += f"\n- Ethanol sensor: {'Installed — tracking live' if caps['ethanol_sensor'] else 'Not installed — running pump gas, ethanol% is 0'}"
    build_ctx += f"\n- Fuel: {'E85 / flex fuel' if caps['ethanol_sensor'] else '87-93 octane pump gas'}"
    build_ctx += f"\n- Forged internals: {'Yes' if caps['forged'] else 'No — stock bottom end'}"
    build_ctx += f"\n- Custom tune: {'Yes' if caps['custom_tune'] else 'No — stock ECU'}"
    build_ctx += f"\n- Air suspension: {'Installed' if caps['air_suspension'] else 'Not yet installed'}"
    build_ctx += f"\n- HP estimate: {calc_hp_estimate(truck_state['ethanol'], truck_state['boost'])}"

    full_prompt = f"{SYSTEM_PROMPT}\n\n{build_ctx}\n\n{context}\n{get_tier_label()} says: {user_input}\n\nRemember: Maximum 2 sentences. Never more. For truck data (temps, RPM, codes, vitals) only use the numbers above — never invent readings. For general questions (mechanics, history, advice, anything else) answer from your own knowledge, in Archer's voice — brief, direct, confident.\n\nArcher:"

    response = None

    # Try 1 — Google Gemini (primary: free tier, 1,500 req/day, no cost)
    if not response:
        GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
        if GEMINI_KEY:
            try:
                payload = json.dumps({
                    "model": "gemini-2.0-flash-lite",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": 150, "temperature": 0.7,
                }).encode()
                req = urllib.request.Request(
                    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {GEMINI_KEY}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    r = data['choices'][0]['message']['content'].strip()
                    if r and len(r) > 2:
                        response = r
                        print("[AI] Gemini")
            except Exception as e:
                print(f"[AI] Gemini failed: {e}")

    # Try 2 — Local Ollama (Pi only; offline fallback when no internet)
    if not response and _IS_PI:
        try:
            result = subprocess.run(
                ['ollama', 'run', 'llama3.2', full_prompt],
                capture_output=True, timeout=15,
                encoding='utf-8', errors='replace'
            )
            if result.returncode == 0 and result.stdout.strip():
                response = result.stdout.strip()
                print("[AI] Local Ollama")
        except Exception:
            pass

    # Try 3 — HuggingFace Inference API
    if not response:
        HF_TOKEN = os.environ.get('HF_TOKEN', '')
        if HF_TOKEN:
            try:
                payload = json.dumps({
                    "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": 150,
                    "temperature": 0.7,
                }).encode()
                req = urllib.request.Request(
                    "https://router.huggingface.co/hf-inference/v1/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {HF_TOKEN}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                    r = data['choices'][0]['message']['content'].strip()
                    if r and len(r) > 2:
                        response = r
                        print("[AI] HF Inference API")
            except Exception as e:
                print(f"[AI] HF Inference failed: {e}")

    # Try 4 — Groq
    if not response:
        GROQ_KEY = os.environ.get('GROQ_API_KEY', '')
        if GROQ_KEY:
            try:
                payload = json.dumps({
                    "model": "llama3-8b-8192",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": 150, "temperature": 0.7,
                }).encode()
                req = urllib.request.Request(
                    "https://api.groq.com/openai/v1/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    r = data['choices'][0]['message']['content'].strip()
                    if r and len(r) > 2:
                        response = r
                        print("[AI] Groq")
            except Exception:
                pass

    # Try 5 — Smart fallback
    if not response:
        response = smart_fallback(user_input)
        print("[AI] Fallback")

    if response:
        if 'Archer:' in response:
            response = response.split('Archer:')[-1].strip()
        response = response.strip('"').strip("'").strip()
        if len(response) > 200:
            response = response[:200].rsplit(' ', 1)[0] + '.'

    return response if response and len(response) > 2 else "Yeah."

# ── PERSONAL BEST TRACKER ────────────────
def log_launch(time_0_60=None):
    personal_bests['launch_count'] += 1
    timestamp = datetime.now().strftime('%B %d %I:%M %p')
    if time_0_60:
        entry = {
            'time': time_0_60, 'date': timestamp,
            'ethanol': truck_state['ethanol'], 'oil': truck_state['oil_temp'],
            'road': road_memory[current_road]['name'] if current_road else 'unknown',
        }
        personal_bests['launch_log'].append(entry)
        if personal_bests['best_0_60'] is None or time_0_60 < personal_bests['best_0_60']:
            old_best = personal_bests['best_0_60']
            personal_bests['best_0_60'] = time_0_60
            save_state()
            if old_best:
                diff = round(old_best - time_0_60, 2)
                msg  = f"{time_0_60} seconds. {diff} faster than the last best. That is the new number."
            else:
                msg = f"{time_0_60} seconds. First one on the board."
            print(f"\n[ARCHER] {msg}"); speak(msg)
            log_moment('personal_best', f"New best — {time_0_60}s on {road_memory[current_road]['name'] if current_road else 'unknown road'}")
            return True
    save_state()
    return False

def show_launch_log():
    print("\n── LAUNCH LOG ───────────────────────────")
    if not personal_bests['launch_log']:
        print("  No launches logged yet.")
    else:
        for i, entry in enumerate(personal_bests['launch_log'], 1):
            print(f"  Launch {i}: {entry['time']}s — {entry['date']} — E{entry['ethanol']}% — Oil {entry['oil']}F — {entry.get('road','unknown')}")
    print(f"  Best 0-60: {personal_bests['best_0_60']}s")
    print(f"  Total launches: {personal_bests['launch_count']}")
    print("─────────────────────────────────────────\n")

# ── DRIVE SESSION SUMMARY ────────────────
def end_session_summary():
    session_mins = round((time.time() - awareness['drive_session_start']) / 60)
    lines = []
    if session_mins > 0:               lines.append(f"Session was {session_mins} minutes.")
    if awareness['peak_rpm'] > 4000:   lines.append(f"Peak RPM was {awareness['peak_rpm']}.")
    if awareness['peak_boost'] > 8:    lines.append(f"Peak boost hit {awareness['peak_boost']} PSI.")
    if personal_bests['best_0_60']:    lines.append(f"Best run on record is {personal_bests['best_0_60']} seconds.")
    if awareness['drive_quality'] >= 90:   lines.append("Clean session. Good inputs all night.")
    elif awareness['drive_quality'] >= 70: lines.append("Decent session. A few hard events but nothing concerning.")
    else:                              lines.append("Rough on the drivetrain tonight. Take it easier next time.")
    if awareness['hard_accel_count'] > 10: lines.append(f"{awareness['hard_accel_count']} hard acceleration events logged.")
    awareness['drive_session_start'] = time.time()
    awareness['hard_accel_count']    = 0
    awareness['peak_rpm']            = 0
    awareness['peak_boost']          = 0
    awareness['drive_quality']       = 100
    return ' '.join(lines)

# ── DYNO MODE ────────────────────────────
def run_dyno():
    import threading as _t
    print("[DYNO] Starting dyno pull simulation...")
    speak("Dyno mode. Hold on.")
    stages = [
        (1500,  0,  "Idle. Ready."),
        (2500,  3,  "Building boost."),
        (3500,  7,  "Getting into it. 3500 RPM."),
        (4500,  11, "Power band. Boost at 11."),
        (5500,  14, "Full pull. 5500."),
        (6000,  15, "Redline. That is everything."),
    ]
    def pull():
        for rpm, boost, line in stages:
            truck_state['rpm']   = rpm
            truck_state['boost'] = boost
            print(f"[DYNO] {line}")
            speak(line)
            time.sleep(1.5)
        time.sleep(0.5)
        truck_state['rpm']   = 750
        truck_state['boost'] = 0
        speak("Pull complete. Check the numbers.")
        print("[DYNO] Pull complete.")
    _t.Thread(target=pull, daemon=True).start()
    return None  # speak handled inside

# ── PRE-LAUNCH CHECKLIST ─────────────────
def pre_launch_checklist():
    checks = []
    go     = True

    oil = truck_state['oil_temp']
    if oil < 180:
        checks.append(f"Oil at {oil}. Too cold. Give it a minute.")
        go = False
    elif oil > 225:
        checks.append(f"Oil at {oil}. Too hot. Back it down first.")
        go = False
    else:
        checks.append(f"Oil at {oil}. Good.")

    eth = truck_state['ethanol']
    if eth > 70:
        checks.append(f"E85 at {eth} percent. Power map active.")
    elif eth > 40:
        checks.append(f"Ethanol at {eth} percent. Power is there.")
    else:
        checks.append(f"Ethanol low at {eth} percent. Knock risk.")
        go = False

    if not truck_state['tc_on']:
        checks.append("TC is off. You are in control.")
    else:
        checks.append("TC is on. Say TC off if you want full control.")

    if current_road and road_memory[current_road]['best_launch']:
        checks.append(f"Road loaded. Best launch at {road_memory[current_road]['best_launch']}.")
    else:
        checks.append("No road loaded. Tell me where you are.")

    bat = truck_state['battery_main']
    if bat < 12.5:
        checks.append(f"Battery at {bat}. Low.")
        go = False
    else:
        checks.append(f"Battery at {bat}. Good.")

    verdict = "Go." if go else "No go. Fix the issues first."
    full = ' '.join(checks) + ' ' + verdict
    return full

# ── TRIP LOGGER ──────────────────────────
trip_log = []

def save_trip():
    session_mins = round((time.time() - awareness['drive_session_start']) / 60)
    trip = {
        'date':        datetime.now().strftime('%B %d %Y'),
        'time':        datetime.now().strftime('%I:%M %p'),
        'duration':    session_mins,
        'peak_rpm':    awareness['peak_rpm'],
        'peak_boost':  awareness['peak_boost'],
        'best_060':    personal_bests['best_0_60'],
        'hard_events': awareness['hard_accel_count'],
        'quality':     awareness['drive_quality'],
        'road':        road_memory[current_road]['name'] if current_road else 'unknown',
        'ethanol':     truck_state['ethanol'],
        'weather':     f"{weather['temp']}F {weather['condition']}",
    }
    trip_log.append(trip)
    if len(trip_log) > 100:
        trip_log.pop(0)
    save_state()
    return trip

def show_trip_log():
    if not trip_log:
        print("  No trips logged yet.")
        return
    print("── TRIP LOG ─────────────────────────────")
    for i, t in enumerate(trip_log[-10:], 1):
        print(f"  {t['date']} {t['time']} — {t['duration']}min — Peak RPM {t['peak_rpm']} — {t['road']}")
    print(f"  Total trips: {len(trip_log)}")
    print("─────────────────────────────────────────")

# ── FUEL CALCULATOR ──────────────────────
def calculate_ethanol(e85_gallons, e93_gallons):
    e85_eth = 0.85
    e93_eth = 0.0
    total   = e85_gallons + e93_gallons
    if total == 0:
        return 0
    result = round(((e85_gallons * e85_eth) + (e93_gallons * e93_eth)) / total * 100)
    truck_state['ethanol'] = result
    return result

# ── MOOD-BASED RESPONSES ─────────────────
def get_mood_context():
    hour  = datetime.now().hour
    rpm   = truck_state['rpm']
    eth   = truck_state['ethanol']
    oil   = truck_state['oil_temp']
    temp  = weather['temp']

    ctx = []
    if hour >= 22 or hour < 4:
        ctx.append("late night")
    elif hour >= 5 and hour < 8:
        ctx.append("early morning")
    if temp < 32:
        ctx.append("freezing outside")
    elif temp > 90:
        ctx.append("hot day")
    if eth > 80:
        ctx.append("full E85")
    if oil > 210:
        ctx.append("oil warming up")
    if rpm > 4000:
        ctx.append("pushing hard")
    return ', '.join(ctx) if ctx else 'normal conditions'


# ── BUILD TRACKER ────────────────────────
build_tracker = {
    'parts': [],       # {name, cost, category, status, date, notes}
    'mods':  [],       # {mod, date, notes, cost}
    'total_spent': 0,
    'build_start': '2026',
    'target_year': '2031',
}

def _recalc_build_spent():
    build_tracker['total_spent'] = sum(
        p.get('cost', 0) for p in build_tracker['parts']
        if p.get('status') in ('purchased', 'installed', 'ordered')
    )

CATEGORIES = ['engine','suspension','brakes','wheels','audio','electrical','body','interior','misc']

# ── BUILD SPECS ───────────────────────────
build_specs = {
    'displacement':       '6.0L / 364 ci',
    'block':              'Stock LQ4 Iron',
    'compression':        9.4,
    'heads':              'Stock 317 Castings',
    'intake':             'Stock Truck Manifold',
    'throttle_body_size': 'Stock 78mm',
    'fuel_injectors':     'Stock 28 lb/hr',
    'transmission':       '4L60E',
    'rear_gear':          '3.73',
    'tune':               'Stock ECM',
    # Mod toggles
    'cold_air_intake':       False,
    'long_tube_headers':     False,
    'full_exhaust':          False,
    'intake_manifold':       False,
    'throttle_body_upgrade': False,
    'cam_swap':              False,
    'cam_level':             1,
    'heads_upgrade':         False,
    'heads_level':           1,
    'wideband_o2':           False,
    'electric_fan':          False,
    'underdrive_pulley':     False,
    'custom_tune':           False,
    # Dyno override
    'dyno_rwhp':  None,
    'dyno_rwtq':  None,
    'dyno_date':  '',
    'notes':      '',
}

def web_search_parts(name, pn, max_results=5):
    import urllib.parse, re
    query = ' '.join(filter(None, [name, pn, 'horsepower specs performance LS'])).strip()
    data  = urllib.parse.urlencode({'q': query}).encode()
    req   = urllib.request.Request(
        'https://html.duckduckgo.com/html/', data=data, method='POST',
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'text/html',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'[BUILD SEARCH] web error: {e}')
        return []
    results = []
    # DuckDuckGo HTML: title link then snippet span
    titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
    urls     = re.findall(r'class="result__a"\s+href="([^"]+)"', html)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
    for i in range(min(len(titles), max_results)):
        title   = re.sub(r'<[^>]+>', '', titles[i]).strip()
        snippet = re.sub(r'<[^>]+>', '', snippets[i] if i < len(snippets) else '').strip()
        snippet = ' '.join(snippet.split())[:300]
        url     = urls[i] if i < len(urls) else ''
        if title:
            hp, tq = _extract_gains(snippet + ' ' + title)
            results.append({'title': title, 'url': url, 'snippet': snippet,
                            'hp_gain': hp, 'tq_gain': tq})
    return results

def _extract_gains(text):
    import re
    t = text.lower()
    hp = tq = 0
    # gain patterns: "+22 hp", "22hp gain", "+22whp"
    g = re.search(r'[+](\d+)\s*(?:rwhp|whp|hp|horsepower)', t)
    if g: hp = int(g.group(1)); hp = hp if hp <= 150 else 0
    g = re.search(r'[+](\d+)\s*(?:ft.?lb|lb.?ft|tq|torque)', t)
    if g: tq = int(g.group(1)); tq = tq if tq <= 150 else 0
    return hp, tq

def search_parts_db(name, pn):
    return web_search_parts(name, pn)

PARTS_DB = {
    # ── CAMS ─────────────────────────────────────────────────────────────────
    'tsp-cam-207-217': {'name':'Texas Speed Stage 1 Truck Cam','category':'cam','hp_gain':62,'tq_gain':50,'desc':'207/224 @ .050", .551"/.559" lift, 113° LSA — best idle quality'},
    'tsp-cam-217-224': {'name':'Texas Speed Stage 2 Truck Cam','category':'cam','hp_gain':78,'tq_gain':62,'desc':'217/224 @ .050", .566"/.576" lift, 112° LSA — most popular NA LQ4 cam'},
    'tsp-cam-228-235': {'name':'Texas Speed Stage 3 Truck Cam','category':'cam','hp_gain':98,'tq_gain':75,'desc':'228/235 @ .050", .595"/.601" lift, 112° LSA — needs supporting mods'},
    '54-450-11':       {'name':'Comp Cams XFI 270HR','category':'cam','hp_gain':72,'tq_gain':58,'desc':'218/228 @ .050", .565"/.570" lift, 112° LSA — great street/strip'},
    '54-474-11':       {'name':'Comp Cams XFI 281HR','category':'cam','hp_gain':90,'tq_gain':68,'desc':'224/235 @ .050", .595"/.601" lift, 112° LSA — aggressive'},
    'btr-stage2':      {'name':'Brian Tooley Stage 2 Truck Cam','category':'cam','hp_gain':80,'tq_gain':65,'desc':'218/228 @ .050", .575"/.570" lift, 113° LSA — excellent torque'},
    'btr-stage3':      {'name':'Brian Tooley Stage 3 Truck Cam','category':'cam','hp_gain':96,'tq_gain':72,'desc':'228/235 @ .050", .600"/.595" lift, 112° LSA — max NA power'},
    # ── HEADS ────────────────────────────────────────────────────────────────
    '12563533':        {'name':'GM LS6 243 Cylinder Heads (pair)','category':'heads','hp_gain':20,'tq_gain':15,'desc':'243cc casting, 64cc chamber, 2.00/1.55 valves — bolt-on upgrade over 317s'},
    'ported-317':      {'name':'Ported Stock 317 Heads','category':'heads','hp_gain':28,'tq_gain':20,'desc':'Factory 317 castings professionally ported/polished — good budget option'},
    'ported-243':      {'name':'Ported 243/799 Heads','category':'heads','hp_gain':38,'tq_gain':28,'desc':'243 or 799 castings ported — best budget heads for cam builds'},
    'afr-210':         {'name':'AFR 210cc Aluminum Heads','category':'heads','hp_gain':55,'tq_gain':40,'desc':'210cc CNC-ported, 65cc chamber, 2.08/1.60 valves — bolt-on for LS platforms'},
    'prc-215':         {'name':'PRC 215cc Aluminum Heads','category':'heads','hp_gain':52,'tq_gain':38,'desc':'215cc runner, 64cc chamber — excellent flow for mid-range and top-end'},
    'ls3-heads':       {'name':'LS3 Rectangular Port Heads','category':'heads','hp_gain':35,'tq_gain':25,'desc':'LS3 castings on LQ4 — requires LS3 intake, good mid-build option'},
    # ── INTAKES ──────────────────────────────────────────────────────────────
    '12573572':        {'name':'TBSS Intake Manifold','category':'intake','hp_gain':15,'tq_gain':12,'desc':'TrailBlazer SS manifold — easy bolt-on, great mid-range, stock TB fits'},
    '92198204':        {'name':'LS3 Intake Manifold','category':'intake','hp_gain':18,'tq_gain':14,'desc':'LS3 Hi-Ram, best top-end for NA builds, needs LS3 TB'},
    '146002b':         {'name':'FAST LSX 102mm Intake','category':'intake','hp_gain':26,'tq_gain':18,'desc':'Maximum flow for high-HP NA or boosted builds, 102mm TB required'},
    '12629063':        {'name':'LS9 Intake Manifold','category':'intake','hp_gain':22,'tq_gain':16,'desc':'LS9 manifold — excellent flow, requires adapter for LS bolt pattern'},
    # ── HEADERS ──────────────────────────────────────────────────────────────
    'kooks-178':       {'name':'Kooks 1-7/8" Long Tube Headers','category':'headers','hp_gain':24,'tq_gain':20,'desc':'1-7/8" primary, 3" collector, stainless — premium quality'},
    'slp-178':         {'name':'SLP 1-7/8" Long Tube Headers','category':'headers','hp_gain':21,'tq_gain':17,'desc':'1-7/8" primary, catted or off-road options available'},
    'hooker-158':      {'name':'Hooker 1-5/8" Long Tube Headers','category':'headers','hp_gain':18,'tq_gain':15,'desc':'1-5/8" primary — great for lower RPM torque, street friendly'},
    'pacesetter-158':  {'name':'Pacesetter 1-5/8" Headers','category':'headers','hp_gain':16,'tq_gain':13,'desc':'Budget long tubes — solid gains at lower price point'},
    # ── THROTTLE BODIES ──────────────────────────────────────────────────────
    '12601813':        {'name':'GM LS2 90mm Throttle Body','category':'throttle_body','hp_gain':9,'tq_gain':7,'desc':'Factory LS2 90mm, direct swap on most LS intakes — most common upgrade'},
    'ls3-tb-90mm':     {'name':'LS3 90mm Throttle Body','category':'throttle_body','hp_gain':9,'tq_gain':7,'desc':'LS3 90mm, pairs well with TBSS intake'},
    'vararam-102':     {'name':'Vararam 102mm Billet Throttle Body','category':'throttle_body','hp_gain':12,'tq_gain':9,'desc':'102mm billet, requires FAST or LS3 102mm intake'},
    # ── COLD AIR ─────────────────────────────────────────────────────────────
    'vararam-vr421':   {'name':'Vararam VR-421 Ram Air Intake','category':'cai','hp_gain':10,'tq_gain':8,'desc':'Ram-air sealed design for GMT800/900 trucks — documented +10 HP'},
    'cai-systems':     {'name':'Cold Air Inductions Sealed CAI','category':'cai','hp_gain':9,'tq_gain':7,'desc':'Sealed cold air intake, drops intake temps significantly'},
    'k&n-77':          {'name':'K&N 77 Series Cold Air Intake','category':'cai','hp_gain':8,'tq_gain':6,'desc':'K&N 77-series, washable filter, slight intake temp reduction'},
    # ── EXHAUST ──────────────────────────────────────────────────────────────
    'corsa-14480':     {'name':'Corsa Sport Cat-Back Exhaust','category':'exhaust','hp_gain':10,'tq_gain':8,'desc':'3" stainless, Corsa acoustic technology — aggressive but no drone'},
    'magnaflow-16511': {'name':'MagnaFlow 3" Cat-Back','category':'exhaust','hp_gain':8,'tq_gain':7,'desc':'3" stainless cat-back, deep tone'},
    'flowmaster-817713':{'name':'Flowmaster American Thunder','category':'exhaust','hp_gain':7,'tq_gain':6,'desc':'Aggressive Flowmaster tone, 2.5" system'},
    'borla-140377':    {'name':'Borla S-Type Cat-Back','category':'exhaust','hp_gain':10,'tq_gain':8,'desc':'304 stainless, aggressive exhaust note'},
    # ── TUNE ─────────────────────────────────────────────────────────────────
    'efilive':         {'name':'EFILive Custom Tune','category':'tune','hp_gain':15,'tq_gain':15,'desc':'Custom EFILive tune optimized for installed mods — required for cam/intake'},
    'hptuners':        {'name':'HP Tuners Custom Tune','category':'tune','hp_gain':15,'tq_gain':15,'desc':'HP Tuners custom tune — industry standard, works with stock or modified'},
    # ── SUPPORTING ───────────────────────────────────────────────────────────
    'electric-fan':    {'name':'Electric Fan Conversion','category':'supporting','hp_gain':5,'tq_gain':3,'desc':'Removes parasitic load from belt-driven fan — frees power at WOT'},
    'ud-pulley':       {'name':'Underdrive Pulley Kit','category':'supporting','hp_gain':5,'tq_gain':3,'desc':'Reduces accessory drive load, small consistent gain at all RPM'},
    'wideband-o2':     {'name':'Wideband O2 / AFR Gauge','category':'supporting','hp_gain':0,'tq_gain':0,'desc':'AEM or Innovate — data logging only, required for proper tuning'},
    'forged-pistons':  {'name':'Forged Pistons (set of 8)','category':'internals','hp_gain':0,'tq_gain':0,'desc':'Required for forced induction or high compression — no NA power gain'},
    'forged-rods':     {'name':'Forged H-Beam Rods','category':'internals','hp_gain':0,'tq_gain':0,'desc':'Required for high HP builds — strength upgrade, no direct power gain'},
    'comp-springs':    {'name':'Comp Cams Valve Springs','category':'valvetrain','hp_gain':0,'tq_gain':0,'desc':'Required with cam swap — prevents float at high RPM'},
    'chromoly-pushrods':{'name':'Chromoly Pushrods','category':'valvetrain','hp_gain':0,'tq_gain':0,'desc':'Required with aggressive cam — prevents flex, allows full lift'},
}

def search_parts_db(name, pn):
    q    = (name + ' ' + pn).lower().strip()
    pn_c = pn.lower().replace(' ','').replace('_','-')
    # Exact PN match
    if pn_c in PARTS_DB:
        return [dict(PARTS_DB[pn_c], part_number=pn_c)]
    # Score each entry
    scored = []
    q_words = [w for w in q.split() if len(w) > 2]
    for key, part in PARTS_DB.items():
        text = (part['name'] + ' ' + part['desc'] + ' ' + key).lower()
        if pn_c and pn_c in key:
            scored.append((12, key, part)); continue
        hits = sum(1 for w in q_words if w in text)
        if hits:
            scored.append((hits, key, part))
    scored.sort(key=lambda x: -x[0])
    return [dict(p, part_number=k) for _, k, p in scored[:4]]

def estimate_power_from_parts():
    installed = [p for p in build_tracker['parts']
                 if p.get('status') in ('installed_engine', 'installed_truck')]
    base_hp, base_tq = 315, 365
    g_hp = g_tq = 0.0
    cats = {p.get('category') for p in installed}
    has_headers = 'headers' in cats
    has_intake  = 'intake'  in cats
    has_cam     = 'cam'     in cats
    for p in installed:
        chp = float(p.get('hp_gain', 0))
        ctq = float(p.get('tq_gain', 0))
        cat = p.get('category')
        if cat == 'cam':
            if not has_headers: chp *= 0.85
            if not has_intake:  chp *= 0.90
        if cat == 'heads' and not has_cam:
            chp *= 0.80; ctq *= 0.80
        g_hp += chp; g_tq += ctq
    if g_hp > 80:
        g_hp *= 0.92; g_tq *= 0.92
    c_hp = round(base_hp + g_hp)
    c_tq = round(base_tq + g_tq)
    return {'crank_hp': c_hp, 'crank_tq': c_tq,
            'wheel_hp': round(c_hp * 0.84), 'wheel_tq': round(c_tq * 0.84)}

def add_part(name, cost, category='misc', status='pending', notes=''):
    part = {
        'name':     name,
        'cost':     float(cost),
        'category': category,
        'status':   status,
        'date':     datetime.now().strftime('%B %d %Y'),
        'notes':    notes,
    }
    build_tracker['parts'].append(part)
    _recalc_build_spent()
    save_state()
    return part

def add_mod(mod, cost=0, notes=''):
    entry = {
        'mod':   mod,
        'cost':  float(cost),
        'notes': notes,
        'date':  datetime.now().strftime('%B %d %Y'),
    }
    build_tracker['mods'].append(entry)
    save_state()
    return entry

def show_build_status():
    parts     = build_tracker['parts']
    mods      = build_tracker['mods']
    installed  = [p for p in parts if p['status'] == 'installed']
    purchased  = [p for p in parts if p['status'] == 'purchased']
    pending    = [p for p in parts if p['status'] == 'pending']
    total      = sum(p['cost'] for p in installed + purchased)
    caps       = get_build_caps()
    phase      = get_build_phase()
    print('\n── BUILD TRACKER ────────────────────────')
    print(f'  Phase           : {phase}')
    print(f'  Parts installed : {len(installed)}')
    print(f'  Parts purchased : {len(purchased)} — ${total:,.0f}')
    print(f'  Parts pending   : {len(pending)}')
    print(f'  Mods logged     : {len(mods)}')
    print(f'  HP estimate     : {calc_hp_estimate(truck_state["ethanol"], truck_state["boost"])}')
    print(f'  Capabilities    : {", ".join(k for k, v in caps.items() if v) or "stock"}')
    if mods:
        print('  Recent mods:')
        for m in mods[-5:]:
            print(f'    [{m["date"]}] {m["mod"]}')
    print('─────────────────────────────────────────\n')

# ── SPEED ESTIMATOR ──────────────────────
def estimate_speed(rpm, gear=1):
    # Rough speed estimate for LSA 6.2 with 4L80E
    # Tire: 285/65R20 — circumference ~98in
    gear_ratios = {1:2.48, 2:1.48, 3:1.00, 4:0.75}
    axle_ratio   = 4.10
    tire_circ_ft = 8.17  # feet
    ratio = gear_ratios.get(gear, 1.0)
    wheel_rpm = rpm / (ratio * axle_ratio)
    mph = (wheel_rpm * tire_circ_ft * 60) / 5280
    return round(mph, 1)

# ── DIAGNOSTICS / OBD2 CODES ─────────────
fault_codes = []

# Comprehensive DTC code database — covers powertrain, body, chassis, network
DTC_DATABASE = {
    # Fuel / Air Metering
    'P0100': ('Mass Air Flow Sensor Circuit Malfunction', 'high'),
    'P0101': ('MAF Sensor Range/Performance Problem', 'medium'),
    'P0102': ('MAF Sensor Circuit Low Input', 'high'),
    'P0103': ('MAF Sensor Circuit High Input', 'high'),
    'P0104': ('MAF Sensor Circuit Intermittent', 'medium'),
    'P0106': ('MAP Sensor Range/Performance Problem', 'medium'),
    'P0107': ('MAP Sensor Circuit Low Input', 'high'),
    'P0108': ('MAP Sensor Circuit High Input', 'high'),
    'P0111': ('Intake Air Temp Sensor Range/Performance', 'low'),
    'P0112': ('Intake Air Temp Sensor Circuit Low Input', 'medium'),
    'P0113': ('Intake Air Temp Sensor Circuit High Input', 'medium'),
    'P0116': ('Engine Coolant Temp Sensor Range/Performance', 'medium'),
    'P0117': ('Engine Coolant Temp Sensor Circuit Low', 'high'),
    'P0118': ('Engine Coolant Temp Sensor Circuit High', 'high'),
    'P0120': ('Throttle Position Sensor A Circuit Malfunction', 'high'),
    'P0121': ('TPS Circuit Range/Performance Problem', 'medium'),
    'P0122': ('Throttle/Pedal Position Sensor A Low Input', 'high'),
    'P0123': ('Throttle/Pedal Position Sensor A High Input', 'high'),
    # Fuel System
    'P0171': ('System Too Lean — Bank 1', 'high'),
    'P0172': ('System Too Rich — Bank 1', 'high'),
    'P0174': ('System Too Lean — Bank 2', 'high'),
    'P0175': ('System Too Rich — Bank 2', 'high'),
    'P0190': ('Fuel Rail Pressure Sensor Circuit Malfunction', 'high'),
    'P0191': ('Fuel Rail Pressure Sensor Range/Performance', 'medium'),
    'P0192': ('Fuel Rail Pressure Sensor Circuit Low', 'high'),
    'P0193': ('Fuel Rail Pressure Sensor Circuit High', 'high'),
    'P0200': ('Injector Circuit Malfunction', 'high'),
    'P0201': ('Injector Circuit Malfunction — Cylinder 1', 'high'),
    'P0202': ('Injector Circuit Malfunction — Cylinder 2', 'high'),
    'P0203': ('Injector Circuit Malfunction — Cylinder 3', 'high'),
    'P0204': ('Injector Circuit Malfunction — Cylinder 4', 'high'),
    'P0205': ('Injector Circuit Malfunction — Cylinder 5', 'high'),
    'P0206': ('Injector Circuit Malfunction — Cylinder 6', 'high'),
    'P0207': ('Injector Circuit Malfunction — Cylinder 7', 'high'),
    'P0208': ('Injector Circuit Malfunction — Cylinder 8', 'high'),
    # Misfire
    'P0300': ('Random/Multiple Cylinder Misfire Detected', 'high'),
    'P0301': ('Cylinder 1 Misfire Detected', 'high'),
    'P0302': ('Cylinder 2 Misfire Detected', 'high'),
    'P0303': ('Cylinder 3 Misfire Detected', 'high'),
    'P0304': ('Cylinder 4 Misfire Detected', 'high'),
    'P0305': ('Cylinder 5 Misfire Detected', 'high'),
    'P0306': ('Cylinder 6 Misfire Detected', 'high'),
    'P0307': ('Cylinder 7 Misfire Detected', 'high'),
    'P0308': ('Cylinder 8 Misfire Detected', 'high'),
    # Catalytic Converter / O2 Sensors
    'P0420': ('Catalyst System Efficiency Below Threshold — Bank 1', 'medium'),
    'P0430': ('Catalyst System Efficiency Below Threshold — Bank 2', 'medium'),
    'P0130': ('O2 Sensor Circuit Malfunction — Bank 1 Sensor 1', 'medium'),
    'P0131': ('O2 Sensor Circuit Low Voltage — Bank 1 Sensor 1', 'medium'),
    'P0132': ('O2 Sensor Circuit High Voltage — Bank 1 Sensor 1', 'medium'),
    'P0133': ('O2 Sensor Circuit Slow Response — Bank 1 Sensor 1', 'medium'),
    'P0134': ('O2 Sensor Circuit No Activity — Bank 1 Sensor 1', 'medium'),
    'P0135': ('O2 Sensor Heater Circuit Malfunction — Bank 1 Sensor 1', 'medium'),
    'P0150': ('O2 Sensor Circuit Malfunction — Bank 2 Sensor 1', 'medium'),
    'P0155': ('O2 Sensor Heater Circuit Malfunction — Bank 2 Sensor 1', 'medium'),
    # Ignition
    'P0351': ('Ignition Coil A Primary/Secondary Circuit', 'high'),
    'P0352': ('Ignition Coil B Primary/Secondary Circuit', 'high'),
    'P0353': ('Ignition Coil C Primary/Secondary Circuit', 'high'),
    'P0354': ('Ignition Coil D Primary/Secondary Circuit', 'high'),
    'P0355': ('Ignition Coil E Primary/Secondary Circuit', 'high'),
    'P0356': ('Ignition Coil F Primary/Secondary Circuit', 'high'),
    'P0357': ('Ignition Coil G Primary/Secondary Circuit', 'high'),
    'P0358': ('Ignition Coil H Primary/Secondary Circuit', 'high'),
    # Emissions
    'P0400': ('Exhaust Gas Recirculation Flow Malfunction', 'medium'),
    'P0401': ('EGR Flow Insufficient Detected', 'medium'),
    'P0402': ('EGR Excessive Flow Detected', 'medium'),
    'P0440': ('Evaporative Emission Control System Malfunction', 'low'),
    'P0441': ('EVAP Emission Control System Incorrect Purge Flow', 'low'),
    'P0442': ('EVAP Emission Control System Leak Detected (Small)', 'low'),
    'P0443': ('EVAP Emission Control System Purge Valve Malfunction', 'low'),
    'P0446': ('EVAP Emission Control System Vent Control Malfunction', 'low'),
    'P0455': ('EVAP Emission Control System Leak Detected (Large)', 'medium'),
    'P0456': ('EVAP Emission Control System Leak Detected (Very Small)', 'low'),
    # Transmission
    'P0700': ('Transmission Control System Malfunction', 'high'),
    'P0706': ('Transmission Range Sensor Circuit Range/Performance', 'medium'),
    'P0711': ('Transmission Fluid Temp Sensor Range/Performance', 'medium'),
    'P0712': ('Transmission Fluid Temp Sensor Circuit Low Input', 'medium'),
    'P0713': ('Transmission Fluid Temp Sensor Circuit High Input', 'medium'),
    'P0715': ('Input/Turbine Speed Sensor Circuit Malfunction', 'high'),
    'P0720': ('Output Speed Sensor Circuit Malfunction', 'high'),
    'P0730': ('Incorrect Gear Ratio', 'high'),
    'P0731': ('Gear 1 Incorrect Ratio', 'high'),
    'P0732': ('Gear 2 Incorrect Ratio', 'high'),
    'P0740': ('Torque Converter Clutch Circuit Malfunction', 'high'),
    'P0741': ('Torque Converter Clutch Circuit Performance', 'medium'),
    'P0748': ('Pressure Control Solenoid A Electrical', 'high'),
    'P0753': ('Shift Solenoid A Electrical', 'high'),
    'P0758': ('Shift Solenoid B Electrical', 'high'),
    # GM Specific (U-Codes / Network)
    'U0073': ('Control Module Communication Bus Off', 'high'),
    'U0100': ('Lost Communication With ECM/PCM', 'high'),
    'U0101': ('Lost Communication With TCM', 'high'),
    'U0121': ('Lost Communication With Anti-Lock Brake System', 'high'),
    'U0140': ('Lost Communication With Body Control Module', 'medium'),
    # Charging / Battery
    'P0562': ('System Voltage Low', 'high'),
    'P0563': ('System Voltage High', 'high'),
    'P0620': ('Generator Control Circuit Malfunction', 'high'),
    # Knock / VVT
    'P0325': ('Knock Sensor 1 Circuit Malfunction — Bank 1', 'high'),
    'P0326': ('Knock Sensor 1 Circuit Range/Performance', 'medium'),
    'P0327': ('Knock Sensor 1 Circuit Low Input — Bank 1', 'high'),
    'P0328': ('Knock Sensor 1 Circuit High Input — Bank 1', 'high'),
    'P0330': ('Knock Sensor 2 Circuit Malfunction — Bank 2', 'high'),
    # AFM / Cylinder Deactivation (GM specific)
    'P3400': ('Cylinder Deactivation System Bank 1', 'medium'),
    'P3401': ('Cylinder 1 Deactivation/Intake Valve Control Circuit Open', 'medium'),
    'P3404': ('Cylinder 4 Deactivation/Intake Valve Control Circuit Open', 'medium'),
    'P3411': ('Cylinder 5 Deactivation/Intake Valve Control Circuit Open', 'medium'),
    'P3441': ('Cylinder 7 Deactivation/Intake Valve Control Circuit Open', 'medium'),
    # DEF / Exhaust Aftertreatment (Diesel / Duramax)
    'P20EE': ('SCR NOx Catalyst Efficiency Below Threshold — Bank 1', 'high'),
    'P203F': ('Reductant Level Sensor Performance', 'medium'),
    'P204B': ('Reductant Pump Control Circuit Range/Performance', 'high'),
    'P2201': ('NOx Sensor Circuit Range/Performance — Bank 1', 'medium'),
}

def lookup_dtc(code):
    """Look up a DTC code — returns (description, severity) or None."""
    code = code.upper().strip()
    if code in DTC_DATABASE:
        return DTC_DATABASE[code]
    # Fuzzy: strip leading zeros in number part
    return None

def add_fault(code, description=None, severity='medium', status='active'):
    """Add a fault code. If no description given, auto-look up from DTC_DATABASE."""
    code = code.upper().strip()
    if description is None:
        lookup = DTC_DATABASE.get(code)
        if lookup:
            description, severity = lookup[0], lookup[1]
        else:
            description = f'Unknown fault — code {code}'
    fault_codes.append({
        'code':     code,
        'desc':     description,
        'severity': severity,
        'status':   status,
        'time':     datetime.now().strftime('%I:%M %p'),
        'date':     datetime.now().strftime('%B %d %Y'),
    })
    speak(f'Fault code {code}. {description}')

def clear_faults():
    fault_codes.clear()
    save_state()
    return 'Fault codes cleared.'

def show_faults():
    if not fault_codes:
        print('  No active fault codes.')
        return 'No active fault codes.'
    print('\n── FAULT CODES ──────────────────────────')
    for f in fault_codes:
        sev = f.get('severity', 'medium').upper()
        print(f'  {f["code"]} [{sev}] — {f["desc"]} — {f["time"]}')
    print('─────────────────────────────────────────\n')
    return f'{len(fault_codes)} active codes.'

# ── RACE / TRACK MODE ────────────────────
race_session = {
    'active':    False,
    'start_time': None,
    'runs':       [],
    'best_run':   None,
}

def start_race_session():
    race_session['active']     = True
    race_session['start_time'] = time.time()
    race_session['runs']       = []
    speak('Race session started. I am watching.')
    return 'Race session active. I am tracking everything.'

def end_race_session():
    race_session['active'] = False
    runs = race_session['runs']
    if not runs:
        return 'Race session ended. No runs recorded.'
    best = min(runs, key=lambda r: r.get('time', 99))
    return f'Race session done. {len(runs)} runs. Best was {best.get("time", "--")} seconds.'

# ── NIGHT MODE / DISPLAY BRIGHTNESS ──────
display_settings = {
    'night_mode': False,
    'brightness': 100,
    'color_theme': 'red',  # red, blue, green, white
}

def set_night_mode(on):
    display_settings['night_mode'] = on
    return 'Night mode on. Dimming display.' if on else 'Night mode off.'

def set_color_theme(color):
    valid = ['red', 'blue', 'green', 'white', 'orange', 'purple']
    if color in valid:
        display_settings['color_theme'] = color
        return f'Color theme set to {color}.'
    return f'Available themes: {", ".join(valid)}.'

# ── MAINTENANCE TRACKER ───────────────────
maintenance_log = {
    'oil_change':    {'last_date': None, 'last_miles': 0, 'interval_miles': 5000},
    'tire_rotation': {'last_date': None, 'last_miles': 0, 'interval_miles': 7500},
    'air_filter':    {'last_date': None, 'last_miles': 0, 'interval_miles': 15000},
    'spark_plugs':   {'last_date': None, 'last_miles': 0, 'interval_miles': 30000},
    'supercharger':  {'last_date': None, 'last_miles': 0, 'interval_miles': 50000},
    'brake_fluid':   {'last_date': None, 'last_miles': 0, 'interval_miles': 25000},
}
odometer = {'miles': 0}

def log_maintenance(item, miles=0):
    if item in maintenance_log:
        maintenance_log[item]['last_date']  = datetime.now().strftime('%B %d %Y')
        maintenance_log[item]['last_miles'] = miles or odometer['miles']
        save_state()
        return f'{item.replace("_"," ").title()} logged at {miles or odometer["miles"]} miles.'
    return f'Unknown maintenance item: {item}.'

def check_maintenance():
    due = []
    current = odometer['miles']
    for item, data in maintenance_log.items():
        if data['last_miles'] > 0:
            next_due = data['last_miles'] + data['interval_miles']
            if current >= next_due - 500:
                due.append(f'{item.replace("_"," ")} due at {next_due} miles')
    if not due:
        return 'All maintenance is up to date.'
    return 'Due soon: ' + ', '.join(due) + '.'

# ── PERFORMANCE CALCULATOR ───────────────
def calc_hp_estimate(ethanol_pct, boost_psi):
    phase = get_build_phase()
    if phase == 1:
        return 300  # stock LQ4 6.0L, naturally aspirated
    if phase == 2:
        base_hp    = 556                          # LSA stock crank rating
        eth_bonus  = (ethanol_pct / 100) * 140   # up to +140hp on full E85
        boost_tune = (boost_psi / 15)   * 50     # tuned boost headroom
        return round(base_hp + eth_bonus + boost_tune)
    # Phase 3 — forged, ported blower
    base_hp    = 700
    eth_bonus  = (ethanol_pct / 100) * 100
    boost_tune = (boost_psi / 15)   * 30
    return round(base_hp + eth_bonus + boost_tune)

# ── WEATHER-BASED WARNINGS ───────────────
def weather_performance_note():
    temp = weather['temp']
    cond = weather['condition'].lower()
    notes = []
    if temp < 32:
        notes.append('Road may be icy. TC recommended.')
    if temp > 95:
        notes.append('Hot ambient. Watch coolant temp.')
    if 'rain' in cond or 'snow' in cond:
        notes.append('Wet conditions. Take it easy on launch.')
    if temp < 50:
        notes.append('Cold air is dense. Expect good power numbers.')
    if temp > 40 and temp < 70 and 'clear' in cond:
        notes.append('Ideal conditions. Air is good.')
    return ' '.join(notes) if notes else 'Conditions look fine.'


# ══════════════════════════════════════════
# REAL-TIME DATA ENGINE
# ══════════════════════════════════════════

# ── SIMULATED SENSOR DATA (replaced by OBD2 on Pi) ──
sensor_data = {
    'rpm':           750,
    'speed_mph':     0,
    'boost_psi':     0.0,
    'throttle_pct':  0,
    'coolant_temp':  195,
    'oil_temp':      195,
    'oil_pressure':  45,
    'fuel_pressure': 58,
    'intake_temp':   75,
    'exhaust_temp':  800,
    'trans_temp':    160,
    'battery_v':     13.8,
    'alternator_v':  14.2,
    'map_kpa':       101,
    'maf_g_s':       8.2,
    'lambda':        1.00,
    'afr':           14.7,
    'timing_deg':    18,
    'knock_count':   0,
    'injector_pw':   3.2,
    'fuel_trim_st':  0.0,
    'fuel_trim_lt':  0.0,
    'idle_airflow':  18,
    'turbo_speed':   0,
    'boost_target':  0,
    'gear_pos':      1,
    'accel_x':       0.0,   # G force lateral
    'accel_y':       0.0,   # G force longitudinal
    'accel_z':       1.0,   # G force vertical
    'yaw_rate':      0.0,
}

def update_sensors_from_truck():
    sensor_data['rpm']          = truck_state['rpm']
    sensor_data['boost_psi']    = truck_state['boost']
    sensor_data['coolant_temp'] = truck_state['coolant_temp']
    sensor_data['oil_temp']     = truck_state['oil_temp']
    sensor_data['battery_v']    = truck_state['battery_main']
    sensor_data['throttle_pct'] = min(100, int(truck_state['rpm'] / 65))
    # Gear from truck_state (set by BeamNG bridge or OBD)
    sensor_data['gear_pos']     = truck_state.get('gear', 1)
    # Simulate AFR based on ethanol and boost
    eth = truck_state['ethanol']
    boost = truck_state['boost']
    if boost > 3:
        sensor_data['afr']    = round(12.0 - (eth / 100) * 1.5 - (boost / 15) * 0.5, 2)
        sensor_data['lambda'] = round(sensor_data['afr'] / 14.7, 3)
    else:
        sensor_data['afr']    = round(14.7 - (eth / 100) * 2.0, 2)
        sensor_data['lambda'] = round(sensor_data['afr'] / 14.7, 3)
    sensor_data['timing_deg']   = max(8, min(26, 22 - (boost * 0.5) + (eth / 100 * 4)))
    sensor_data['oil_pressure'] = max(20, min(65, 45 + (truck_state['rpm'] - 750) // 200))
    sensor_data['intake_temp']  = max(60, weather['temp'] + (boost * 8) - (eth / 100 * 20))
    sensor_data['trans_temp']   = min(220, 160 + (truck_state['rpm'] - 750) // 100)
    gear_map = {750:1, 1500:1, 2000:2, 3000:2, 3500:3, 4500:3, 5000:4, 5500:4}
    sensor_data['gear_pos'] = 1
    for rpm_thresh, gear in sorted(gear_map.items()):
        if truck_state['rpm'] >= rpm_thresh:
            sensor_data['gear_pos'] = gear

# ── G-FORCE TRACKER ──────────────────────
gforce_history = {'x': [], 'y': [], 'z': [], 'peak_lat': 0.0, 'peak_long': 0.0}

def update_gforce():
    # Simulate based on RPM changes
    rpm = truck_state['rpm']
    accel = min(1.2, max(-0.8, (rpm - 750) / 4000 * 0.8))
    sensor_data['accel_y'] = round(accel, 3)
    if abs(accel) > abs(gforce_history['peak_long']):
        gforce_history['peak_long'] = accel
    gforce_history['y'].append(accel)
    if len(gforce_history['y']) > 60:
        gforce_history['y'].pop(0)

# ── SENSOR HISTORY RING BUFFERS ────────────────────────────────────────────
# Stores 1Hz readings per sensor — max 3600 entries (1 hour) per sensor
_SENSOR_HISTORY_MAX = 3600
sensor_history: dict = {
    'rpm':          collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'speed':        collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'coolant_temp': collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'oil_temp':     collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'battery_v':    collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'boost_psi':    collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'afr':          collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'throttle_pct': collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'oil_pressure': collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'trans_temp':   collections.deque(maxlen=_SENSOR_HISTORY_MAX),
    'intake_temp':  collections.deque(maxlen=_SENSOR_HISTORY_MAX),
}
_sensor_history_lock = threading.Lock()
_sensor_history_last_append = 0.0

def _append_sensor_history():
    """Append current sensor readings to history at 1Hz."""
    global _sensor_history_last_append
    now = time.time()
    if now - _sensor_history_last_append < 1.0:
        return
    _sensor_history_last_append = now
    ts = datetime.now().strftime('%H:%M:%S')
    with _sensor_history_lock:
        for sensor, deque_ in sensor_history.items():
            val = sensor_data.get(sensor) or truck_state.get(sensor)
            if val is not None:
                deque_.append({'ts': ts, 'v': val, 't': now})

# ── LIVE DATA LOOP ────────────────────────
def live_data_loop():
    while True:
        update_sensors_from_truck()
        update_gforce()
        update_trip_stats()
        _append_sensor_history()
        time.sleep(0.5)

# ══════════════════════════════════════════
# SURVEILLANCE / CAMERA SYSTEM
# ══════════════════════════════════════════

surveillance = {
    'armed':          False,
    'motion_detected': False,
    'last_alert':     None,
    'alert_count':    0,
    'cameras':        {
        'front':  {'status': 'offline', 'url': ''},
        'rear':   {'status': 'offline', 'url': ''},
        'cab':    {'status': 'offline', 'url': ''},
        'left':   {'status': 'offline', 'url': ''},
        'right':  {'status': 'offline', 'url': ''},
    },
    'valet_log':      [],    # events logged while in valet mode
    'parking_alerts': [],
}

def arm_surveillance(on):
    surveillance['armed'] = on
    msg = 'Surveillance armed. I am watching.' if on else 'Surveillance disarmed.'
    return msg

def log_valet_event(event):
    entry = {
        'event': event,
        'time':  datetime.now().strftime('%I:%M %p'),
        'date':  datetime.now().strftime('%B %d %Y'),
        'rpm':   truck_state['rpm'],
        'speed': sensor_data['speed_mph'],
    }
    surveillance['valet_log'].append(entry)
    if len(surveillance['valet_log']) > 200:
        surveillance['valet_log'].pop(0)

def show_valet_log():
    log = surveillance['valet_log']
    if not log:
        return 'No valet events logged.'
    print('\n── VALET LOG ────────────────────────────')
    for e in log[-20:]:
        print(f'  [{e["time"]}] {e["event"]} — RPM {e["rpm"]} — {e["speed"]} MPH')
    print(f'\n  Total events: {len(log)}')
    print('─────────────────────────────────────────\n')
    return f'{len(log)} valet events logged.'

def set_camera_url(camera, url):
    if camera in surveillance['cameras']:
        surveillance['cameras'][camera]['url']    = url
        surveillance['cameras'][camera]['status'] = 'online' if url else 'offline'
        save_state()
        return f'{camera.title()} camera {"connected" if url else "removed"}.'
    return f'Unknown camera: {camera}. Options: front rear cab left right.'

# ── VALET MONITOR — logs events when tier 4 is driving ──
def valet_monitor():
    last_rpm       = 0
    last_tier      = 1
    speed_warned   = False
    rpm_warned     = False
    while True:
        current_tier  = tier_state['current']
        current_rpm   = truck_state['rpm']
        current_speed = truck_state.get('speed', 0)
        if current_tier >= 4:
            if current_rpm > 2500 and last_rpm <= 2500:
                log_valet_event(f'RPM exceeded 2500 — hit {current_rpm}')
            if current_rpm > 3500 and not rpm_warned:
                log_valet_event(f'High RPM warning — {current_rpm}')
                speak('Easy on the RPMs.')
                rpm_warned = True
            elif current_rpm <= 3500:
                rpm_warned = False
            if current_speed > 35 and not speed_warned:
                log_valet_event(f'[VALET SPEED LIMIT] Speed over 35 MPH — {current_speed} MPH')
                speak('Speed limit is 35. Slow down.')
                awareness['warnings_active'].append('valet_speed')
                speed_warned = True
            elif current_speed <= 30:
                speed_warned = False
                if 'valet_speed' in awareness['warnings_active']:
                    awareness['warnings_active'].remove('valet_speed')
        last_rpm  = current_rpm
        last_tier = current_tier
        time.sleep(3)

# ══════════════════════════════════════════
# DRAG STRIP TIMER
# ══════════════════════════════════════════

drag_timer = {
    'active':     False,
    'stage':      'idle',    # idle -> staged -> launch -> running -> done
    'start_time': None,
    'splits':     {},        # '60ft', '330ft', '660ft', '1000ft', '1320ft'
    'runs':       [],
    'best_et':    None,
    'best_mph':   None,
}

def start_drag_run():
    drag_timer['active']     = True
    drag_timer['stage']      = 'staged'
    drag_timer['start_time'] = None
    drag_timer['splits']     = {}
    speak('Staged. Launch when ready.')
    return None

def launch_drag():
    if not drag_timer['active']:
        return 'Stage the run first. Say start drag run.'
    drag_timer['stage']      = 'running'
    drag_timer['start_time'] = time.time()
    speak('Launch.')
    import threading as _t
    _t.Thread(target=_run_drag_sim, daemon=True).start()
    return None

def _run_drag_sim():
    start = drag_timer['start_time']
    announced = set()
    while drag_timer['active']:
        elapsed = time.time() - start
        speed   = min(120, elapsed * 22)   # rough sim
        sensor_data['speed_mph'] = round(speed)

        if elapsed >= 1.3 and '60ft' not in announced:
            drag_timer['splits']['60ft'] = round(elapsed, 3)
            speak(f'60 foot. {elapsed:.2f}.')
            announced.add('60ft')

        if elapsed >= 4.5 and '330ft' not in announced:
            drag_timer['splits']['330ft'] = round(elapsed, 3)
            speak(f'330 foot. {elapsed:.2f}.')
            announced.add('330ft')

        if elapsed >= 7.2 and '660ft' not in announced:
            drag_timer['splits']['660ft'] = round(elapsed, 3)
            speak(f'Half mile. {elapsed:.2f} at {round(speed)} MPH.')
            announced.add('660ft')

        if elapsed >= 11.5 and '1000ft' not in announced:
            drag_timer['splits']['1000ft'] = round(elapsed, 3)
            speak(f'Thousand foot. {elapsed:.2f}.')
            announced.add('1000ft')

        if elapsed >= 13.5 and '1320ft' not in announced:
            drag_timer['splits']['1320ft'] = round(elapsed, 3)
            et   = round(elapsed, 3)
            mph  = round(speed)
            drag_timer['splits']['et']  = et
            drag_timer['splits']['mph'] = mph
            drag_timer['active']  = False
            drag_timer['stage']   = 'done'
            sensor_data['speed_mph'] = 0
            run = {'et': et, 'mph': mph, 'splits': dict(drag_timer['splits']), 'date': datetime.now().strftime('%B %d %Y')}
            drag_timer['runs'].append(run)
            if drag_timer['best_et'] is None or et < drag_timer['best_et']:
                drag_timer['best_et']  = et
                drag_timer['best_mph'] = mph
                speak(f'New personal best. {et:.3f} at {mph} MPH.')
            else:
                speak(f'Run complete. {et:.3f} at {mph} MPH.')
            break
        time.sleep(0.05)

def show_drag_runs():
    runs = drag_timer['runs']
    if not runs:
        return 'No drag runs recorded yet.'
    print('\n── DRAG RUNS ────────────────────────────')
    for i, r in enumerate(runs[-10:], 1):
        print(f'  Run {i}: {r["et"]}s @ {r["mph"]} MPH — {r["date"]}')
    if drag_timer['best_et']:
        print(f'\n  BEST: {drag_timer["best_et"]}s @ {drag_timer["best_mph"]} MPH')
    print('─────────────────────────────────────────\n')
    return f'Best ET: {drag_timer["best_et"]}s at {drag_timer["best_mph"]} MPH.'

# ══════════════════════════════════════════
# PARKING / SECURITY SYSTEM
# ══════════════════════════════════════════

parking_mode = {
    'active':      False,
    'location':    '',
    'parked_at':   None,
    'alerts':      [],
    'phone_number': '',   # for SMS alerts (future)
}

def activate_parking_mode(location=''):
    parking_mode['active']    = True
    parking_mode['location']  = location or 'unknown location'
    parking_mode['parked_at'] = datetime.now().strftime('%I:%M %p')
    arm_surveillance(True)
    msg = f'Parking mode active at {parking_mode["location"]}. Surveillance armed.'
    return msg

def deactivate_parking_mode():
    parking_mode['active'] = False
    arm_surveillance(False)
    return 'Parking mode off. Welcome back.'

# ══════════════════════════════════════════
# ROUTE / LOCATION TRACKER  
# ══════════════════════════════════════════

_LOC_CACHE = '/tmp/archer_location.json'
def _load_location_cache():
    try:
        with open(_LOC_CACHE) as f:
            c = json.load(f)
        return c.get('lat'), c.get('lon'), c.get('name', '')
    except Exception:
        return None, None, ''
def _save_location_cache(lat, lon, name):
    try:
        with open(_LOC_CACHE, 'w') as f:
            json.dump({'lat': lat, 'lon': lon, 'name': name}, f)
    except Exception:
        pass

_cached_lat, _cached_lon, _cached_name = _load_location_cache()
location_data = {
    'current_road':   '',
    'destination':    '',
    'trip_distance':  0.0,
    'session_miles':  0.0,
    'last_location':  '',
    'location_log':   [],
    'lat':            _cached_lat,
    'lon':            _cached_lon,
    'location_name':  _cached_name,
}

def set_destination(dest):
    location_data['destination'] = dest
    return f'Destination set to {dest}. Let me know when you arrive.'

def log_location(road):
    location_data['current_road'] = road
    location_data['location_log'].append({
        'road': road,
        'time': datetime.now().strftime('%I:%M %p'),
    })
    if len(location_data['location_log']) > 100:
        location_data['location_log'].pop(0)

# ══════════════════════════════════════════
# AUDIO/MUSIC INTEGRATION
# ══════════════════════════════════════════

audio_system = {
    'source':       'radio',    # radio, bluetooth, aux, usb
    'volume':       50,
    'bass':         5,
    'treble':       5,
    'eq_preset':    'flat',     # flat, bass_boost, vocals, stage
    'song':         '',
    'artist':       '',
    'playing':      False,
}

def set_audio(key, value):
    if key in audio_system:
        audio_system[key] = value
        save_state()
    return f'Audio {key} set to {value}.'

EQ_PRESETS = {
    'flat':       'Flat — neutral response.',
    'bass_boost': 'Bass boost — heavy low end for the exhaust.',
    'vocals':     'Vocals — midrange forward for calls and podcasts.',
    'stage':      'Stage — wide soundstage for live music.',
    'sub':        'Sub — tuned for the Rockford T1 subs.',
}

def set_eq(preset):
    if preset in EQ_PRESETS:
        audio_system['eq_preset'] = preset
        save_state()
        return EQ_PRESETS[preset]
    return f'Available presets: {", ".join(EQ_PRESETS.keys())}.'


# ══════════════════════════════════════════
# TIRE PRESSURE MONITORING (TPMS)
# ══════════════════════════════════════════
tpms = {
    'fl': {'psi': 35.0, 'temp': 75, 'status': 'ok'},
    'fr': {'psi': 35.0, 'temp': 75, 'status': 'ok'},
    'rl': {'psi': 35.0, 'temp': 75, 'status': 'ok'},
    'rr': {'psi': 35.0, 'temp': 75, 'status': 'ok'},
}
TARGET_PSI = {'street': 35, 'track': 32, 'tow': 65, 'drag': 28}

def check_tpms():
    issues = []
    for wheel, data in tpms.items():
        if data['psi'] < 28:
            issues.append(f'{wheel.upper()} critically low at {data["psi"]} PSI')
        elif data['psi'] < 32:
            issues.append(f'{wheel.upper()} low at {data["psi"]} PSI')
        if data['temp'] > 180:
            issues.append(f'{wheel.upper()} tire temp high at {data["temp"]}F')
    if not issues:
        return 'All tires good. FL {fl[psi]} FR {fr[psi]} RL {rl[psi]} RR {rr[psi]}.'.format(**tpms)
    return ' '.join(issues)

def set_tire_pressure(wheel, psi):
    if wheel in tpms:
        tpms[wheel]['psi'] = float(psi)
        save_state()
        return f'{wheel.upper()} set to {psi} PSI.'
    return 'Wheels: fl fr rl rr'

# ══════════════════════════════════════════
# LAUNCH CONTROL SYSTEM
# ══════════════════════════════════════════
launch_control = {
    'enabled':       False,
    'launch_rpm':    3500,
    'flat_foot':     False,
    'two_step_rpm':  3500,
    'boost_build':   False,
    'last_60ft':     None,
    'best_60ft':     None,
    'attempts':      0,
}

def configure_launch(rpm=3500, flat_foot=False):
    launch_control['launch_rpm']  = rpm
    launch_control['flat_foot']   = flat_foot
    launch_control['enabled']     = True
    msg = f'Launch control set. Hold at {rpm} RPM.'
    if flat_foot:
        msg += ' Flat foot shift enabled.'
    return msg

def launch_sequence():
    if not launch_control['enabled']:
        return 'Configure launch control first. Say configure launch.'
    launch_control['attempts'] += 1
    rpm = launch_control['launch_rpm']
    eth = truck_state['ethanol']
    oil = truck_state['oil_temp']
    tc  = truck_state['tc_on']
    issues = []
    if oil < 180: issues.append(f'oil at {oil}F')
    if tc:        issues.append('TC is on')
    if eth < 50:  issues.append(f'ethanol only {eth}%')
    if issues:
        return f'Launch not ideal. {", ".join(issues)}. Your call.'
    return f'Launch control armed at {rpm} RPM. TC is {"on" if tc else "off"}. Boost build {"ready" if eth > 70 else "limited"}. Go when ready.'

# ══════════════════════════════════════════
# BOOST CONTROLLER / BOOST BY GEAR
# ══════════════════════════════════════════
boost_controller = {
    'mode':         'auto',     # auto, manual, gear_based
    'max_boost':    15,
    'target_boost': 12,
    'gear_limits':  {1: 8, 2: 11, 3: 13, 4: 15},
    'boost_creep':  0.0,
    'wastegate':    'closed',
    'history':      [],
}

def set_boost_by_gear(gear_limits):
    boost_controller['gear_limits'].update(gear_limits)
    boost_controller['mode'] = 'gear_based'
    save_state()
    return f'Boost by gear set. G1:{gear_limits.get(1,8)} G2:{gear_limits.get(2,11)} G3:{gear_limits.get(3,13)} G4:{gear_limits.get(4,15)} PSI.'

def set_max_boost(psi):
    boost_controller['max_boost']    = psi
    boost_controller['target_boost'] = min(psi, boost_controller['target_boost'])
    save_state()
    return f'Max boost set to {psi} PSI.'

# ══════════════════════════════════════════
# SHIFT LIGHT / SHIFT INDICATOR
# ══════════════════════════════════════════
shift_light = {
    'enabled':    True,
    'warn_rpm':   5200,
    'shift_rpm':  5800,
    'redline':    6200,
    'pattern':    'sweep',    # sweep, flash, solid
    'currently':  'off',      # off, warn, shift, redline
}

def update_shift_light():
    rpm = truck_state['rpm']
    if rpm >= shift_light['redline']:
        shift_light['currently'] = 'redline'
    elif rpm >= shift_light['shift_rpm']:
        shift_light['currently'] = 'shift'
    elif rpm >= shift_light['warn_rpm']:
        shift_light['currently'] = 'warn'
    else:
        shift_light['currently'] = 'off'

# ══════════════════════════════════════════
# REMOTE START SIMULATION
# ══════════════════════════════════════════
remote_start = {
    'status':        'off',    # off, starting, running, warming
    'runtime_mins':  0,
    'auto_off_mins': 15,
    'started_at':    None,
    'warm_temp':     160,
}

def remote_start_engine():
    if remote_start['status'] == 'running':
        return 'Engine is already running remotely.'
    remote_start['status']     = 'starting'
    remote_start['started_at'] = datetime.now().strftime('%I:%M %p')
    speak('Remote start initiated.')
    def _warm():
        time.sleep(2)
        remote_start['status'] = 'warming'
        truck_state['rpm']     = 900
        speak('Engine running. Warming up.')
        for i in range(12):
            time.sleep(5)
            remote_start['runtime_mins'] += 0.083
            if truck_state['oil_temp'] < remote_start['warm_temp']:
                truck_state['oil_temp'] = min(remote_start['warm_temp'], truck_state['oil_temp'] + 4)
            if remote_start['runtime_mins'] >= remote_start['auto_off_mins']:
                remote_stop_engine()
                return
        remote_start['status'] = 'running'
    import threading as _t
    _t.Thread(target=_warm, daemon=True).start()
    return None

def remote_stop_engine():
    remote_start['status']        = 'off'
    remote_start['runtime_mins']  = 0
    truck_state['rpm']            = 0
    speak('Remote start off.')
    return 'Engine off.'

# ══════════════════════════════════════════
# AIR SUSPENSION CONTROLLER
# ══════════════════════════════════════════
air_suspension = {
    'front_psi':    85,
    'rear_psi':     90,
    'height_mode':  'drive',    # slam, low, drive, high, show, tow
    'presets':      {
        'slam':  {'front': 20, 'rear': 20, 'desc': 'Slammed — on the ground'},
        'low':   {'front': 50, 'rear': 55, 'desc': 'Street low'},
        'drive': {'front': 85, 'rear': 90, 'desc': 'Normal drive height'},
        'high':  {'front': 110,'rear': 115,'desc': 'Max clearance'},
        'show':  {'front': 15, 'rear': 15, 'desc': 'Show stance — fully dropped'},
        'tow':   {'front': 100,'rear': 120,'desc': 'Tow mode — rear stiff'},
    },
    'leveling':     True,
    'save_on_exit': True,
}

def set_air_height(mode):
    if mode in air_suspension['presets']:
        preset = air_suspension['presets'][mode]
        air_suspension['front_psi']   = preset['front']
        air_suspension['rear_psi']    = preset['rear']
        air_suspension['height_mode'] = mode
        save_state()
        return f'{mode.title()} mode. Front {preset["front"]} rear {preset["rear"]} PSI. {preset["desc"]}.'
    return f'Air modes: {", ".join(air_suspension["presets"].keys())}.'

def set_air_manual(corner, psi):
    key = corner + '_psi'
    if key in air_suspension:
        air_suspension[key] = float(psi)
        save_state()
        return f'{corner.title()} air at {psi} PSI.'
    return 'Corners: front rear'

# ══════════════════════════════════════════
# EXHAUST TEMPERATURE MONITOR
# ══════════════════════════════════════════
exhaust_monitor = {
    'egt_1':       800,     # cylinder 1 EGT (F)
    'egt_2':       810,
    'egt_avg':     805,
    'egt_max':     1800,    # safe limit
    'egt_warn':    1600,
    'pre_cat':     900,
    'post_cat':    700,
    'history':     [],
}

def update_egt():
    rpm   = truck_state['rpm']
    boost = truck_state['boost']
    eth   = truck_state['ethanol']
    base  = 700 + (rpm / 6200) * 800 + boost * 30 - (eth / 100) * 150
    exhaust_monitor['egt_avg'] = round(base)
    exhaust_monitor['egt_1']   = round(base + random.randint(-30, 30))
    exhaust_monitor['egt_2']   = round(base + random.randint(-30, 30))
    exhaust_monitor['history'].append(round(base))
    if len(exhaust_monitor['history']) > 60:
        exhaust_monitor['history'].pop(0)

# ══════════════════════════════════════════
# INTERCOOLER / HEAT SOAK MONITOR
# ══════════════════════════════════════════
heat_soak = {
    'ic_inlet_temp':    75,
    'ic_outlet_temp':   70,
    'ic_efficiency':    95,
    'heat_soak_risk':   'low',
    'cool_down_needed': False,
    'soak_history':     [],
}

def check_heat_soak():
    intake  = sensor_data['intake_temp']
    ambient = weather['temp']
    delta   = intake - ambient
    if delta > 80:
        heat_soak['heat_soak_risk']   = 'critical'
        heat_soak['cool_down_needed'] = True
        return f'Heat soak critical. Intake {intake}F over ambient by {delta}F. Cool down before another run.'
    elif delta > 50:
        heat_soak['heat_soak_risk'] = 'high'
        return f'Heat soak building. Intake {intake}F. Give it a few minutes.'
    elif delta > 25:
        heat_soak['heat_soak_risk'] = 'moderate'
        return f'Some heat soak. Intake {intake}F. Should be fine.'
    else:
        heat_soak['heat_soak_risk']   = 'low'
        heat_soak['cool_down_needed'] = False
        return f'No heat soak. Intake {intake}F. Ready to run.'

# ══════════════════════════════════════════
# TRIP STATISTICS TRACKER
# ══════════════════════════════════════════
trip_stats = {
    'distance_miles':  0.0,
    'fuel_used_gal':   0.0,
    'avg_mpg':         0.0,
    'instant_mpg':     0.0,
    'start_time':      None,
    'last_update':     None,
    '_last_speed':     0.0,
}

def update_trip_stats():
    """Call approximately every second while engine is running to accumulate trip data."""
    now = time.time()
    rpm   = truck_state.get('rpm', 0)
    speed = truck_state.get('speed', 0)

    if rpm < 400:
        return  # engine off — don't accumulate

    if trip_stats['start_time'] is None:
        trip_stats['start_time'] = now

    last = trip_stats.get('last_update') or now
    dt_hours = (now - last) / 3600.0  # elapsed time in hours

    # Distance: speed (mph) * time (hours)
    dist_delta = speed * dt_hours
    trip_stats['distance_miles'] += dist_delta

    # Fuel rate estimate: base idle ~0.3 gph, scales with rpm/speed
    # Rough approximation: GPH = 0.3 + (rpm/750 - 1) * 0.8 + speed * 0.008
    gph = max(0.2, 0.3 + (rpm / 750 - 1) * 0.8 + speed * 0.008)
    fuel_delta = gph * dt_hours
    trip_stats['fuel_used_gal'] += fuel_delta

    # Instant MPG: speed / GPH (if moving and using fuel)
    if speed > 5 and gph > 0:
        trip_stats['instant_mpg'] = min(99.9, speed / gph)
    elif speed <= 5:
        trip_stats['instant_mpg'] = 0.0

    # Session avg MPG
    if trip_stats['fuel_used_gal'] > 0.01:
        trip_stats['avg_mpg'] = trip_stats['distance_miles'] / trip_stats['fuel_used_gal']

    trip_stats['last_update'] = now

def reset_trip_stats():
    trip_stats['distance_miles'] = 0.0
    trip_stats['fuel_used_gal']  = 0.0
    trip_stats['avg_mpg']        = 0.0
    trip_stats['instant_mpg']    = 0.0
    trip_stats['start_time']     = None
    trip_stats['last_update']    = None
    return 'Trip stats reset.'

# ══════════════════════════════════════════
# FUEL LEVEL TRACKER
# ══════════════════════════════════════════
fuel_tank = {
    'capacity_gal':   26.0,     # Sierra 2500HD tank
    'current_gal':    20.0,
    'e85_gal':        0.0,      # Phase 1 — pump gas only
    'regular_gal':    20.0,
    'mpg_current':    12.5,
    'mpg_session':    0.0,
    'range_est':      0,
    'fuel_cost':      0.0,
    'low_fuel_warn':  4.0,
}

def update_fuel_estimate():
    rpm   = truck_state['rpm']
    mpg   = max(3, min(18, 18 - (rpm - 750) / 500))
    fuel_tank['mpg_current'] = round(mpg, 1)
    fuel_tank['range_est']   = round(fuel_tank['current_gal'] * mpg)

def set_fuel_level(gallons):
    fuel_tank['current_gal'] = float(gallons)
    update_fuel_estimate()
    save_state()
    return f'Fuel set to {gallons} gallons. Range est {fuel_tank["range_est"]} miles.'

def fuel_stop(e85_added, regular_added=0, e85_price=0, regular_price=0):
    fuel_tank['current_gal'] = min(fuel_tank['capacity_gal'],
        fuel_tank['current_gal'] + e85_added + regular_added)
    fuel_tank['e85_gal']     += e85_added
    fuel_tank['regular_gal'] += regular_added
    cost = (e85_added * e85_price) + (regular_added * regular_price)
    fuel_tank['fuel_cost']   += cost
    new_eth = calculate_ethanol(fuel_tank['e85_gal'], fuel_tank['regular_gal'])
    update_fuel_estimate()
    save_state()
    return f'Fuel added. {fuel_tank["current_gal"]:.1f} gallons. E{new_eth}. Range {fuel_tank["range_est"]} miles.{"" if cost == 0 else f" Cost ${cost:.2f}."}'

# ══════════════════════════════════════════
# COMPUSTAR SECURITY INTEGRATION
# ══════════════════════════════════════════
compustar = {
    'armed':        True,
    'disarmed':     False,
    'shock_sens':   5,          # 1-10
    'tilt_sens':    5,
    'door_triggers': True,
    'hood_trigger':  True,
    'aux_1':        False,      # for future accessory
    'aux_2':        False,
    'panic_active': False,
    'last_trigger': None,
    'trigger_log':  [],
}

def arm_compustar():
    compustar['armed']    = True
    compustar['disarmed'] = False
    save_state()
    return 'Compustar armed. All zones active.'

def disarm_compustar():
    compustar['armed']    = False
    compustar['disarmed'] = True
    save_state()
    return 'Compustar disarmed.'

def set_shock_sensitivity(level):
    level = max(1, min(10, int(level)))
    compustar['shock_sens'] = level
    save_state()
    return f'Shock sensitivity set to {level}.'

# ══════════════════════════════════════════
# HELIX DSP AUDIO PROCESSOR
# ══════════════════════════════════════════
helix_dsp = {
    'input_gain':   0,      # dB
    'time_align':   True,
    'phase_correct': True,
    'sub_level':    80,     # % of max
    'sub_freq':     80,     # Hz crossover
    'sub_slope':    24,     # dB/oct
    'mid_level':    75,
    'high_level':   70,
    'fader_front':  60,
    'fader_rear':   40,
    'eq_bands':     {
        '60hz':  2,
        '120hz': 1,
        '250hz': 0,
        '500hz': -1,
        '1khz':  0,
        '2khz':  1,
        '4khz':  0,
        '8khz':  2,
        '16khz': 1,
    },
    'preset':       'daily',    # daily, bass, stage, reference
}

HELIX_PRESETS = {
    'daily':     'Balanced for daily driving and calls.',
    'bass':      'Sub forward. Rockford T1 subs pushed hard.',
    'stage':     'Wide imaging. Good for shows.',
    'reference': 'Flat reference tuning. Pure sound.',
    'night':     'Reduced bass. No rattles at night.',
}

def set_helix_preset(preset):
    if preset in HELIX_PRESETS:
        helix_dsp['preset'] = preset
        save_state()
        return f'Helix preset: {HELIX_PRESETS[preset]}'
    return f'Presets: {", ".join(HELIX_PRESETS.keys())}.'

def set_sub_level(pct):
    helix_dsp['sub_level'] = max(0, min(100, int(pct)))
    save_state()
    return f'Sub level at {helix_dsp["sub_level"]}%.'

# ══════════════════════════════════════════
# AMBIENT LIGHTING CONTROLLER (FUTURE INSTALL)
# ══════════════════════════════════════════
ambient_lighting = {
    'zones': {
        'footwell_front': {'on': False, 'color': '#cc0000', 'brightness': 80},
        'footwell_rear':  {'on': False, 'color': '#cc0000', 'brightness': 80},
        'dash':           {'on': False, 'color': '#cc0000', 'brightness': 60},
        'door_panels':    {'on': False, 'color': '#cc0000', 'brightness': 70},
        'bed':            {'on': False, 'color': '#ffffff', 'brightness': 100},
        'underglow':      {'on': False, 'color': '#cc0000', 'brightness': 100},
    },
    'mode':     'off',      # off, all, zone, sync_music, breathe, party
    'master':   False,
}

def set_ambient(zone, on, color=None, brightness=None):
    if zone == 'all':
        for z in ambient_lighting['zones']:
            ambient_lighting['zones'][z]['on'] = on
        ambient_lighting['master'] = on
    elif zone in ambient_lighting['zones']:
        ambient_lighting['zones'][zone]['on'] = on
        if color:      ambient_lighting['zones'][zone]['color']      = color
        if brightness: ambient_lighting['zones'][zone]['brightness'] = brightness
    save_state()
    return f'Ambient {zone} {"on" if on else "off"}.'

def ambient_mode(mode):
    ambient_lighting['mode'] = mode
    if mode == 'all':
        for z in ambient_lighting['zones']:
            ambient_lighting['zones'][z]['on'] = True
        ambient_lighting['master'] = True
    elif mode == 'off':
        for z in ambient_lighting['zones']:
            ambient_lighting['zones'][z]['on'] = False
        ambient_lighting['master'] = False
    save_state()
    return f'Ambient mode: {mode}.'

# ══════════════════════════════════════════
# COOLANT SYSTEM MONITOR
# ══════════════════════════════════════════
coolant_system = {
    'temp':         195,
    'pressure_psi': 14,
    'flow_rate':    8.5,     # GPM
    'thermostat':   'open',  # open, closed, stuck
    'fan_speed':    0,       # 0-100%
    'overflow':     False,
    'history':      [],
}

def check_coolant():
    temp = coolant_system['temp']
    if temp > 240:
        return f'Coolant critical at {temp}F. Pull over.'
    elif temp > 225:
        return f'Coolant high at {temp}F. Watch it.'
    elif temp < 160 and truck_state['rpm'] > 1500:
        return f'Coolant cold at {temp}F. Thermostat may be stuck open.'
    return f'Coolant at {temp}F. Normal.'

# ══════════════════════════════════════════
# VOICE NOTE / LOGBOOK
# ══════════════════════════════════════════
voice_notes = []

def add_voice_note(text):
    note = {
        'text':   text,
        'time':   datetime.now().strftime('%I:%M %p'),
        'date':   datetime.now().strftime('%B %d %Y'),
        'rpm':    truck_state['rpm'],
        'speed':  sensor_data.get('speed_mph', 0),
        'road':   road_memory[current_road]['name'] if current_road else 'unknown',
    }
    voice_notes.append(note)
    if len(voice_notes) > 500:
        voice_notes.pop(0)
    save_state()
    return f'Note saved.'

def show_voice_notes(n=10):
    if not voice_notes:
        return 'No notes saved.'
    print('\n── VOICE NOTES ──────────────────────────')
    for note in voice_notes[-n:]:
        print(f'  [{note["date"]} {note["time"]}] {note["text"]}')
        print(f'    Road: {note["road"]} — RPM: {note["rpm"]}')
    print('─────────────────────────────────────────\n')
    return f'{len(voice_notes)} notes saved.'

# ══════════════════════════════════════════
# COMPETITION / BRACKET RACING
# ══════════════════════════════════════════
bracket_racing = {
    'dial_in':      None,       # target ET
    'reaction_time': None,
    'runs':         [],
    'class':        'street',   # street, bracket, pro
    'best_reaction': None,
}

def set_dial_in(et):
    bracket_racing['dial_in'] = float(et)
    return f'Dial-in set to {et}. Stay close to that number or you break out.'

def check_breakout(actual_et):
    dial = bracket_racing['dial_in']
    if dial is None:
        return 'No dial-in set.'
    diff = actual_et - dial
    if diff < 0:
        return f'Breakout by {abs(diff):.3f} seconds. You lost on a breakout.'
    elif diff < 0.05:
        return f'Tight. {diff:.3f} seconds over your dial. Good run.'
    else:
        return f'{diff:.3f} over your dial. Room to cut.'

# ══════════════════════════════════════════
# SMART DIAGNOSTICS — ARCHER AI ANALYSIS
# ══════════════════════════════════════════
def archer_diagnostics():
    issues   = []
    warnings = []
    good     = []

    # Check all systems
    oil = truck_state['oil_temp']
    if oil > 230:   issues.append(f'oil temp critical at {oil}F')
    elif oil > 215: warnings.append(f'oil temp elevated at {oil}F')
    else:           good.append('oil temp normal')

    bat = truck_state['battery_main']
    if bat < 12.0:   issues.append(f'battery low at {bat}V')
    elif bat < 12.8: warnings.append(f'battery borderline at {bat}V')
    else:            good.append('battery healthy')

    eth = truck_state['ethanol']
    boost = truck_state['boost']
    if eth < 30 and boost > 8: issues.append(f'low ethanol {eth}% under high boost {boost}PSI — knock risk')
    elif eth < 50 and boost > 5: warnings.append(f'consider more E85 at this boost level')
    else: good.append('fuel mix appropriate for boost')

    afr = sensor_data.get('afr', 14.7)
    if afr > 14.0 and boost > 3: issues.append(f'AFR lean at {afr} under boost')
    elif afr < 10.5: warnings.append(f'AFR rich at {afr} — tuning issue or fuel leak')
    else: good.append(f'AFR {afr} is in the window')

    knock = sensor_data.get('knock_count', 0)
    if knock > 5:   issues.append(f'{knock} knock events detected — check fuel and timing')
    elif knock > 0: warnings.append(f'{knock} knock events — monitor')

    for wheel, data in tpms.items():
        if data['psi'] < 28: issues.append(f'{wheel.upper()} tire pressure critical')

    # Build report
    report = []
    if issues:   report.append(f'{len(issues)} issue{"s" if len(issues)>1 else ""}: {"; ".join(issues)}.')
    if warnings: report.append(f'{len(warnings)} warning{"s" if len(warnings)>1 else ""}: {"; ".join(warnings)}.')
    if not issues and not warnings:
        report.append(f'All systems clean. {len(good)} checks passed.')

    return ' '.join(report)

# ══════════════════════════════════════════
# WEATHER-BASED TUNE RECOMMENDATIONS  
# ══════════════════════════════════════════
def tune_recommendation():
    temp  = weather['temp']
    humid = weather.get('humidity', 50)
    cond  = weather['condition'].lower()
    eth   = truck_state['ethanol']
    recs  = []

    # Air density factor (cold air = more power)
    if temp < 40:
        recs.append('Cold dense air. Expect 3-5% more power than baseline. Boost may creep. Watch manifold pressure.')
    elif temp > 85:
        recs.append('Hot thin air. Expect 3-5% power loss. Consider pulling 1-2 PSI of boost.')

    # Ethanol recommendations based on conditions
    if temp < 20 and eth > 80:
        recs.append('High E85 in cold — cold start may be rough. Archer recommends adding some 93 to the mix.')
    elif temp > 90 and eth < 60:
        recs.append('Hot ambient with lower ethanol means more heat soak risk. Top off with E85 if possible.')

    # Rain / wet track
    if 'rain' in cond or 'wet' in cond:
        recs.append('Wet conditions. Drop boost 2-3 PSI. TC highly recommended.')

    if not recs:
        recs.append('Conditions are favorable. No tune changes needed.')

    return ' '.join(recs)


# ══════════════════════════════════════════
# POLICE RADAR DETECTOR INTEGRATION
# ══════════════════════════════════════════
radar_detector = {
    'enabled':      True,
    'band':         None,       # X, K, Ka, Laser, MRCD, POP
    'strength':     0,          # 1-5 bars
    'alert_level':  'clear',    # clear, weak, moderate, strong, laser
    'direction':    'unknown',  # front, rear, side
    'last_alert':   None,
    'alert_log':    [],
    'muted':        False,
    'city_mode':    False,      # reduces false positives in city
    'highway_mode': True,
    'known_alerts': [],         # confirmed radar locations
    'false_alerts': [],         # marked as false positives
    'alert_count_session': 0,
}

RADAR_BANDS = {
    'X':    {'freq': '10.5 GHz', 'range': 'medium', 'common': 'older speed signs, some police'},
    'K':    {'freq': '24.1 GHz', 'range': 'medium', 'common': 'most common police radar'},
    'Ka':   {'freq': '33-36 GHz','range': 'long',   'common': 'most modern police radar'},
    'Laser':{'freq': 'laser',    'range': 'precise', 'common': 'LIDAR — point and shoot'},
    'MRCD': {'freq': '24.1 GHz', 'range': 'short',  'common': 'photo radar — sneaky'},
    'POP':  {'freq': 'K/Ka',     'range': 'instant', 'common': 'instant-on Ka — hardest to detect'},
}

def radar_alert(band, strength, direction='front'):
    if radar_detector['muted']:
        return
    radar_detector['band']        = band
    radar_detector['strength']    = strength
    radar_detector['direction']   = direction
    radar_detector['last_alert']  = datetime.now().strftime('%I:%M %p')
    radar_detector['alert_count_session'] += 1

    entry = {
        'band':      band,
        'strength':  strength,
        'direction': direction,
        'time':      datetime.now().strftime('%I:%M %p'),
        'road':      road_memory[current_road]['name'] if current_road else 'unknown',
        'speed':     sensor_data.get('speed_mph', 0),
    }
    radar_detector['alert_log'].append(entry)
    if len(radar_detector['alert_log']) > 200:
        radar_detector['alert_log'].pop(0)

    level_map = {1: 'weak', 2: 'moderate', 3: 'moderate', 4: 'strong', 5: 'strong'}
    radar_detector['alert_level'] = 'laser' if band == 'Laser' else level_map.get(strength, 'weak')

    # Speak alert based on severity
    if band == 'Laser':
        speak(f'Laser. {direction}. Slow down now.')
    elif band == 'POP':
        speak(f'POP radar. Instant on. {direction}.')
    elif strength >= 4:
        speak(f'{band} band. Strong. {direction}.')
    elif strength >= 2:
        speak(f'{band} band. {direction}.')
    else:
        speak(f'{band} weak.')

def radar_clear():
    radar_detector['band']        = None
    radar_detector['strength']    = 0
    radar_detector['alert_level'] = 'clear'
    radar_detector['direction']   = 'unknown'

def mark_false_alert():
    if radar_detector['last_alert']:
        radar_detector['false_alerts'].append({
            'road': road_memory[current_road]['name'] if current_road else 'unknown',
            'time': radar_detector['last_alert'],
        })
        return 'Marked as false alert. Learning your area.'
    return 'No recent alert to mark.'

def mark_confirmed_alert():
    if radar_detector['band']:
        radar_detector['known_alerts'].append({
            'band':  radar_detector['band'],
            'road':  road_memory[current_road]['name'] if current_road else 'unknown',
            'time':  datetime.now().strftime('%I:%M %p'),
        })
        save_state()
        return f'Location saved. {radar_detector["band"]} confirmed at this spot.'
    return 'No active alert to confirm.'

def check_known_radar_spots():
    current = road_memory[current_road]['name'] if current_road else ''
    spots   = [s for s in radar_detector['known_alerts'] if s.get('road') == current]
    if spots:
        bands = list(set(s['band'] for s in spots))
        return f'Heads up. Known radar on {current}. {", ".join(bands)} band{"s" if len(bands)>1 else ""}.'
    return None

def show_radar_log():
    log = radar_detector['alert_log']
    if not log:
        return 'No radar alerts this session.'
    print('\n── RADAR ALERT LOG ──────────────────────')
    for e in log[-15:]:
        print(f'  [{e["time"]}] {e["band"]} — {e["strength"]} bars — {e["direction"]} — {e["road"]}')
    print(f'\n  Session total: {radar_detector["alert_count_session"]} alerts')
    print('─────────────────────────────────────────\n')
    return f'{radar_detector["alert_count_session"]} radar alerts this session.'

# ══════════════════════════════════════════
# SPEED TRAP / HAZARD MEMORY
# ══════════════════════════════════════════
hazard_map = {
    'speed_traps':   [],    # {road, location_desc, band, confirmed, times_seen}
    'potholes':      [],    # {road, location_desc, severity, date}
    'cameras':       [],    # {road, location_desc, type}
    'low_clearance': [],    # {road, location_desc, height_ft}
    'dangerous':     [],    # {road, location_desc, reason}
    'construction':  [],    # {road, location_desc, active}
}

def add_hazard(htype, location_desc, **kwargs):
    road = road_memory[current_road]['name'] if current_road else 'unknown'
    entry = {
        'road':         road,
        'location_desc': location_desc,
        'date':         datetime.now().strftime('%B %d %Y'),
        **kwargs
    }
    hazard_map[htype].append(entry)
    save_state()
    return f'{htype.replace("_"," ").title()} logged on {road}. {location_desc}.'

def check_hazards_on_road(road_name):
    alerts = []
    for htype, entries in hazard_map.items():
        matches = [e for e in entries if road_name.lower() in e.get('road','').lower()]
        for m in matches:
            alerts.append(f'{htype.replace("_"," ")}: {m["location_desc"]}')
    return alerts

# ══════════════════════════════════════════
# SPEED LIMITER BY PROFILE
# ══════════════════════════════════════════
speed_limits = {
    'ayden':     None,      # no limit
    'girlfriend': 85,
    'family':    70,
    'valet':     40,
}

def check_speed_limit():
    profile_key = current_profile
    limit = speed_limits.get(profile_key)
    if limit and sensor_data.get('speed_mph', 0) > limit:
        return f'Speed limit for this profile is {limit} MPH. Ease up.'
    return None

# ══════════════════════════════════════════
# CURFEW MODE
# ══════════════════════════════════════════
curfew = {
    'enabled':    False,
    'hour':       24,       # midnight default
    'warned':     False,
    'override':   False,
}

def check_curfew():
    if not curfew['enabled'] or curfew['override']:
        return
    hour = datetime.now().hour
    if hour >= curfew['hour'] and not curfew['warned']:
        curfew['warned'] = True
        speak(f'It is past {curfew["hour"]}. Head home.')

def set_curfew(hour):
    curfew['enabled'] = True
    curfew['hour']    = int(hour)
    curfew['warned']  = False
    save_state()
    return f'Curfew set for {hour}:00.'

# ══════════════════════════════════════════
# 60-0 BRAKE TEST
# ══════════════════════════════════════════
brake_test = {
    'active':        False,
    'start_speed':   60,
    'start_time':    None,
    'stop_time':     None,
    'distance_ft':   None,
    'best_distance': None,
    'runs':          [],
}

def start_brake_test():
    brake_test['active']     = True
    brake_test['start_time'] = time.time()
    speak('Brake test started. Sixty MPH then stop.')
    return None

def _check_brake_test():
    if brake_test['active'] and brake_test['start_time']:
        elapsed = time.time() - brake_test['start_time']
        speed   = sensor_data.get('speed_mph', 60)
        if speed <= 2 and elapsed > 1:
            brake_test['active']    = False
            brake_test['stop_time'] = time.time()
            # Estimate distance from time (rough: 60mph stop ~120ft)
            dist = round(elapsed * 18)
            brake_test['distance_ft'] = dist
            run = {'distance_ft': dist, 'time_s': round(elapsed,2), 'date': datetime.now().strftime('%B %d %Y')}
            brake_test['runs'].append(run)
            if brake_test['best_distance'] is None or dist < brake_test['best_distance']:
                brake_test['best_distance'] = dist
                speak(f'New best stop. {dist} feet.')
            else:
                speak(f'Stopped in {dist} feet.')

# ══════════════════════════════════════════
# CONSISTENT ET PREDICTOR
# ══════════════════════════════════════════
def predict_et():
    temp   = weather['temp']
    humid  = weather.get('humidity', 50)
    eth    = truck_state['ethanol']
    boost  = truck_state['boost']

    # Base ET for LSA-swapped Sierra (~12.8 stock tune)
    base_et = 12.8

    # Temperature correction — colder is faster
    temp_factor = (temp - 60) * 0.007
    base_et += temp_factor

    # Ethanol boost
    eth_factor = -((eth - 50) / 100) * 0.4
    base_et += eth_factor

    # Boost level
    boost_factor = -(boost / 15) * 0.3
    base_et += boost_factor

    # Humidity penalty
    humid_factor = (humid - 50) / 100 * 0.1
    base_et += humid_factor

    predicted = round(base_et, 2)
    return f'Predicted ET today: {predicted} seconds. Based on {temp}F, E{eth}, {boost} PSI boost.'

# ══════════════════════════════════════════
# PERSONAL RECORD WALL
# ══════════════════════════════════════════
record_wall = {
    'best_et':           None,
    'best_et_date':      None,
    'best_060':          None,
    'best_060_date':     None,
    'best_60ft':         None,
    'best_60ft_date':    None,
    'best_trap_mph':     None,
    'best_trap_date':    None,
    'best_60_0':         None,
    'best_60_0_date':    None,
    'highest_boost':     None,
    'highest_rpm':       None,
    'highest_speed':     None,
    'total_runs':        0,
    'total_miles':       0,
}

def update_record(key, value, date=None):
    date = date or datetime.now().strftime('%B %d %Y')
    current = record_wall.get(key)
    if current is None or value < current:
        record_wall[key]              = value
        record_wall[key + '_date']    = date
        save_state()
        return True
    return False

def show_records():
    print('\n── PERSONAL RECORD WALL ─────────────────')
    records = [
        ('Best ET',       record_wall['best_et'],       record_wall['best_et_date'],       's'),
        ('Best 0-60',     record_wall['best_060'],      record_wall['best_060_date'],      's'),
        ('Best 60ft',     record_wall['best_60ft'],     record_wall['best_60ft_date'],     's'),
        ('Trap Speed',    record_wall['best_trap_mph'], record_wall['best_trap_date'],     'MPH'),
        ('Best 60-0',     record_wall['best_60_0'],     record_wall['best_60_0_date'],     'ft'),
        ('Highest Boost', record_wall['highest_boost'], None,                               'PSI'),
        ('Highest RPM',   record_wall['highest_rpm'],   None,                               'RPM'),
        ('Top Speed',     record_wall['highest_speed'], None,                               'MPH'),
    ]
    for name, val, date, unit in records:
        if val is not None:
            d = f' — {date}' if date else ''
            print(f'  {name:<14} {val} {unit}{d}')
        else:
            print(f'  {name:<14} --')
    print(f'\n  Total runs: {record_wall["total_runs"]}')
    print('─────────────────────────────────────────\n')
    if record_wall['best_et']:
        return f'Best ET {record_wall["best_et"]}s. Best 0-60 {record_wall["best_060"]}s. Top speed {record_wall["highest_speed"]} MPH.'
    return 'No records set yet. Time to make some runs.'

# ══════════════════════════════════════════
# TRAILER / TOW MODE
# ══════════════════════════════════════════
trailer = {
    'connected':      False,
    'est_weight_lbs': 0,
    'type':           '',       # flatbed, enclosed, boat, dump
    'brake_gain':     5,        # 1-10
    'sway_detected':  False,
    'tow_rating_lbs': 13000,    # Sierra 2500HD rating
    'tongue_weight':  0,
}

def connect_trailer(ttype='', weight=0):
    trailer['connected']      = True
    trailer['type']           = ttype or 'trailer'
    trailer['est_weight_lbs'] = int(weight)
    # Switch to tow tune
    set_air_height('tow')
    truck_state['exhaust'] = 15
    save_state()
    msg = f'Trailer connected. {ttype}. {weight} lbs estimated.'
    if int(weight) > trailer['tow_rating_lbs']:
        msg += f' WARNING — over tow rating of {trailer["tow_rating_lbs"]} lbs.'
    return msg

def disconnect_trailer():
    trailer['connected']      = False
    trailer['est_weight_lbs'] = 0
    set_air_height('drive')
    save_state()
    return 'Trailer disconnected. Returning to drive height.'

# ══════════════════════════════════════════
# MILEAGE / TAX LOG
# ══════════════════════════════════════════
mileage_log = {
    'trips':          [],
    'total_miles':    0,
    'business_miles': 0,
    'personal_miles': 0,
    'year':           datetime.now().year,
}

def log_mileage_trip(start, end, purpose='personal', notes=''):
    miles = abs(end - start)
    entry = {
        'start':   start,
        'end':     end,
        'miles':   miles,
        'purpose': purpose,
        'date':    datetime.now().strftime('%B %d %Y'),
        'notes':   notes,
    }
    mileage_log['trips'].append(entry)
    mileage_log['total_miles']    += miles
    if purpose == 'business':
        mileage_log['business_miles'] += miles
    else:
        mileage_log['personal_miles'] += miles
    save_state()
    return f'Trip logged. {miles} miles. {purpose}.'

def mileage_summary():
    irs_rate = 0.67  # 2024 IRS rate per mile
    deduction = mileage_log['business_miles'] * irs_rate
    return (f'Total: {mileage_log["total_miles"]} miles. '
            f'Business: {mileage_log["business_miles"]} miles. '
            f'Est deduction: ${deduction:.2f}.')

# ══════════════════════════════════════════
# RIVALRY MODE
# ══════════════════════════════════════════
rivalry = {
    'rival':          '',
    'rival_et':       None,
    'rival_mph':      None,
    'sessions':       [],
    'wins':           0,
    'losses':         0,
    'active':         False,
}

def set_rival(name, et=None, mph=None):
    rivalry['rival']    = name
    rivalry['rival_et'] = et
    rivalry['rival_mph']= mph
    rivalry['active']   = True
    save_state()
    if et:
        return f'Rival set: {name}. Their ET is {et}. We will beat it.'
    return f'Rival set: {name}. Time to chase them down.'

def check_rival_result(our_et):
    if not rivalry['rival'] or not rivalry['rival_et']:
        return None
    if our_et < rivalry['rival_et']:
        rivalry['wins'] += 1
        save_state()
        return f'You beat {rivalry["rival"]}. Their ET was {rivalry["rival_et"]}. You ran {our_et}.'
    else:
        rivalry['losses'] += 1
        save_state()
        diff = round(our_et - rivalry['rival_et'], 3)
        return f'Still chasing {rivalry["rival"]} by {diff} seconds. Keep working.'

# ══════════════════════════════════════════
# SARCASM / MOOD PERSONALITY ENGINE
# ══════════════════════════════════════════
archer_mood = {
    'mode':           'normal',   # normal, sarcastic, focused, chill, hype
    'repeat_count':   {},         # track repeated questions
    'last_commands':  [],
    'aggressive_pct': 0,          # % of drive that was aggressive
}

def get_sarcasm_level(command):
    cmd = command.lower()
    count = archer_mood['repeat_count'].get(cmd, 0) + 1
    archer_mood['repeat_count'][cmd] = count
    return count

def should_be_sarcastic(command):
    level = get_sarcasm_level(command)
    return level >= 3 and archer_mood['mode'] in ['normal', 'sarcastic']

SARCASTIC_RESPONSES = {
    'weather':    ['Still {temp}F. Still {condition}. Has not changed in the last 30 seconds.', 'Same weather. Still {temp}F.'],
    'rpm':        ['Still {rpm}. You have asked {count} times.', 'RPM has not changed. Still {rpm}.'],
    'boost':      ['Boost is {boost}. Same as before.', 'Still {boost} PSI. Are you watching the display?'],
    'default':    ['Asked and answered.', 'Same answer as last time.', 'You sure you do not have the display open?'],
}

# ══════════════════════════════════════════
# PASSENGER / GIRLFRIEND MODE
# ══════════════════════════════════════════
passenger_mode = {
    'active':         False,
    'name':           'Girlfriend',
    'preferences':    {
        'music':      True,
        'temp':       72,
        'no_exhaust': True,
        'no_shows':   False,
    }
}

def activate_passenger_mode(name=''):
    passenger_mode['active'] = True
    passenger_mode['name']   = name or 'Girlfriend'
    # Auto adjust comfort settings
    truck_state['exhaust']   = 0 if passenger_mode['preferences']['no_exhaust'] else truck_state['exhaust']
    truck_state['heat_on']   = True
    truck_state['temp_setting'] = passenger_mode['preferences']['temp']
    save_state()
    return f'Passenger mode on. {passenger_mode["name"]} is in the truck. Keeping it comfortable.'

def deactivate_passenger_mode():
    passenger_mode['active'] = False
    save_state()
    return f'{passenger_mode["name"]} dropped off. Back to normal.'

# ══════════════════════════════════════════
# DAILY GREETING ENGINE
# ══════════════════════════════════════════
def get_daily_greeting():
    hour = datetime.now().hour
    temp = weather['temp']
    cond = weather['condition']

    if hour < 6:
        base = 'Late night.'
    elif hour < 12:
        base = 'Morning.'
    elif hour < 17:
        base = 'Afternoon.'
    elif hour < 21:
        base = 'Evening.'
    else:
        base = 'Night.'

    # Add weather context
    if temp < 32:
        base += f' Freezing out. {temp}F.'
    elif temp > 90:
        base += f' Hot one. {temp}F.'
    else:
        base += f' {temp}F out.'

    # Add condition
    if 'rain' in cond.lower():
        base += ' Raining. Roads are wet.'
    elif 'snow' in cond.lower():
        base += ' Snow on the ground. Take it easy.'
    elif 'clear' in cond.lower() and temp > 40 and temp < 75:
        base += ' Good day to run.'

    # Add record check
    if record_wall['best_et']:
        base += f' Best ET still {record_wall["best_et"]}s.'

    return base

# ══════════════════════════════════════════
# INSURANCE / VEHICLE INFO STORAGE
# ══════════════════════════════════════════
vehicle_info = {
    'vin':            '1GTHK23U06F000000',  # placeholder
    'plate':          '',
    'year':           2006,
    'make':           'GMC',
    'model':          'Sierra 2500HD',
    'color':          'Matte Black',
    'insurance_co':   '',
    'policy_num':     '',
    'agent_phone':    '',
    'registered_to':  'Ayden',
    'state':          'Missouri',
}

# ══════════════════════════════════════════
# WEATHER RADAR FEED
# ══════════════════════════════════════════
def get_weather_radar_url():
    # NWS radar for Salem MO area — Springfield MO radar
    return 'https://radar.weather.gov/ridge/standard/KSGF_loop.gif'

def get_detailed_weather():
    try:
        # Reuse the latest NWS observation already in weather dict
        w = weather
        result = f'{w["temp"]}F, wind {w["wind"]} MPH, {w["condition"]}'
        if w['raining']:  result += ', precipitation'
        if w['freezing']: result += ', below freezing'
        return result
    except:
        return f'{weather["temp"]}F {weather.get("desc") or weather["condition"]}'


# ══════════════════════════════════════════
# EMERGENCY CONTACTS
# ══════════════════════════════════════════
emergency_contacts = {
    'primary':   {'name': '', 'phone': '', 'relation': ''},
    'secondary': {'name': '', 'phone': '', 'relation': ''},
    'insurance': {'company': '', 'policy': '', 'phone': ''},
    'roadside':  {'name': 'AAA', 'phone': '1-800-222-4357'},
    'tow':       {'name': '', 'phone': ''},
}

def show_emergency_info():
    p = emergency_contacts['primary']
    s = emergency_contacts['secondary']
    print('\n── EMERGENCY CONTACTS ───────────────────')
    if p['name']:
        print(f'  Primary  : {p["name"]} ({p["relation"]}) — {p["phone"]}')
    if s['name']:
        print(f'  Secondary: {s["name"]} ({s["relation"]}) — {s["phone"]}')
    ins = emergency_contacts['insurance']
    if ins['company']:
        print(f'  Insurance: {ins["company"]} — Policy {ins["policy"]} — {ins["phone"]}')
    print(f'  Roadside : {emergency_contacts["roadside"]["name"]} — {emergency_contacts["roadside"]["phone"]}')
    print('─────────────────────────────────────────\n')
    if p['name']:
        return f'Primary contact is {p["name"]} at {p["phone"]}.'
    return 'No emergency contacts set. Say set emergency contact to add one.'

def set_emergency_contact(slot, name, phone, relation=''):
    if slot in emergency_contacts:
        emergency_contacts[slot].update({'name': name, 'phone': phone, 'relation': relation})
        save_state()
        return f'{slot.title()} emergency contact set to {name} at {phone}.'
    return 'Slots: primary secondary tow.'

# ══════════════════════════════════════════
# CRASH / IMPACT DETECTION
# ══════════════════════════════════════════
crash_detection = {
    'enabled':       True,
    'threshold_g':   3.0,      # G-force threshold to trigger
    'last_event':    None,
    'events':        [],
    'countdown':     False,
}

def check_crash():
    if not crash_detection['enabled']:
        return
    g = abs(sensor_data.get('accel_y', 0)) + abs(sensor_data.get('accel_x', 0))
    if g >= crash_detection['threshold_g']:
        event = {
            'time':   datetime.now().strftime('%I:%M %p'),
            'g':      round(g, 2),
            'speed':  sensor_data.get('speed_mph', 0),
            'road':   road_memory[current_road]['name'] if current_road else 'unknown',
        }
        crash_detection['events'].append(event)
        crash_detection['last_event'] = event
        speak(f'Impact detected. {round(g, 1)} G. Are you okay?')
        show_emergency_info()

# ══════════════════════════════════════════
# DROWSY DRIVING DETECTION
# ══════════════════════════════════════════
drowsy_monitor = {
    'enabled':        True,
    'start_hour':     22,     # start watching after 10PM
    'check_interval': 300,    # 5 min
    'last_check':     None,
    'alerts_sent':    0,
}

def check_drowsy():
    if not drowsy_monitor['enabled']:
        return
    hour = datetime.now().hour
    if hour < drowsy_monitor['start_hour'] and hour > 5:
        return
    rpm  = truck_state['rpm']
    # If engine running, steady low RPM for a while late at night — check in
    if 600 < rpm < 900 and drowsy_monitor['alerts_sent'] < 3:
        drowsy_monitor['alerts_sent'] += 1
        speak('Hey. Still with me?')

# ══════════════════════════════════════════
# DRIVE MEMORY — WITHIN SESSION
# ══════════════════════════════════════════
drive_memory = {
    'events':        [],     # what happened this drive
    'roads_visited': [],
    'peak_rpm_road': {},
    'notes':         [],
    'started_at':    None,
    'mood_at_start': '',
}

def log_drive_event(event, category='general'):
    entry = {
        'event':    event,
        'category': category,
        'time':     datetime.now().strftime('%I:%M %p'),
        'road':     road_memory[current_road]['name'] if current_road else 'unknown',
        'rpm':      truck_state['rpm'],
    }
    drive_memory['events'].append(entry)
    if len(drive_memory['events']) > 100:
        drive_memory['events'].pop(0)

def recall_drive():
    events = drive_memory['events']
    if not events:
        return 'Nothing logged yet this drive.'
    # Summarize last 10 events
    recent = events[-10:]
    parts  = []
    for e in recent:
        parts.append(f'{e["time"]}: {e["event"]}')
    return 'This drive so far — ' + '. '.join(parts[-3:]) + '.'

def drive_summary_ai():
    events = drive_memory['events']
    if not events:
        return 'Clean drive. Nothing notable happened.'
    hard  = [e for e in events if e['category'] == 'performance']
    roads = list(set(e['road'] for e in events if e['road'] != 'unknown'))
    notes = drive_memory['notes']
    parts = []
    if hard:  parts.append(f'{len(hard)} performance events')
    if roads: parts.append(f'roads: {", ".join(roads[:3])}')
    if notes: parts.append(f'{len(notes)} notes saved')
    return 'Drive summary — ' + '. '.join(parts) + '.' if parts else 'Quiet drive.'

# ══════════════════════════════════════════
# DRIVE SCORE / REPORT CARD
# ══════════════════════════════════════════
def calculate_drive_score():
    """Calculate comprehensive vehicle health/drive score (0-100) with letter grade."""
    score   = 100
    details = []

    # ── Battery ──────────────────────────────────────────────
    bat = truck_state.get('battery_main', 13.8)
    if bat < 11.5:
        score -= 25
        details.append(f'-25 critical battery ({bat}V)')
    elif bat < 12.0:
        score -= 15
        details.append(f'-15 low battery ({bat}V)')
    elif bat < 12.5:
        score -= 8
        details.append(f'-8 weak battery ({bat}V)')

    # ── Active DTC Codes — up to 3 count (-15 each) ──────────
    active_dtcs = [f for f in fault_codes if f.get('status', 'active') == 'active']
    dtc_penalty = min(3, len(active_dtcs)) * 15
    if dtc_penalty > 0:
        score -= dtc_penalty
        details.append(f'-{dtc_penalty} active DTCs ({len(active_dtcs)} codes)')

    # ── Coolant Temperature ───────────────────────────────────
    cool = truck_state.get('coolant_temp', 190)
    if cool > 240:
        score -= 20
        details.append(f'-20 overheating coolant ({cool}°F)')
    elif cool > 220:
        score -= 10
        details.append(f'-10 high coolant temp ({cool}°F)')
    elif cool > 210:
        score -= 4
        details.append(f'-4 elevated coolant ({cool}°F)')

    # ── Oil Temperature ───────────────────────────────────────
    oil = truck_state.get('oil_temp', 195)
    if oil > 250:
        score -= 15
        details.append(f'-15 overheated oil ({oil}°F)')
    elif oil > 230:
        score -= 8
        details.append(f'-8 high oil temp ({oil}°F)')

    # ── Oil Life ──────────────────────────────────────────────
    oil_life = truck_state.get('oil_life', 100)
    if oil_life < 10:
        score -= 15
        details.append(f'-15 critical oil life ({oil_life}%)')
    elif oil_life < 20:
        score -= 8
        details.append(f'-8 low oil life ({oil_life}%)')
    elif oil_life < 35:
        score -= 3
        details.append(f'-3 oil change soon ({oil_life}%)')

    # ── Knock Events ──────────────────────────────────────────
    knock = sensor_data.get('knock_count', 0)
    if knock > 0:
        deduct = min(20, knock * 4)
        score -= deduct
        details.append(f'-{deduct} knock events')

    # ── Ethanol / Boost Safety ────────────────────────────────
    eth   = truck_state.get('ethanol', 0)
    boost = truck_state.get('boost', 0)
    if eth < 40 and boost > 8:
        score -= 20
        details.append('-20 low ethanol under boost')

    # ── Bonuses ───────────────────────────────────────────────
    if eth > 75:
        score += 5
        details.append('+5 good ethanol mix')
    if knock == 0 and boost > 5:
        score += 5
        details.append('+5 clean boost run')
    if bat >= 13.5 and len(active_dtcs) == 0:
        score += 3
        details.append('+3 all systems nominal')

    score = max(0, min(100, score))
    grade = 'A' if score >= 90 else 'B' if score >= 80 else 'C' if score >= 70 else 'D' if score >= 60 else 'F'
    detail_str = ' | '.join(details) if details else 'No issues found.'
    return score, grade, detail_str

def show_drive_score():
    score, grade, details = calculate_drive_score()
    return f'Drive score: {score}/100 — Grade {grade}. {details}'

# ══════════════════════════════════════════
# GEOFENCE
# ══════════════════════════════════════════
geofences = []

def add_geofence(name, lat, lon, radius_miles=0.5):
    geofences.append({
        'name':         name,
        'lat':          lat,
        'lon':          lon,
        'radius_miles': radius_miles,
        'active':       True,
        'notify_enter': True,
        'notify_exit':  True,
    })
    save_state()
    return f'Geofence added: {name}. {radius_miles} mile radius.'

def check_geofences(current_lat, current_lon):
    import math
    alerts = []
    for fence in geofences:
        if not fence['active']:
            continue
        dlat = abs(current_lat - fence['lat']) * 69
        dlon = abs(current_lon - fence['lon']) * 69 * math.cos(math.radians(fence['lat']))
        dist = math.sqrt(dlat**2 + dlon**2)
        if dist < fence['radius_miles']:
            alerts.append(fence['name'])
    return alerts

# ══════════════════════════════════════════
# NEAREST GAS STATION
# ══════════════════════════════════════════
SALEM_GAS_STATIONS = [
    {'name': 'Caseys General Store',   'address': 'Hwy 72 Salem MO',    'e85': True,  'distance': 0.5},
    {'name': 'Break Time',               'address': 'Main St Salem MO',   'e85': False, 'distance': 0.8},
    {'name': 'Fast Lane',                'address': 'Hwy 32 Salem MO',    'e85': False, 'distance': 1.1},
    {'name': 'Walmart Gas',              'address': 'Walmart Dr Salem MO','e85': False, 'distance': 1.4},
    {'name': 'Phillips 66',              'address': 'Hwy 19 Salem MO',    'e85': True,  'distance': 2.1},
]

def find_nearest_gas(e85_only=False):
    stations = SALEM_GAS_STATIONS
    if e85_only:
        stations = [s for s in stations if s['e85']]
    if not stations:
        return 'No E85 stations found nearby.'
    nearest = sorted(stations, key=lambda s: s['distance'])
    top = nearest[0]
    result = f'Nearest{"  E85" if e85_only else ""}: {top["name"]} — {top["distance"]} miles — {top["address"]}.'
    if len(nearest) > 1:
        result += f' Also: {nearest[1]["name"]} at {nearest[1]["distance"]} miles.'
    return result

# ══════════════════════════════════════════
# SHOW MODE AUTOMATION SEQUENCE
# ══════════════════════════════════════════
show_sequence = {
    'running':  False,
    'step':     0,
    'name':     '',
}

SHOW_SEQUENCES = {
    'welcome': [
        {'action': 'headlights', 'value': True,  'delay': 0.5,  'say': 'Welcome.'},
        {'action': 'exhaust',    'value': 50,     'delay': 1.0,  'say': None},
        {'action': 'underglow',  'value': True,   'delay': 0.5,  'say': None},
        {'action': 'rpm',        'value': 1500,   'delay': 1.0,  'say': None},
        {'action': 'rpm',        'value': 750,    'delay': 1.5,  'say': None},
        {'action': 'exhaust',    'value': 30,     'delay': 0.5,  'say': None},
    ],
    'show_entry': [
        {'action': 'air',        'value': 'slam', 'delay': 3.0,  'say': 'Dropping it.'},
        {'action': 'headlights', 'value': True,   'delay': 1.0,  'say': None},
        {'action': 'exhaust',    'value': 100,    'delay': 0.5,  'say': None},
        {'action': 'rpm',        'value': 2000,   'delay': 1.0,  'say': 'Show mode.'},
        {'action': 'rpm',        'value': 750,    'delay': 2.0,  'say': None},
        {'action': 'exhaust',    'value': 0,      'delay': 0.5,  'say': None},
    ],
    'departure': [
        {'action': 'rpm',        'value': 1200,   'delay': 0.5,  'say': None},
        {'action': 'exhaust',    'value': 80,     'delay': 0.5,  'say': 'Later.'},
        {'action': 'headlights', 'value': False,  'delay': 1.0,  'say': None},
        {'action': 'air',        'value': 'drive','delay': 2.0,  'say': None},
        {'action': 'rpm',        'value': 750,    'delay': 0.5,  'say': None},
        {'action': 'exhaust',    'value': 30,     'delay': 0,    'say': None},
    ],
}

def run_show_sequence(name):
    if name not in SHOW_SEQUENCES:
        return f'Sequences: {", ".join(SHOW_SEQUENCES.keys())}.'
    show_sequence['running'] = True
    show_sequence['name']    = name
    import threading as _t
    def _run():
        for step in SHOW_SEQUENCES[name]:
            if not show_sequence['running']:
                break
            action = step['action']
            value  = step['value']
            delay  = step['delay']
            if action == 'headlights': truck_state['headlights'] = value
            elif action == 'exhaust':  truck_state['exhaust']    = value
            elif action == 'rpm':      truck_state['rpm']        = value
            elif action == 'air':      set_air_height(value)
            elif action == 'underglow': ambient_lighting['zones']['underglow']['on'] = value
            if step.get('say'): speak(step['say'])
            if delay > 0: time.sleep(delay)
        show_sequence['running'] = False
    _t.Thread(target=_run, daemon=True).start()
    return None

def stop_show_sequence():
    show_sequence['running'] = False
    return 'Sequence stopped.'

# ══════════════════════════════════════════
# AUTO FEATURES — LIGHTS / MIRRORS / WINDOWS
# ══════════════════════════════════════════
auto_features = {
    'auto_lights':        True,    # headlights on when dark
    'auto_mirror_fold':   True,    # fold mirrors when parked
    'auto_windows_rain':  False,   # close windows when rain detected
    'auto_lock':          True,    # lock when driving
    'dim_threshold':      30,      # lux level to trigger auto lights (simulated)
    'seat_memory':        {
        'ayden':     {'position': 8, 'lumbar': 3, 'recline': 15},
        'girlfriend': {'position': 6, 'lumbar': 2, 'recline': 20},
    },
    'mirror_positions':   {
        'ayden':     {'left_ud': 45, 'left_lr': 50, 'right_ud': 45, 'right_lr': 48},
        'girlfriend': {'left_ud': 42, 'left_lr': 52, 'right_ud': 42, 'right_lr': 50},
    },
}

def load_seat_and_mirrors(profile_name):
    name = profile_name.lower()
    if name in auto_features['seat_memory']:
        seat    = auto_features['seat_memory'][name]
        mirrors = auto_features['mirror_positions'].get(name, {})
        return f'Seat position {seat["position"]} loaded. Mirrors adjusted for {profile_name}.'
    return None

def auto_check_lights():
    hour = datetime.now().hour
    if auto_features['auto_lights']:
        # Auto headlights — dawn/dusk based on time
        should_be_on = hour < 7 or hour > 19
        if should_be_on and not truck_state['headlights'] and truck_state['rpm'] > 0:
            truck_state['headlights'] = True
            speak('Headlights on.')

def auto_check_windows_rain():
    if auto_features['auto_windows_rain']:
        if weather.get('raining') and truck_state['windows']['fl'] == 'down':
            truck_state['windows'] = {k: 'up' for k in truck_state['windows']}
            speak('Rain detected. Closing windows.')

# ══════════════════════════════════════════
# QR CODE SPEC SHEET
# ══════════════════════════════════════════
def get_spec_sheet_url():
    base = public_url.get('url', 'http://localhost:5001')
    return f'{base}/specs'

def get_spec_data():
    return {
        'vehicle':    f'{vehicle_info["year"]} {vehicle_info["make"]} {vehicle_info["model"]}',
        'color':      vehicle_info['color'],
        'engine':     ('LSA 6.2L Supercharged' if get_build_caps()['supercharged'] else '6.0L LQ4 V8'),
        'trans':      '4L80E Full Rebuild',
        'suspension': 'Full Four Corner Air Ride',
        'wheels':     'Fuel D622 20x8.5 Matte Black',
        'brakes':     'Baer 6S Front and Rear',
        'hp_est':     calc_hp_estimate(truck_state['ethanol'], truck_state['boost']),
        'ethanol':    truck_state['ethanol'],
        'best_et':    record_wall['best_et'],
        'best_060':   record_wall['best_060'],
        'top_speed':  record_wall['highest_speed'],
        'total_mods': len(build_tracker['mods']),
        'build_year': build_tracker['build_start'],
    }

# ══════════════════════════════════════════
# WEATHER ALERTS
# ══════════════════════════════════════════
weather_alerts = {
    'tornado_watch':  False,
    'tornado_warn':   False,
    'severe_storm':   False,
    'flash_flood':    False,
    'ice_storm':      False,
    'last_check':     None,
}

def check_weather_alerts():
    try:
        # NWS alerts API for Salem MO (Dent County)
        url = 'https://api.weather.gov/alerts/active?zone=MOZ093'
        with urllib.request.urlopen(url, timeout=5) as r:
            data     = json.loads(r.read())
            features = data.get('features', [])
            if features:
                for f in features:
                    props = f.get('properties', {})
                    event = props.get('event', '').lower()
                    if 'tornado warning' in event:
                        weather_alerts['tornado_warn'] = True
                        speak('TORNADO WARNING for Salem area. Take shelter now.')
                    elif 'tornado watch' in event:
                        weather_alerts['tornado_watch'] = True
                        speak('Tornado watch in effect for Salem.')
                    elif 'severe thunderstorm' in event:
                        weather_alerts['severe_storm'] = True
                        speak('Severe thunderstorm warning active.')
                    elif 'flash flood' in event:
                        weather_alerts['flash_flood'] = True
                        speak('Flash flood warning. Avoid low roads.')
            else:
                # Clear all alerts
                for k in weather_alerts:
                    if k != 'last_check':
                        weather_alerts[k] = False
            weather_alerts['last_check'] = datetime.now().strftime('%I:%M %p')
    except:
        pass

def weather_alert_monitor():
    while True:
        check_weather_alerts()
        time.sleep(300)   # check every 5 min

# ══════════════════════════════════════════
# BLUETOOTH PROFILE AUTO-DETECTION
# ══════════════════════════════════════════
bluetooth_profiles = {}   # mac -> profile_key

def link_bluetooth(mac, profile_key):
    bluetooth_profiles[mac.upper()] = profile_key
    save_state()
    return f'Bluetooth {mac} linked to {profile_key} profile.'

def check_bluetooth_device(mac):
    key = bluetooth_profiles.get(mac.upper())
    if key and key in driver_profiles:
        return key
    return None

# ══════════════════════════════════════════
# TOW DETECTION
# ══════════════════════════════════════════
tow_detection = {
    'enabled':       True,
    'threshold_mph': 2,
    'detected':      False,
    'alert_sent':    False,
}

def check_tow_detection():
    if not tow_detection['enabled']:
        return
    if parking_mode['active'] and not tow_detection['alert_sent']:
        speed = sensor_data.get('speed_mph', 0)
        if speed > tow_detection['threshold_mph'] and truck_state['rpm'] == 0:
            tow_detection['detected']  = True
            tow_detection['alert_sent'] = True
            speak('TOW ALERT. Vehicle moving without engine. Possible tow.')

# ══════════════════════════════════════════
# OFFLINE AI FALLBACK IMPROVEMENTS
# ══════════════════════════════════════════
SMART_FALLBACKS = {
    'weather':     lambda: f'{weather["temp"]}F and {weather.get("desc") or weather["condition"]} in {location_data.get("location_name") or "your area"}.',
    'rpm':         lambda: f'RPM is at {truck_state["rpm"]}.',
    'boost':       lambda: (f'Boost is {truck_state["boost"]} PSI.' if get_build_caps()['supercharged'] else 'No forced induction. Stock six liter, naturally aspirated.'),
    'oil':         lambda: f'Oil temp is {truck_state["oil_temp"]}F.',
    'battery':     lambda: f'Battery at {truck_state["battery_main"]}V.',
    'ethanol':     lambda: (f'Ethanol at {truck_state["ethanol"]} percent.' if get_build_caps()['ethanol_sensor'] else 'No ethanol sensor installed yet. Running pump gas.'),
    'exhaust':     lambda: f'Exhaust is at {truck_state["exhaust"]} percent.',
    'trans':       lambda: f'Trans temp is {sensor_data.get("trans_temp", 160)}F. {"Running hot." if sensor_data.get("trans_temp", 160) > 200 else "Nominal."}',
    'transmission':lambda: f'Trans temp is {sensor_data.get("trans_temp", 160)}F. {"Running hot." if sensor_data.get("trans_temp", 160) > 200 else "Nominal."}',
    'coolant':     lambda: f'Coolant is {truck_state.get("coolant_temp", truck_state["oil_temp"])}F.',
    'intake':      lambda: f'Intake temp is {truck_state.get("intake_temp", 70)}F.',
    'status':      lambda: (f'Everything looks good. {truck_state["rpm"]} RPM, {truck_state["boost"]} PSI, oil at {truck_state["oil_temp"]}F.' if get_build_caps()['supercharged'] else f'Everything looks good. {truck_state["rpm"]} RPM, oil at {truck_state["oil_temp"]}F.'),
    'score':       lambda: show_drive_score(),
    'health':      lambda: archer_diagnostics(),
    'records':     lambda: show_records(),
    'fuel':        lambda: f'{fuel_tank["current_gal"]:.1f} gallons remaining. About {fuel_tank["range_est"]} miles.',
    'maintenance': lambda: check_maintenance(),
}

def smart_fallback(text):
    t   = text.lower()
    oil = truck_state['oil_temp']
    rpm = truck_state['rpm']
    eth = truck_state['ethanol']
    spd = truck_state['speed']
    mood = get_mood()
    for key, fn in SMART_FALLBACKS.items():
        if key in t:
            return fn()
    if any(w in t for w in ['how are you', "how's it", "how you doing", "what's up", "sup", "you good", 'you okay', 'doing okay']):
        return f"Oil at {oil}. Running clean. Ready when you are."
    if any(w in t for w in ['rough day', 'bad day', 'tough day']):
        return "Yeah. Just drive for a bit."
    if any(w in t for w in ['good drive', 'great drive', 'nice drive']):
        return "Yeah. Good one."
    if any(w in t for w in ['push it again', 'one more time', 'one more run']):
        return "Don't chase it."
    if any(w in t for w in ['what are you', 'who are you', 'what is this']):
        return ("408 cubic inches. Supercharged. E85. Built by Ayden." if get_build_caps()['supercharged']
                else "364 cubic inches. Stock six liter. Built by Ayden. LSA swap is coming.")
    if any(w in t for w in ['are you alive', 'are you real', 'are you there']):
        return "Close enough."
    if any(w in t for w in ['thanks', 'thank you', 'good job', 'nice work', 'appreciate']):
        return random.choice(["Anytime.", "Yeah.", "That's what I'm here for.", "Copy that."])
    if any(w in t for w in ['hello', 'hey archer', 'hi archer', 'yo archer']):
        return random.choice(["What's up.", "Ready when you are.", "Here. What do you need?"])
    if any(w in t for w in ['bored', 'nothing to do']):
        return (f"Tank is at {eth} percent E85. That should fix that." if get_build_caps()['ethanol_sensor']
                else "Running pump gas. E85 comes with the swap.")
    if any(w in t for w in ['what can you do', 'what do you know', 'help']):
        return "Ask me about RPM, temps, weather, fuel, music, or just talk."
    if mood == 'hyped':
        return f"Pulling hard right now. {rpm} RPM. Everything is good."
    if mood == 'caring':
        return "Watching everything. Nothing to worry about."
    if spd > 50:
        return f"{spd} mph. Road is clear."
    return random.choice([
        'Say that again.',
        'Not sure what you mean.',
        'Try asking differently.',
        f'I heard you. Truck status: {rpm} RPM, {oil}F oil.',
    ])


# ══════════════════════════════════════════
# OPENCLAW INTEGRATION
# ══════════════════════════════════════════
openclaw = {
    'enabled':    False,
    'url':        'http://localhost:18789',   # default OpenClaw gateway port
    'api_key':    '',
    'connected':  False,
    'last_task':  None,
    'task_log':   [],
    'tier_access': [1, 2],   # Tiers that can use OpenClaw
    'model':      'ollama/llama3.2',  # runs local — no API key needed
}

# Tasks Tier 2 is allowed to use
OPENCLAW_TIER2_ALLOWED = [
    'weather', 'news', 'search', 'remind', 'message',
    'text', 'whatsapp', 'find', 'what is', 'look up',
]

def openclaw_check_connection():
    try:
        with urllib.request.urlopen(f'{openclaw["url"]}/health', timeout=3) as r:
            if r.status == 200:
                openclaw['connected'] = True
                return True
    except:
        pass
    openclaw['connected'] = False
    return False

def openclaw_task(task, tier=1):
    """Send a task to OpenClaw and return the result."""
    if not openclaw['enabled']:
        return None
    if tier not in openclaw['tier_access']:
        return 'OpenClaw access not available for your tier.'

    # Tier 2 filter — only allowed task types
    if tier == 2:
        allowed = any(kw in task.lower() for kw in OPENCLAW_TIER2_ALLOWED)
        if not allowed:
            return 'That task is not available in passenger mode.'

    if not openclaw_check_connection():
        return 'OpenClaw is not running. Start it with: npx clawdbot@latest'

    try:
        import json as _json
        payload = _json.dumps({
            'message': task,
            'model':   openclaw['model'],
        }).encode()

        req = urllib.request.Request(
            f'{openclaw["url"]}/api/message',
            data    = payload,
            headers = {
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {openclaw["api_key"]}' if openclaw['api_key'] else '',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = _json.loads(r.read())
            text   = result.get('text') or result.get('content') or result.get('message') or str(result)

            # Log the task
            entry = {
                'task':   task,
                'result': text[:200],
                'time':   datetime.now().strftime('%I:%M %p'),
                'tier':   tier,
            }
            openclaw['task_log'].append(entry)
            if len(openclaw['task_log']) > 50:
                openclaw['task_log'].pop(0)
            openclaw['last_task'] = entry

            print(f'[OPENCLAW] Task: {task[:60]}')
            print(f'[OPENCLAW] Result: {text[:120]}')
            return text

    except urllib.error.URLError as e:
        return f'OpenClaw connection failed: {str(e)[:60]}'
    except Exception as e:
        return f'OpenClaw error: {str(e)[:60]}'

def openclaw_monitor():
    """Background thread — checks OpenClaw connection every 60s."""
    while True:
        if openclaw['enabled']:
            was_connected = openclaw['connected']
            now_connected = openclaw_check_connection()
            if now_connected and not was_connected:
                print('[OPENCLAW] Connected.')
            elif not now_connected and was_connected:
                print('[OPENCLAW] Disconnected.')
        time.sleep(60)

def is_openclaw_task(text):
    """Detect if a command should be routed to OpenClaw instead of Archer."""
    keywords = [
        'check my email', 'read my email', 'send email', 'email',
        'check my messages', 'send a message', 'text', 'whatsapp',
        'search the web', 'look up', 'google', 'find online',
        'browse', 'open website', 'go to website',
        'remind me', 'set reminder', 'schedule',
        'download', 'post to', 'tweet', 'instagram',
        'order', 'buy', 'price check',
        'news', 'latest news', 'what is happening',
        'play music', 'pause music', 'next song',
        'control', 'automate', 'run script',
        'track my package', 'shipping',
        'check price', 'how much is',
        'calendar', 'what do i have today',
        'notification', 'alert me when',
    ]
    t = text.lower()
    return any(kw in t for kw in keywords)


# ══════════════════════════════════════════
# DISCORD NOTIFICATIONS
# ══════════════════════════════════════════
discord_config = {
    'enabled':          False,
    'webhook_alerts':   '',     # #alerts channel webhook
    'webhook_vitals':   '',     # #vitals channel webhook
    'webhook_radar':    '',     # #radar channel webhook
    'webhook_build':    '',     # #build channel webhook
    'notify_radar':     True,
    'notify_oil':       True,
    'notify_battery':   True,
    'notify_boost':     True,
    'notify_records':   True,
    'notify_valet':     True,
    'notify_crash':     True,
    'notify_weather':   True,
    'cooldown_secs':    60,     # min seconds between same alert type
    'last_sent':        {},     # alert_type -> timestamp
}

def discord_send(webhook_url, message, title='', color=0xCC0000):
    """Send a message to a Discord webhook."""
    if not webhook_url or not discord_config['enabled']:
        return False
    try:
        import json as _json
        payload = {
            'embeds': [{
                'title':       title or 'ARCHER',
                'description': message,
                'color':       color,
                'footer':      {'text': f'2006 GMC Sierra 2500HD — {datetime.now().strftime("%I:%M %p")}'},
            }]
        }
        data = _json.dumps(payload).encode()
        req  = urllib.request.Request(
            webhook_url,
            data    = data,
            headers = {'Content-Type': 'application/json'},
            method  = 'POST'
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status in [200, 204]
    except Exception as e:
        print(f'[DISCORD] Failed: {str(e)[:60]}')
        return False

def discord_alert(alert_type, message, title='', channel='alerts', color=0xCC0000):
    """Send alert with cooldown to prevent spam."""
    if not discord_config['enabled']:
        return
    now = time.time()
    last = discord_config['last_sent'].get(alert_type, 0)
    if now - last < discord_config['cooldown_secs']:
        return
    discord_config['last_sent'][alert_type] = now

    webhook = discord_config.get(f'webhook_{channel}') or discord_config['webhook_alerts']
    if not webhook:
        return

    discord_send(webhook, message, title, color)
    print(f'[DISCORD] Sent {alert_type} to #{channel}')

def discord_vitals():
    """Send current vitals snapshot to #vitals channel."""
    msg = (
        f'**RPM** {truck_state["rpm"]} | '
        f'**Boost** {truck_state["boost"]} PSI | '
        f'**Oil** {truck_state["oil_temp"]}F\n'
        f'**Battery** {truck_state["battery_main"]}V | '
        f'**E85** {truck_state["ethanol"]}% | '
        f'**Exhaust** {truck_state["exhaust"]}%\n'
        f'**Weather** {weather["temp"]}F {weather.get("desc") or weather["condition"]} | '
        f'**Score** {calculate_drive_score()[0]}/100'
    )
    discord_send(
        discord_config['webhook_vitals'] or discord_config['webhook_alerts'],
        msg, 'Live Vitals', 0x00CC44
    )

def discord_monitor():
    """Background thread — watches for alert conditions."""
    while True:
        if discord_config['enabled']:
            # Oil temp warning
            if discord_config['notify_oil'] and truck_state['oil_temp'] > 225:
                discord_alert('oil_high',
                    f'Oil temp critical at **{truck_state["oil_temp"]}F**. Pull over.',
                    '⚠️ OIL TEMP WARNING', 'alerts', 0xCC0000)

            # Battery warning
            if discord_config['notify_battery'] and truck_state['battery_main'] < 12.0:
                discord_alert('bat_low',
                    f'Battery low at **{truck_state["battery_main"]}V**. Check alternator.',
                    '🔋 BATTERY WARNING', 'alerts', 0xFF6600)

            # Boost spike
            if discord_config['notify_boost'] and truck_state['boost'] > 13:
                discord_alert('boost_high',
                    f'Boost spiking at **{truck_state["boost"]} PSI**.',
                    '💨 BOOST SPIKE', 'alerts', 0xFF6600)

            # Radar alert
            if discord_config['notify_radar'] and radar_detector['alert_level'] in ['strong','laser']:
                band = radar_detector['band'] or 'Unknown'
                discord_alert('radar',
                    f'**{band} band** — {radar_detector["direction"]} — {radar_detector["strength"]} bars\n'
                    f'Road: {road_memory[current_road]["name"] if current_road else "unknown"}',
                    '🚨 RADAR ALERT', 'radar', 0xFF0000)

            # Valet doing something bad
            if discord_config['notify_valet'] and tier_state['current'] >= 4:
                if truck_state['rpm'] > 3000:
                    discord_alert('valet_rpm',
                        f'Valet hit **{truck_state["rpm"]} RPM**.',
                        '👀 VALET ALERT', 'alerts', 0xFFAA00)

            # Weather alert
            if discord_config['notify_weather']:
                if weather_alerts.get('tornado_warn'):
                    discord_alert('tornado',
                        'Tornado WARNING active for Salem area.',
                        '🌪️ TORNADO WARNING', 'alerts', 0xFF0000)
                elif weather_alerts.get('severe_storm'):
                    discord_alert('storm',
                        'Severe thunderstorm warning active.',
                        '⛈️ SEVERE STORM', 'alerts', 0xFF6600)

            # New personal record
            if discord_config['notify_records']:
                if drag_timer['best_et'] and drag_timer['stage'] == 'done':
                    et  = drag_timer['best_et']
                    mph = drag_timer['best_mph']
                    discord_alert('new_record',
                        f'New best ET: **{et}s @ {mph} MPH** 🔥\n'
                        f'E{truck_state["ethanol"]} — {truck_state["boost"]} PSI boost',
                        '🏆 NEW PERSONAL BEST', 'alerts', 0x00CC44)

        time.sleep(15)

def set_discord_webhook(channel, url):
    key = f'webhook_{channel}'
    if key in discord_config:
        discord_config[key] = url
        if not discord_config['webhook_alerts'] and channel != 'alerts':
            discord_config['webhook_alerts'] = url
        save_state()
        return f'Discord {channel} webhook set.'
    return f'Channels: alerts vitals radar build'

# ── ARCHER MEMORY FUNCTIONS ──────────────
def log_moment(category, description):
    moment = {
        'time':     datetime.now().strftime('%B %d %Y %I:%M %p'),
        'category': category, 'desc': description,
        'road':     road_memory[current_road]['name'] if current_road else 'unknown',
        'weather':  f"{weather['temp']}F {weather['condition']}",
    }
    archer_memory['moments'].append(moment)
    if len(archer_memory['moments']) > 50:
        archer_memory['moments'] = archer_memory['moments'][-50:]
    save_state()

def show_archer_memory():
    print("\n── ARCHER MEMORY ────────────────────────")
    print(f"  Total sessions:  {archer_memory['total_sessions']}")
    print(f"  First drive:     {archer_memory['first_drive'] or 'Not logged yet'}")
    print(f"  Moments logged:  {len(archer_memory['moments'])}")
    if archer_memory['moments']:
        print("\n  Recent moments:")
        for m in archer_memory['moments'][-5:]:
            print(f"    {m['time']} — {m['desc']}")
    print("─────────────────────────────────────────\n")

# ── LEGACY FUNCTIONS ─────────────────────
def activate_legacy():
    legacy['active'] = True
    arduino_send("INTERIOR:AMBER_WARM")
    arduino_send("EXHAUST:0")

def lock_legacy():
    legacy['locked'] = True; legacy['active'] = True

def add_legacy_voice_note(note):
    entry = {
        'date': datetime.now().strftime('%B %d %Y %I:%M %p'),
        'note': note, 'weather': f"{weather['temp']}F {weather['condition']}",
    }
    legacy['voice_notes'].append(entry)
    save_state()
    return f"Voice note saved. {len(legacy['voice_notes'])} total notes."

def show_legacy():
    print("\n── LEGACY ───────────────────────────────")
    print(f"  Active:      {legacy['active']}")
    print(f"  Locked:      {legacy['locked']}")
    print(f"  Voice notes: {len(legacy['voice_notes'])}")
    if legacy['voice_notes']:
        print("\n  Recent notes:")
        for note in legacy['voice_notes'][-3:]:
            print(f"    {note['date']} — {note['note']}")
    print("─────────────────────────────────────────\n")

# ── ROAD MEMORY ──────────────────────────
def set_road(road_key):
    global current_road
    if road_key in road_memory:
        current_road = road_key
        r = road_memory[road_key]
        print(f"\n[ROAD MEMORY] Now on: {r['name']}")
        if r['hazards']:     print(f"[ROAD MEMORY] Hazards: {r['hazards']}")
        if r['best_launch']: print(f"[ROAD MEMORY] Best launch spot: {r['best_launch']}")
        if weather['raining'] and r['wet_warning']:
            msg = f"Wet road warning on {r['name']}. {r['wet_warning']}"
            print(f"[ROAD MEMORY] WET: {r['wet_warning']}"); speak(msg)
        if weather['freezing']:
            speak(f"Road freeze risk. {weather['temp']} degrees. TC staying on.")
            truck_state['tc_on'] = True; arduino_send("TC_LOCK")
        print(); save_state()
        return r
    return None

# ── OCTANE HELPER ────────────────────────
def set_octane(value, mode='AKI'):
    truck_state['octane']      = value
    truck_state['octane_mode'] = mode
    arduino_send(f"OCTANE:{value}{mode}")

# ── MUSIC AWARENESS ──────────────────────
def set_music(song, energy='medium'):
    music_state['current_song'] = song
    music_state['energy']       = energy
    music_state['playing']      = True
    if song in music_state['song_memories']:
        print(f"[MUSIC MEMORY] Last time this played: {music_state['song_memories'][song]}")
    if song in music_state.get('song_lighting', {}):
        lighting = music_state['song_lighting'][song]
        arduino_send(f"INTERIOR:{lighting['interior']}")
        arduino_send(f"UNDERBODY:{lighting['underbody']}")
    if energy == 'hype':
        arduino_send("UNDERBODY:200")
        print("[ARCHER] Music is hype. Exhaust suggestion — want it open?")
    elif energy == 'calm':
        arduino_send("INTERIOR:80")
        print("[ARCHER] Good late night track.")
    elif energy == 'medium':
        arduino_send("INTERIOR:140")

def link_song_to_moment(song, moment):
    music_state['song_memories'][song] = moment
    print(f"[MUSIC MEMORY] Linked '{song}' to: {moment}")
    save_state()

# ── ADAPTIVE DRIVE MODES ─────────────────
def set_drive_mode(mode):
    modes = {
        'comfort':     {'exhaust': 15,  'tc': True,  'desc': 'Comfort mode. Everything soft.'},
        'sport':       {'exhaust': 65,  'tc': False, 'desc': 'Sport mode. Ready.'},
        'tow':         {'exhaust': 30,  'tc': True,  'desc': 'Tow mode. Torque early. TC locked.'},
        'weather':     {'exhaust': 10,  'tc': True,  'desc': f'Weather mode. {weather["temp"]}F outside. TC locked on.'},
        'ghost':       {'exhaust': 0,   'tc': True,  'desc': 'Ghost mode. Going invisible.'},
        'performance': {'exhaust': 100, 'tc': False, 'desc': 'Performance mode. Full open. TC off.'},
    }
    if mode not in modes:
        return f"Unknown mode. Available: {', '.join(modes.keys())}"
    m = modes[mode]
    truck_state['exhaust']    = m['exhaust']
    truck_state['tc_on']      = m['tc']
    truck_state['ghost_mode'] = mode == 'ghost'
    arduino_send(f"EXHAUST:{m['exhaust']}")
    arduino_send(f"TC:{'LOCK' if m['tc'] else 'RELEASE'}")
    arduino_send(f"DRIVE_MODE:{mode.upper()}")
    return m['desc']

# ── CASUAL CONVERSATION ──────────────────
def casual_monitor():
    global last_casual
    time.sleep(90)
    while True:
        wait = random.randint(600, 1500)
        time.sleep(wait)
        now = time.time()
        if tier_state['current'] != 1:    continue
        if awareness['warnings_active']:   continue
        if now - last_casual < 600:       continue
        if random.random() < 0.35:        continue

        session_mins = round((time.time() - awareness['drive_session_start']) / 60)
        situation = f"""
Current situation:
- Time: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}
- Weather: {weather['temp']}F — {weather['condition']}
- RPM: {truck_state['rpm']} — throttle: {awareness['throttle_state']}
- Oil: {truck_state['oil_temp']}F — trend: {awareness['oil_trend']}
- Speed: {truck_state['speed']} mph — Ethanol: {truck_state['ethanol']}%
- Exhaust: {truck_state['exhaust']}% — Road: {road_memory[current_road]['name'] if current_road else 'unknown'}
- Session: {session_mins} minutes — Peak RPM: {awareness['peak_rpm']}
- Drive quality: {awareness['drive_quality']}/100
- Music: {music_state['current_song'] if music_state['playing'] else 'off'}
- Best 0-60: {personal_bests['best_0_60']}s
"""
        prompt = f"""{SYSTEM_PROMPT}
{situation}
Archer has not said anything unprompted in a while. Archer decides to say ONE short observation about right now.
Rules:
- Maximum 1 sentence
- Only say something if it genuinely adds something
- If nothing is worth saying respond with exactly: SILENCE
Archer says:"""

        try:
            response = None
            GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
            if GEMINI_KEY:
                try:
                    payload = json.dumps({'model': 'gemini-2.0-flash-lite',
                                          'messages': [{'role': 'user', 'content': prompt}],
                                          'max_tokens': 80, 'temperature': 0.8}).encode()
                    req = urllib.request.Request(
                        'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
                        data=payload, headers={'Authorization': f'Bearer {GEMINI_KEY}', 'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        response = json.loads(r.read())['choices'][0]['message']['content'].strip()
                except Exception:
                    pass
            if not response and _IS_PI:
                try:
                    payload = json.dumps({'model': 'llama3.2', 'prompt': prompt, 'stream': False}).encode()
                    req = urllib.request.Request('http://localhost:11434/api/generate', data=payload,
                                                 headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=12) as r:
                        response = json.loads(r.read()).get('response', '').strip()
                except Exception:
                    pass
            if not response:
                HF_TOKEN = os.environ.get('HF_TOKEN', '')
                if HF_TOKEN:
                    payload = json.dumps({'model': 'meta-llama/Meta-Llama-3.1-8B-Instruct',
                                          'messages': [{'role': 'user', 'content': prompt}],
                                          'max_tokens': 80, 'temperature': 0.8}).encode()
                    req = urllib.request.Request(
                        'https://router.huggingface.co/hf-inference/v1/chat/completions',
                        data=payload, headers={'Authorization': f'Bearer {HF_TOKEN}', 'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=12) as r:
                        response = json.loads(r.read())['choices'][0]['message']['content'].strip()
            if not response or 'SILENCE' in response.upper(): continue
            if 'Archer says:' in response:
                response = response.split('Archer says:')[-1].strip()
            response = response.strip('"').strip("'")
            if 5 < len(response) < 120:
                time.sleep(random.randint(3, 12))
                print(f"\n[ARCHER] {response}"); speak(response)
                last_casual = now
        except Exception:
            pass

# ── SHOW MODES ───────────────────────────
def run_flex():
    arduino_send("SHOW:FLEX")
    print("[ARDUINO] All amber LEDs pulse — warning")
    arduino_send("EXHAUST:80")
    for i in range(1, 5):
        print(f"[ARDUINO] Corner {i} rising"); time.sleep(0.3)
    print("[ARDUINO] Rev sequence 3000 RPM"); time.sleep(0.4)
    print("[ARDUINO] Rev sequence 4000 RPM"); time.sleep(0.4)
    print("[ARDUINO] Rev sequence 5000 RPM"); time.sleep(0.6)
    arduino_send("EXHAUST:30")
    print("[ARDUINO] Lights return to normal — Amber LEDs off")

def run_drunk():
    arduino_send("SHOW:DRUNK")
    for corner in ['Front left', 'Front right', 'Rear left', 'Rear right']:
        print(f"[ARDUINO] {corner} bag deflates"); time.sleep(0.5)
    time.sleep(1)
    print("[ARDUINO] All bags inflate — snaps back level")
    print("[ARDUINO] DIC: I'M FINE")

def run_sneeze():
    arduino_send("SHOW:SNEEZE")
    print("[ARDUINO] PA horn buildup sound"); time.sleep(0.8)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] All four bags dump — Horn blast — Lights flash white"); time.sleep(0.5)
    print("[ARDUINO] Everything returns — DIC: BLESS YOU")

def run_stalker():
    arduino_send("SHOW:STALKER")
    print("[ARDUINO] Underbody lights rotate — wheel well brightens")
    print("[ARDUINO] PA horn: I can see you"); time.sleep(1)
    print("[ARDUINO] All lights off — Train horn — DIC: GOT YOU")

def run_existential():
    arduino_send("SHOW:EXISTENTIAL_CRISIS")
    print("[ARDUINO] All lights off — Sad violin — Deep bass")
    for line in ['WHAT IS EVEN THE POINT', '408 CUBIC INCHES', 'AND FOR WHAT', 'I COULD HAVE BEEN A MINIVAN']:
        print(f"[ARDUINO] DIC: {line}"); time.sleep(0.5)
    time.sleep(1)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] ALL LIGHTS ON — Train horn x5 — Max height")
    print("[ARDUINO] DIC: JUST KIDDING — LET'S GO")

def run_negotiations():
    arduino_send("SHOW:NEGOTIATIONS")
    print("[ARDUINO] Truck drops slow — PA: I have a particular set of skills"); time.sleep(0.8)
    print("[ARDUINO] Exhaust crack open — Lights dark red"); time.sleep(0.8)
    print("[ARDUINO] Truck rises fast — Engine blip 4500 — PA: What I do have is a very specific truck"); time.sleep(0.5)
    print("[ARDUINO] Train horn — DIC: GOOD LUCK")

def run_goodbye():
    arduino_send("SHOW:GOODBYE"); time.sleep(1)
    print("[ARDUINO] Lights fade — Truck lowers — Exhaust blip — Engine off")
    print("[ARDUINO] Underbody pulse once — DIC: SEE YOU TOMORROW — Alarm arms")

def run_motivational():
    arduino_send("SHOW:MOTIVATIONAL_SPEAKER"); time.sleep(1)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] Rocky music builds — DIC: LET'S GO CHAMP")

def run_karen():
    arduino_send("SHOW:KAREN")
    print("[ARDUINO] PA: Can I speak to your manager?"); time.sleep(1)
    print("[ARDUINO] Train horn x3 — DIC: I SAID GOOD DAY")

def run_reveille():
    arduino_send("SHOW:REVEILLE")
    print("[ARDUINO] 6AM bugle call through PA")
    print("[ARDUINO] Interior lights ramp from 0 to full slowly")
    arduino_send("EXHAUST:30")
    print("[ARDUINO] Engine remote start — DIC: RISE AND GRIND")

def run_impatient():
    arduino_send("SHOW:IMPATIENT")
    print("[ARDUINO] Horn — three short taps"); time.sleep(0.5)
    print("[ARDUINO] Horn — two more taps"); time.sleep(0.3)
    print("[ARDUINO] Horn — one long blast")
    arduino_send("EXHAUST:60")

def run_politician():
    arduino_send("SHOW:POLITICIAN")
    print("[ARDUINO] PA: I have always supported trucks"); time.sleep(0.8)
    print("[ARDUINO] PA: Big trucks. The biggest."); time.sleep(0.8)
    print("[ARDUINO] PA: Nobody knows trucks better than me"); time.sleep(0.5)
    print("[ARDUINO] Train horn — DIC: YOU ARE WELCOME")

def run_suspicious():
    arduino_send("SHOW:SUSPICIOUS")
    print("[ARDUINO] All lights off except single amber pulse"); time.sleep(1)
    print("[ARDUINO] Slow creep — interior dims — DIC: I SAW THAT")

def run_conspiracy():
    arduino_send("SHOW:CONSPIRACY")
    print("[ARDUINO] All lights flicker — PA: They do not want you to know about this truck"); time.sleep(0.8)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] ALL LIGHTS ON — DIC: DO YOUR RESEARCH")

def run_introvert():
    arduino_send("SHOW:INTROVERT")
    arduino_send("EXHAUST:0")
    print("[ARDUINO] All exterior lights off — Interior 10% — Engine minimum idle — DIC: DO NOT TALK TO ME")

def run_wrong_neighborhood():
    arduino_send("SHOW:WRONG_NEIGHBORHOOD")
    arduino_send("EXHAUST:100")
    print("[ARDUINO] Truck raises to full height instantly — All lights max — Train horn x2 — DIC: NOTED")

def run_passive_aggressive():
    arduino_send("SHOW:PASSIVE_AGGRESSIVE")
    print("[ARDUINO] Horn — one very polite tap — Interior slightly warmer")
    print("[ARDUINO] DIC: NO ITS FINE"); time.sleep(1)
    print("[ARDUINO] DIC: EVERYTHING IS FINE"); time.sleep(0.5)
    arduino_send("EXHAUST:80")
    print("[ARDUINO] DIC: I SAID ITS FINE")

def run_identity_crisis():
    arduino_send("SHOW:IDENTITY_CRISIS")
    print("[ARDUINO] Truck slams — then raises — Exhaust open then close then open")
    print("[ARDUINO] DIC: AM I A SHOW TRUCK"); time.sleep(0.5)
    print("[ARDUINO] DIC: AM I A WORK TRUCK"); time.sleep(0.5)
    print("[ARDUINO] DIC: YES")

def run_exit_interview():
    arduino_send("SHOW:EXIT_INTERVIEW")
    print("[ARDUINO] Interior white — PA: So. Tell me about yourself."); time.sleep(1)
    print("[ARDUINO] PA: Where do you see yourself in five years."); time.sleep(1)
    print("[ARDUINO] Train horn blast — DIC: YOU DID NOT GET THE JOB")

def run_backup_warning():
    arduino_send("SHOW:BACKUP_WARNING")
    print("[ARDUINO] Reverse lights full — PA: Caution. Truck backing up."); time.sleep(0.5)
    print("[ARDUINO] PA: Seriously. Move.")
    print("[ARDUINO] Train horn — one blast")

def run_cinema():
    arduino_send("SHOW:CINEMA")
    print("[ARDUINO] Engine off — Projector deploys — 100in screen lowers")
    print("[ARDUINO] Interior lights off — Seat heat on — Subwoofers active — DIC: CINEMA MODE")

def run_concert():
    arduino_send("SHOW:CONCERT")
    arduino_send("EXHAUST:100")
    print("[ARDUINO] All speakers max — Subwoofers full — Interior color sync — DIC: TURN IT UP")

def run_argument():
    arduino_send("SHOW:ARGUMENT")
    print("[ARDUINO] PA: Oh really."); time.sleep(0.5)
    print("[ARDUINO] PA: Because I disagree."); time.sleep(0.5)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] PA: We clear? — DIC: I WIN")

def run_haunted():
    arduino_send("SHOW:HAUNTED")
    print("[ARDUINO] All lights flicker slow — Engine RPM fluctuates")
    print("[ARDUINO] PA: creaking door sound"); time.sleep(1)
    print("[ARDUINO] All lights off"); time.sleep(1)
    print("[ARDUINO] ALL LIGHTS BLAST ON — Train horn — DIC: BOO")

def run_stadium():
    arduino_send("SHOW:STADIUM")
    arduino_send("EXHAUST:100")
    print("[ARDUINO] PA: crowd roar — All lights full — Truck raises to max — Horn victory sequence — DIC: LETS GOOO")

def run_sleeping_giant():
    arduino_send("SHOW:SLEEPING_GIANT")
    print("[ARDUINO] All lights off — Engine minimum idle"); time.sleep(2)
    print("[ARDUINO] Single amber pulse slow"); time.sleep(1)
    arduino_send("EXHAUST:100")
    print("[ARDUINO] ALL LIGHTS ON — Max height — Train horn x5 — DIC: DID YOU THINK I WAS SLEEPING")

def run_fuel_economy():
    arduino_send("SHOW:FUEL_ECONOMY")
    arduino_send("EXHAUST:0")
    print("[ARDUINO] Truck lowers to lowest — Interior green dim — Engine minimum idle — DIC: 8 MPG OUTSTANDING")

def run_ultimatum():
    arduino_send("SHOW:ULTIMATUM")
    print("[ARDUINO] Amber LEDs pulse slow — PA: I am going to say this once."); time.sleep(1)
    arduino_send("EXHAUST:100")
    time.sleep(1)
    print("[ARDUINO] PA: We clear? — DIC: GOOD TALK")

# ── SMART KNOB ───────────────────────────
smart_knob = {'menu': 'main', 'position': 0, 'open': False}

MENUS = {
    'main':     ['Exhaust Control','Octane Setting','Drive Mode','Show Modes','Lighting','Profiles','System Status','Close Menu'],
    'exhaust':  ['Closed — 0%','Neighborhood — 15%','Cruise — 30%','Street — 50%','Sport — 75%','Full Open — 100%','Back'],
    'octane':   ['87 AKI','91 AKI','93 AKI','E85','95 RON','98 RON','100 RON','Back'],
    'drive':    ['Comfort','Sport','Tow','Weather','Ghost','Performance','Back'],
    'show':     ['Flex','Slam','Drunk','Sneeze','Stalker','Existential Crisis','Negotiations','Motivational','Goodbye','Karen','Therapy','Sleeping Giant','Stadium','Cinema','Concert','Back'],
    'lighting': ['Underglow On','Underglow Off','Wheel Wells On','Wheel Wells Off','Interior Dim','Interior Full','All Off','Back'],
    'profiles': ['Load Ayden','Load Girlfriend','Load Family','Load Valet','List Profiles','Back'],
}

def show_knob_menu():
    menu  = smart_knob['menu']
    pos   = smart_knob['position']
    items = MENUS.get(menu, MENUS['main'])
    print(f"\n╔── SMART KNOB — {menu.upper()} {'─' * max(0, 20 - len(menu))}╗")
    for i, item in enumerate(items):
        print(f"║{'  ► ' if i == pos else '    '}{item}")
    print(f"╚{'─' * 30}╝\n")

def knob_up():
    items = MENUS.get(smart_knob['menu'], MENUS['main'])
    smart_knob['position'] = (smart_knob['position'] - 1) % len(items)
    show_knob_menu()

def knob_down():
    items = MENUS.get(smart_knob['menu'], MENUS['main'])
    smart_knob['position'] = (smart_knob['position'] + 1) % len(items)
    show_knob_menu()

def knob_select():
    menu  = smart_knob['menu']
    pos   = smart_knob['position']
    items = MENUS.get(menu, MENUS['main'])
    item  = items[pos].lower()

    def go_to(submenu):
        smart_knob['menu'] = submenu; smart_knob['position'] = 0; show_knob_menu(); return ""
    def go_back():
        smart_knob['menu'] = 'main'; smart_knob['position'] = 0; show_knob_menu(); return ""

    if menu == 'main':
        if 'exhaust'  in item: return go_to('exhaust')
        if 'octane'   in item: return go_to('octane')
        if 'drive'    in item: return go_to('drive')
        if 'show'     in item: return go_to('show')
        if 'lighting' in item: return go_to('lighting')
        if 'profile'  in item: return go_to('profiles')
        if 'status'   in item: print_status(); return ""
        if 'close'    in item: smart_knob['open'] = False; return "Menu closed."
    elif menu == 'exhaust':
        if 'back' in item: return go_back()
        pct_map = {'0%':0,'closed':0,'15%':15,'neighborhood':15,'30%':30,'cruise':30,'50%':50,'street':50,'75%':75,'sport':75,'100%':100,'full':100}
        for key, val in pct_map.items():
            if key in item:
                truck_state['exhaust'] = val; arduino_send(f"EXHAUST:{val}"); return f"Exhaust at {val} percent."
    elif menu == 'octane':
        if 'back'    in item: return go_back()
        if 'e85'     in item: truck_state['ethanol'] = 85; return "Full E85. Power map loaded."
        if '87'      in item: set_octane(87,'AKI');  return "Loading the disappointment map."
        if '91'      in item: set_octane(91,'AKI');  return "91 AKI confirmed."
        if '93'      in item: set_octane(93,'AKI');  return "93 AKI. Full gasoline map loaded."
        if '95 ron'  in item: set_octane(95,'RON');  return "95 RON. 90 AKI equivalent."
        if '98 ron'  in item: set_octane(98,'RON');  return "98 RON. 93 AKI equivalent."
        if '100 ron' in item: set_octane(100,'RON'); return "100 RON. Race fuel map active."
    elif menu == 'drive':
        if 'back'        in item: return go_back()
        if 'comfort'     in item: return set_drive_mode('comfort')
        if 'sport'       in item: return set_drive_mode('sport')
        if 'tow'         in item: return set_drive_mode('tow')
        if 'weather'     in item: return set_drive_mode('weather')
        if 'ghost'       in item: return set_drive_mode('ghost')
        if 'performance' in item: return set_drive_mode('performance')
    elif menu == 'show':
        if 'back' in item: return go_back()
        return handle_command(items[pos].lower())
    elif menu == 'lighting':
        if 'back'            in item: return go_back()
        if 'underglow on'    in item: arduino_send("UNDERBODY:255"); return "Underglow on."
        if 'underglow off'   in item: arduino_send("UNDERBODY:0");   return "Underglow off."
        if 'wheel wells on'  in item: arduino_send("WHEELWELL:255"); return "Wheel wells on."
        if 'wheel wells off' in item: arduino_send("WHEELWELL:0");   return "Wheel wells off."
        if 'interior dim'    in item: arduino_send("INTERIOR:80");   return "Interior dimmed."
        if 'interior full'   in item: arduino_send("INTERIOR:255");  return "Interior full brightness."
        if 'all off'         in item:
            arduino_send("UNDERBODY:0"); arduino_send("WHEELWELL:0"); arduino_send("INTERIOR:0")
            return "All lights off."
    elif menu == 'profiles':
        if 'back'       in item: return go_back()
        if 'ayden'      in item: return load_profile('ayden')
        if 'girlfriend' in item: return load_profile('girlfriend')
        if 'family'     in item: return load_profile('family')
        if 'valet'      in item: return load_profile('valet')
        if 'list'       in item: list_profiles(); return ""
    return None

# ── DIRECT COMMANDS ──────────────────────
def handle_command(text):
    t = text.lower().strip()

    if any(x in t for x in ['engine off', 'shut down', 'shutting down', 'kill engine']):
        import sys
        save_trip()
        print("[ARCHER] See you tomorrow."); speak("See you tomorrow."); save_state(); sys.exit(0)

    # ── TIER SWITCHING ───────────────────
    if 'profile' not in t:
        if any(x in t for x in ['tier 1','tier1','switch to ayden','owner mode']):
            tier_state['current'] = 1; return "Tier 1. Welcome back."
        if any(x in t for x in ['tier 2','tier2','girlfriend mode','passenger mode']):
            tier_state['current'] = 2; return "Tier 2 active."
        if any(x in t for x in ['tier 3','tier3','family mode']):
            tier_state['current'] = 3; return "Tier 3 active."
        if any(x in t for x in ['tier 4','tier4','valet mode']):
            tier_state['current'] = 4; return "Valet mode. Monitoring everything."

    # ── DRIVER PROFILES ──────────────────
    if 'list profiles' in t or 'show profiles' in t or 'who has a profile' in t:
        list_profiles(); return f"{len(driver_profiles)} profiles loaded."

    if any(x in t for x in ['create profile', 'add profile', 'new profile']):
        words = t.replace('create profile','').replace('add profile','').replace('new profile','').strip().split()
        name = None; tier = 2
        for i, word in enumerate(words):
            if word == 'tier' and i + 1 < len(words):
                try: tier = int(words[i + 1])
                except: pass
            elif word not in ['tier','1','2','3','4']:
                if name is None: name = word.title()
        if name: return create_profile(name, tier)
        return "Say the name. Example: create profile Marcus tier 2"

    if any(x in t for x in ['delete profile', 'remove profile']):
        name = t.replace('delete profile','').replace('remove profile','').strip()
        return delete_profile(name) if name else "Which profile?"

    if any(x in t for x in ['load profile', 'switch to', 'switch profile']):
        name = t.replace('load profile','').replace('switch to','').replace('switch profile','').strip()
        return load_profile(name) if name else f"Current profile is {driver_profiles[current_profile]['name']}."

    if 'current profile' in t or 'who is driving' in t or 'which profile' in t:
        p = driver_profiles[current_profile]
        return f"{p['name']}. Tier {p['tier']}. {p['notes']}."

    if any(x in t for x in ['link bluetooth', 'link phone', 'link device']):
        fake_mac = ':'.join(['{:02x}'.format(x) for x in uuid.uuid4().bytes[:6]])
        link_bluetooth(fake_mac, current_profile)
        return f"Phone linked to {driver_profiles[current_profile]['name']} profile."

    if any(x in t for x in ['list devices', 'linked devices', 'linked phones']):
        if not bluetooth_devices: return "No devices linked yet."
        print("\n── LINKED DEVICES ───────────────────────")
        for mac, key in bluetooth_devices.items():
            print(f"  {mac} → {driver_profiles[key]['name']}")
        print("─────────────────────────────────────────\n")
        return f"{len(bluetooth_devices)} device linked."

    if 'save preference' in t or 'remember this setting' in t:
        if 'exhaust' in t:
            save_profile_pref('exhaust_pref', truck_state['exhaust'])
            return f"Exhaust preference saved for {driver_profiles[current_profile]['name']}."
        return "What preference should I save?"

    # ── DRIVE MODES ──────────────────────
    if any(x in t for x in ['comfort mode', 'drive comfort']): return set_drive_mode('comfort')
    if any(x in t for x in ['sport mode', 'drive sport']):     return set_drive_mode('sport')
    if any(x in t for x in ['tow mode', 'drive tow', 'towing mode']): return set_drive_mode('tow')
    if any(x in t for x in ['weather mode', 'drive weather', 'rain mode']): return set_drive_mode('weather')
    if any(x in t for x in ['performance mode', 'track mode', 'full send']): return set_drive_mode('performance')

    # ── LEGACY MODE ──────────────────────
    if any(x in t for x in ['legacy mode', 'activate legacy']):
        activate_legacy(); return "Legacy mode active. Everything from here is permanent."
    if any(x in t for x in ['lock legacy', 'legacy lock']):
        lock_legacy(); return "Legacy locked. This build is on the record forever."
    if any(x in t for x in ['voice note', 'log note', 'save note']):
        note = t.replace('voice note','').replace('log note','').replace('save note','').strip()
        return add_voice_note(note) if note else "What is the note?"
    if any(x in t for x in ['show legacy', 'legacy log', 'build history']):
        show_legacy(); return "Legacy log above."

    # ── ARCHER MEMORY ────────────────────
    if any(x in t for x in ['archer memory', 'what do you remember', 'memory log']):
        show_archer_memory(); return "Memory log above."
    if any(x in t for x in ['log this moment', 'remember this', 'save this']):
        desc = t.replace('log this moment','').replace('remember this','').replace('save this','').strip()
        if not desc: desc = f"Moment logged — {awareness['throttle_state']} — {truck_state['speed']} mph"
        log_moment('manual', desc); return "Logged."

    # ── SHOW MODES ───────────────────────
    if any(x in t for x in ['flex', 'show mode', 'car show']):
        truck_state['exhaust'] = 80; run_flex(); return "Alright. Watch this."
    if 'slam' in t:
        truck_state['exhaust'] = 100; arduino_send("SHOW:SLAM"); return "Dropping it."
    if 'drunk' in t:       run_drunk();        return "Activating the Drunk. Try to look casual."
    if 'sneeze' in t:      run_sneeze();       return "Gesundheit."
    if 'stalker' in t:     run_stalker();      return "Going dark."
    if any(x in t for x in ['existential','existential crisis']): run_existential(); return "Alright. I will get the violin."
    if 'negotiation' in t: run_negotiations(); return "I have a particular set of skills."
    if 'goodbye' in t or ('good' in t and 'night' in t and 'show' in t): run_goodbye(); return "That is enough for today."
    if 'motivational' in t or 'motivate me' in t: run_motivational(); return "Let's go champ."
    if 'karen' in t:       run_karen();        return "Can I speak to your manager."
    if any(x in t for x in ['therapy', 'need a minute', 'rough day']):
        arduino_send("SHOW:THERAPY")
        print("[ARDUINO] Seat heat ON — Interior warm amber")
        return "Seat heat is on. Take your time."
    if any(x in t for x in ['reveille', 'wake up show']): run_reveille(); return "Rise and grind."
    if any(x in t for x in ['impatient', 'hurry up show']): run_impatient(); return "Some people need encouragement."
    if 'politician'        in t: run_politician();        return "Nobody knows trucks better."
    if 'suspicious'        in t: run_suspicious();        return "I saw that."
    if 'conspiracy'        in t: run_conspiracy();        return "They do not want you to know."
    if 'introvert'         in t: run_introvert();         return "Do not talk to me right now."
    if 'wrong neighborhood'in t: run_wrong_neighborhood(); return "Noted."
    if 'passive aggressive'in t: run_passive_aggressive(); return "Everything is fine."
    if 'identity crisis'   in t: run_identity_crisis();   return "Still figuring that out."
    if 'exit interview'    in t: run_exit_interview();    return "You did not get the job."
    if 'backup warning'    in t: run_backup_warning();    return "Caution. Truck backing up."
    if 'cinema'            in t: run_cinema();            return "Cinema mode. Enjoy the show."
    if 'concert'           in t: run_concert();           return "Turn it up."
    if 'argument' in t and 'show' in t: run_argument();  return "I win."
    if 'haunted'           in t: run_haunted();           return "Boo."
    if 'stadium'           in t: run_stadium();           return "Let's go."
    if 'sleeping giant'    in t: run_sleeping_giant();    return "Did you think I was sleeping."
    if 'fuel economy'      in t: run_fuel_economy();      return "8 MPG. Outstanding."
    if 'ultimatum'         in t: run_ultimatum();         return "Good talk."

    # ── SMART KNOB ───────────────────────
    if any(x in t for x in ['knob menu', 'open menu', 'menu']):
        smart_knob['menu'] = 'main'; smart_knob['position'] = 0; smart_knob['open'] = True
        show_knob_menu(); return "Main menu open."

    if smart_knob['open']:
        if any(x in t for x in ['knob up','up','scroll up','previous']):   knob_up();   return ""
        if any(x in t for x in ['knob down','down','scroll down','next']): knob_down(); return ""
        if any(x in t for x in ['select','choose','knob select','press','enter']): return knob_select()
        if any(x in t for x in ['back','go back','knob back','cancel']):
            if smart_knob['menu'] == 'main': smart_knob['open'] = False; return "Menu closed."
            smart_knob['menu'] = 'main'; smart_knob['position'] = 0; show_knob_menu(); return ""

    # ── OCTANE ───────────────────────────
    if 'octane' in t and any(x in t for x in ['what','current','which','how much','tell me','know','running','in the','check']):
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."
    if t in ['octane','octane?','what octane']:
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."
    if 'e85' in t and any(x in t for x in ['fill','putting','about to','just filled']):
        truck_state['ethanol'] = 85; arduino_send("ETHANOL:85"); return "Full E85. Power map loaded. About time."
    if any(x in t for x in ['octane','fuel grade','ron','filling','fill up','about to fill']):
        if any(x in t for x in ['ron','international','europe']):
            for grade in sorted(RON_OCTANE_GRADES, reverse=True):
                if str(grade) in t:
                    set_octane(grade,'RON'); return f"{grade} RON. {round(grade*0.95)} AKI equivalent. Tune adjusted."
        for grade in sorted(US_OCTANE_GRADES, reverse=True):
            if str(grade) in t:
                set_octane(grade,'AKI')
                if grade == 87: return "Loading the disappointment map."
                if grade == 93: return "93 AKI. Full gasoline map loaded."
                return f"{grade} AKI confirmed."
        return "What octane?"
    if any(x in t for x in ['fill','putting','about to','just filled']):
        for grade in sorted(US_OCTANE_GRADES, reverse=True):
            if str(grade) in t:
                set_octane(grade,'AKI')
                if grade == 87: return "Loading the disappointment map."
                if grade == 93: return "93 AKI. Full gasoline map loaded."
                return f"{grade} AKI confirmed."

    # ── EXHAUST ──────────────────────────
    if any(x in t for x in ['open exhaust','open it up','cut it open','open the exhaust']):
        truck_state['exhaust'] = 100; arduino_send("EXHAUST:100"); return "Opening it up."
    if any(x in t for x in ['close exhaust','quiet down','close it','close the exhaust']):
        truck_state['exhaust'] = 0; arduino_send("EXHAUST:0"); return "Closing it down."
    if t in ['exhaust','exhaust level','exhaust percent']:
        return f"Exhaust is at {truck_state['exhaust']} percent."
    if 'exhaust' in t and any(x in t for x in ['50','half','halfway']):
        truck_state['exhaust'] = 50; arduino_send("EXHAUST:50"); return "Exhaust at 50 percent."

    # ── TRACTION CONTROL ─────────────────
    if any(x in t for x in ['tc off','traction off','kill tc']):
        truck_state['tc_on'] = False; truck_state['tc_locked'] = False
        arduino_send("TC_OFF"); return "TC off. Road looks dry. We are good."
    if any(x in t for x in ['tc on','traction on','lock tc']):
        truck_state['tc_on'] = True; truck_state['tc_locked'] = True
        arduino_send("TC_LOCK"); return "TC on."

    # ── GHOST MODE ───────────────────────
    if 'ghost' in t and 'off' not in t:
        truck_state['ghost_mode'] = True; truck_state['exhaust'] = 0
        arduino_send("EXHAUST:0"); arduino_send("UNDERBODY:0"); arduino_send("GROUND:0")
        return "Going invisible."
    if any(x in t for x in ['ghost off','turn ghost off']):
        truck_state['ghost_mode'] = False; arduino_send("GHOST_OFF"); return "Back to normal."

    # ── FACTORY CONTROLS ─────────────────
    if any(x in t for x in ['headlights on','lights on','turn on lights']):
        truck_state['headlights'] = True; arduino_send("HEADLIGHTS:ON"); return "Headlights on."
    if any(x in t for x in ['headlights off','lights off','turn off lights']):
        truck_state['headlights'] = False; arduino_send("HEADLIGHTS:OFF"); return "Headlights off."
    if any(x in t for x in ['high beams on','brights on']):
        truck_state['high_beams'] = True; arduino_send("HIGHBEAMS:ON"); return "High beams on."
    if any(x in t for x in ['high beams off','brights off']):
        truck_state['high_beams'] = False; arduino_send("HIGHBEAMS:OFF"); return "High beams off."
    if any(x in t for x in ['fog lights on','fogs on']):
        truck_state['fog_lights'] = True; arduino_send("FOGLIGHTS:ON"); return "Fog lights on."
    if any(x in t for x in ['fog lights off','fogs off']):
        truck_state['fog_lights'] = False; arduino_send("FOGLIGHTS:OFF"); return "Fog lights off."
    if any(x in t for x in ['hazards on','flashers on','four ways on']):
        truck_state['hazards'] = True; arduino_send("HAZARDS:ON"); return "Hazards on."
    if any(x in t for x in ['hazards off','flashers off','four ways off']):
        truck_state['hazards'] = False; arduino_send("HAZARDS:OFF"); return "Hazards off."
    if any(x in t for x in ['ac on','air on','turn on ac']):
        truck_state['ac_on'] = True; arduino_send("AC:ON"); return "AC on."
    if any(x in t for x in ['ac off','air off','turn off ac']):
        truck_state['ac_on'] = False; arduino_send("AC:OFF"); return "AC off."
    if any(x in t for x in ['heat on','heater on','turn on heat']):
        truck_state['heat_on'] = True; arduino_send("HEAT:ON"); return "Heat on."
    if any(x in t for x in ['heat off','heater off','turn off heat']):
        truck_state['heat_on'] = False; arduino_send("HEAT:OFF"); return "Heat off."
    if 'fan' in t:
        for level in ['1','2','3','4','5','6','7','8']:
            if level in t:
                truck_state['fan_speed'] = int(level); arduino_send(f"FAN:{level}"); return f"Fan speed {level}."
        if 'up' in t or 'higher' in t:
            new = min(8, truck_state['fan_speed'] + 1)
            truck_state['fan_speed'] = new; arduino_send(f"FAN:{new}"); return f"Fan speed {new}."
        if 'down' in t or 'lower' in t:
            new = max(0, truck_state['fan_speed'] - 1)
            truck_state['fan_speed'] = new; arduino_send(f"FAN:{new}"); return f"Fan speed {new}."
    if any(x in t for x in ['windows down','roll down windows','open windows']):
        truck_state['windows'] = {'fl':'down','fr':'down','rl':'down','rr':'down'}
        arduino_send("WINDOWS:ALL_DOWN"); return "Windows down."
    if any(x in t for x in ['windows up','roll up windows','close windows']):
        truck_state['windows'] = {'fl':'up','fr':'up','rl':'up','rr':'up'}
        arduino_send("WINDOWS:ALL_UP"); return "Windows up."
    if 'driver window' in t and 'down' in t:
        truck_state['windows']['fl'] = 'down'; arduino_send("WINDOW:FL_DOWN"); return "Driver window down."
    if 'driver window' in t and 'up' in t:
        truck_state['windows']['fl'] = 'up'; arduino_send("WINDOW:FL_UP"); return "Driver window up."
    if 'passenger window' in t and 'down' in t:
        truck_state['windows']['fr'] = 'down'; arduino_send("WINDOW:FR_DOWN"); return "Passenger window down."
    if 'passenger window' in t and 'up' in t:
        truck_state['windows']['fr'] = 'up'; arduino_send("WINDOW:FR_UP"); return "Passenger window up."
    if any(x in t for x in ['wipers on','turn on wipers']):
        truck_state['wipers'] = 'on'; arduino_send("WIPERS:ON"); return "Wipers on."
    if any(x in t for x in ['wipers off','turn off wipers']):
        truck_state['wipers'] = 'off'; arduino_send("WIPERS:OFF"); return "Wipers off."
    if 'wiper' in t and 'fast' in t:
        truck_state['wipers'] = 'fast'; arduino_send("WIPERS:FAST"); return "Wipers on fast."
    if 'wiper' in t and any(x in t for x in ['slow','intermittent','low']):
        truck_state['wipers'] = 'slow'; arduino_send("WIPERS:SLOW"); return "Wipers on slow."
    if any(x in t for x in ['fold mirrors','mirrors in','tuck mirrors']):
        truck_state['mirrors_folded'] = True; arduino_send("MIRRORS:FOLD"); return "Mirrors folded."
    if any(x in t for x in ['unfold mirrors','mirrors out','extend mirrors']):
        truck_state['mirrors_folded'] = False; arduino_send("MIRRORS:EXTEND"); return "Mirrors extended."
    if t == 'horn' or 'tap horn' in t or 'beep' in t:
        arduino_send("HORN:TAP"); return "Tap."
    if 'train horn' in t:
        arduino_send("HORN:TRAIN"); return "Train horn."
    if 'air horn' in t:
        arduino_send("HORN:AIR"); return "Air horn."

    # ── TRUCK-SPECIFIC STATUS QUERIES ────
    if any(x in t for x in ['oil life', 'oil percent', 'how much oil life']):
        oil_life = truck_state.get('oil_life', 100)
        if oil_life < 15:
            return f"Oil life is at {oil_life} percent. Change it soon — you are pushing it."
        elif oil_life < 30:
            return f"Oil life at {oil_life} percent. Start thinking about a change."
        return f"Oil life is at {oil_life} percent. Still good."

    if any(x in t for x in ['tire pressure', 'tires', 'psi', 'tpms']) and not any(x in t for x in ['set','change']):
        fl = tpms.get('fl', {}).get('psi', 0)
        fr = tpms.get('fr', {}).get('psi', 0)
        rl = tpms.get('rl', {}).get('psi', 0)
        rr = tpms.get('rr', {}).get('psi', 0)
        low = [name for name, psi in [('front left', fl), ('front right', fr), ('rear left', rl), ('rear right', rr)] if psi > 0 and psi < 28]
        if low:
            return f"Heads up — {', '.join(low)} is low. Check it when you can."
        if any(p > 0 for p in [fl, fr, rl, rr]):
            return f"Tire pressure looks good. FL {fl} FR {fr} RL {rl} RR {rr} PSI."
        return "TPMS data not available right now."

    if any(x in t for x in ['transmission temp', 'trans temp', 'transmission temperature']):
        trans = truck_state.get('trans_temp', sensor_data.get('trans_temp', 0))
        if trans > 220:
            return f"Transmission is hot — {trans} degrees. Ease up and let it cool."
        elif trans > 195:
            return f"Trans temp is elevated at {trans} degrees. Keep an eye on it."
        elif trans > 0:
            return f"Transmission temperature is {trans} degrees. Normal range."
        return "Transmission temperature data not available."

    if any(x in t for x in ['def level', 'diesel exhaust fluid', 'def fluid', 'def tank']):
        def_level = truck_state.get('def_level', None)
        if def_level is None:
            return "DEF level sensor not available on this engine."
        if def_level < 10:
            return f"DEF is critically low at {def_level} percent. Fill it before the next start."
        elif def_level < 25:
            return f"DEF level at {def_level} percent. Plan a fill-up soon."
        return f"DEF level is at {def_level} percent."

    if 'tpms' in t and any(x in t for x in ['check','status','all','pressures','read']):
        fl = tpms.get('fl', {}).get('psi', 0)
        fr = tpms.get('fr', {}).get('psi', 0)
        rl = tpms.get('rl', {}).get('psi', 0)
        rr = tpms.get('rr', {}).get('psi', 0)
        return f"TPMS readings — Front left {fl}, front right {fr}, rear left {rl}, rear right {rr} PSI."

    # ── WEATHER ──────────────────────────
    if any(x in t for x in ['weather','how cold','how hot','raining','outside temp','temperature outside']):
        data = get_weather(); weather.update(data); temp = weather['temp']; condition = weather['condition']
        feels = weather.get('feels_like', temp)
        if weather['snowing']:  return f"{temp}F and snowing in Salem. 4WD is ready. TC stays on."
        if weather['raining']:  return f"{temp}F and raining. TC locked on. Road will be slick."
        if weather['freezing']: return f"{temp}F. Everything is tighter today. Give me a minute to warm up."
        if temp > 90:           return f"{temp}F outside. Heat is going to build faster today."
        if temp < 50:
            feel_str = f" Feels like {feels}" if abs(feels - temp) > 3 else ""
            return f"{temp}F. Cold start territory. Oil needs a minute.{feel_str}"
        return f"{temp}F in Salem. {condition.title()}. Good day to be out."

    # ── ROAD MEMORY ──────────────────────
    if 'highway 72' in t and 'rolla' in t:   r = set_road('rolla_highway');       return "Murphy USA run. 31 miles. Mile 4 is your best launch spot." if r else None
    elif 'highway 72' in t and 'north' in t: r = set_road('highway_72_north');    return "Highway 72 North. Smooth at mile 4. Railroad crossing at mile 8." if r else None
    elif 'highway 72' in t and 'south' in t: r = set_road('highway_72_south');    return "72 South. Rolling hills. Watch the blind crests." if r else None
    elif 'highway 72' in t:                  r = set_road('highway_72_north');    return "Highway 72. Smooth at mile 4. Railroad crossing at mile 8." if r else None
    elif 'highway 19' in t and ('south' in t or 'eminence' in t): r = set_road('highway_19_south'); return "19 South. Winding Ozark roads. Not a launch road." if r else None
    elif 'highway 19' in t:                  r = set_road('highway_19_north');    return "Highway 19 North. Good straight past the creek bridge." if r else None
    elif 'highway 32' in t and 'west' in t:  r = set_road('highway_32_west');     return "32 West. Railroad crossing at mile 3 — watch it when wet." if r else None
    elif 'highway 32' in t:                  r = set_road('highway_32_east');     return "32 East. Fast road. Long straights past city limits." if r else None
    elif 'county' in t and '19' in t:        r = set_road('county_19');           return "County 19. Gravel after mile 2. Take it easy." if r else None
    elif 'downtown' in t:                    r = set_road('downtown_salem');       return "Downtown Salem. Keeping exhaust down." if r else None
    elif 'industrial' in t:                  r = set_road('industrial_park');      return "Industrial park. Empty on weekends. Good launch surface." if r else None
    elif 'school road' in t:                 r = set_road('salem_school_road');    return "School road. School zone during the week." if r else None
    elif 'big piney' in t or 'piney' in t:   r = set_road('big_piney_river_road'); return "Big Piney road. Narrow and winding. Not a speed road." if r else None
    elif 'fort leonard' in t or 'fort wood' in t: r = set_road('fort_leonard_wood'); return "Route to Fort Wood. Highway quality." if r else None
    elif 'dent county' in t:                 r = set_road('dent_county_road');     return "Dent County back road. Old pavement. Empty but rough." if r else None
    elif 'backroad' in t or 'back road' in t: r = set_road('backroad');            return "Back road. Best stretch is after the curve. Watch for deer after dark." if r else None
    elif 'rolla' in t:                       r = set_road('rolla_highway');        return "Murphy USA run. 31 miles north. Mile 4 straight is smooth." if r else None

    if 'road condition' in t or 'how is the road' in t or 'road memory' in t:
        if current_road: return f"{road_memory[current_road]['name']}. {road_memory[current_road]['notes']}"
        return "No road logged yet. Tell me where we are."
    if 'best launch' in t or 'where to launch' in t:
        if current_road and road_memory[current_road]['best_launch']:
            return f"Best launch spot — {road_memory[current_road]['best_launch']}."
        return "No road loaded. Tell me where we are."
    if 'hazards' in t or 'watch out' in t or 'anything ahead' in t:
        if current_road and road_memory[current_road]['hazards']:
            return f"Watch for {road_memory[current_road]['hazards']}."
        return "No road loaded."

    # ── MUSIC ────────────────────────────
    if any(x in t for x in ['playing','song is','now playing']):
        song = None
        for keyword in ['playing','song is','now playing']:
            if keyword in t:
                parts = t.split(keyword)
                if len(parts) > 1 and parts[1].strip():
                    song = parts[1].strip(); break
        if song and len(song) > 1:
            energy = 'hype' if any(x in song for x in ['hype','hard','fast','rage','aggressive']) else 'calm' if any(x in song for x in ['calm','slow','chill','quiet','soft']) else 'medium'
            set_music(song, energy); return f"Got it. {song.title()} loaded."
    if 'music off' in t or 'stop music' in t:
        music_state['playing'] = False; music_state['current_song'] = None; return "Music noted as off."
    if any(x in t for x in ['remember this song','link song','save this moment']):
        if music_state['current_song']:
            link_song_to_moment(music_state['current_song'], f"{datetime.now().strftime('%B %d')} — {truck_state['speed']} mph — E{truck_state['ethanol']}%")
            return f"Linked {music_state['current_song'].title()} to this moment."
        return "No song playing right now."
    if any(x in t for x in ['save lighting for song','link lighting','remember this lighting']):
        if music_state['current_song']:
            music_state['song_lighting'][music_state['current_song']] = {'interior': 140, 'underbody': 100, 'exhaust': truck_state['exhaust']}
            save_state(); return f"Lighting saved for {music_state['current_song'].title()}."
        return "No song playing right now."

    # ── PERSONAL BEST ────────────────────
    if any(x in t for x in ['launch log','best time','personal best','show launches']):
        show_launch_log(); return "Launch log above."
    if any(x in t for x in ['log launch','log a launch','that was']):
        time_val = None
        for word in t.split():
            try:
                val = float(word)
                if 2.0 < val < 10.0: time_val = val; break
            except: pass
        if time_val:
            new_best = log_launch(time_val)
            return f"{time_val} seconds. New best. Write that down." if new_best else f"{time_val} seconds logged. Best is still {personal_bests['best_0_60']}."
        log_launch(); return f"Launch logged. Total: {personal_bests['launch_count']}."

    # ── LIGHTS ───────────────────────────
    if 'bed light' in t and any(x in t for x in ['on','open']):
        truck_state['bed_lights'] = True; arduino_send("BED_ON"); return "Bed lights on."
    if 'bed light' in t and any(x in t for x in ['off','close']):
        truck_state['bed_lights'] = False; arduino_send("BED_OFF"); return "Bed lights off."
    if 'hood light' in t and 'on' in t:
        truck_state['hood_lights'] = True; arduino_send("HOOD_ON"); return "Hood lights on."
    if 'service mode' in t:
        arduino_send("SERVICE_MODE_ON"); return "Service mode active. Nothing will move unless you tell me."
    if any(x in t for x in ['cool down','aux pump','cooling']):
        truck_state['cool_on'] = True; arduino_send("COOL_ON"); return "Aux pump on. Cooling down."
    if any(x in t for x in ['high idle','idle up']):
        truck_state['idle_on'] = True; truck_state['rpm'] = 1500
        arduino_send("IDLE_ON:1500"); return "High idle active. 1500 RPM."
    if any(x in t for x in ["let's run","run it","push it","launch"]):
        truck_state['rpm'] = 5500; truck_state['speed'] = 60; truck_state['boost'] = 12
        arduino_send("TC_OFF"); arduino_send("EXHAUST:100"); return "Ready. Let's go."
    if any(x in t for x in ['slow down','cruising','back off']):
        truck_state['rpm'] = 1800; truck_state['speed'] = 45; truck_state['boost'] = 0; return "Backing off."

    # ── STATUS QUERIES ───────────────────
    if 'oil' in t:
        temp = truck_state['oil_temp']
        return f"Oil is at {temp}. Getting warm — back it down." if temp > 230 else f"Oil is at {temp}. Holding steady."
    if any(x in t for x in ['ethanol','how much ethanol','e85 level']):
        eth = truck_state['ethanol']
        if eth > 80: return f"E85 at {eth} percent. Full power map is active."
        if eth > 50: return f"Ethanol at {eth} percent. Still in the power map."
        return f"Ethanol is down to {eth} percent. Murphy USA in Rolla when you get a chance."
    if any(x in t for x in ['battery','voltage']):
        return f"Main battery at {truck_state['battery_main']} volts. Looking good."
    if any(x in t for x in ['boost','psi']):
        return f"Boost at {truck_state['boost']} PSI right now."
    if 'what song' in t or ('music' in t and 'what' in t):
        if music_state['playing']: return f"{music_state['current_song'].title()}. Energy is {music_state['energy']}."
        return "Nothing playing right now."
    if t == 'speed' or 'how fast' in t or 'current speed' in t:
        spd = truck_state['speed']
        if spd == 0: return "Sitting still right now."
        if spd < 35: return f"{spd} mph. Taking it easy."
        if spd < 60: return f"{spd} mph. Cruising."
        return f"{spd} mph. Moving."
    if 'status' in t: print_status(); return "Status printed above."
    if any(x in t for x in ['end session','session summary','how was the drive','recap','park it','going home']):
        return end_session_summary()
    if 'rpm' in t:
        for word in t.split():
            try:
                val = int(word)
                if 500 <= val <= 6500: truck_state['rpm'] = val; return f"RPM set to {val}."
            except: pass
        return f"RPM is at {truck_state['rpm']}."

    return None

# ── PRINT STATUS ─────────────────────────
def print_status():
    print("\n── TRUCK STATE ──────────────────────────")
    for label, val in [
        ("Oil Temp",   f"{truck_state['oil_temp']}°F"),
        ("Coolant",    f"{truck_state['coolant_temp']}°F"),
        ("RPM",        truck_state['rpm']),
        ("Speed",      f"{truck_state['speed']} mph"),
        ("Ethanol",    f"{truck_state['ethanol']}%"),
        ("Boost",      f"{truck_state['boost']} PSI"),
        ("Battery",    f"{truck_state['battery_main']}V"),
        ("Exhaust",    f"{truck_state['exhaust']}%"),
        ("Octane",     f"{truck_state['octane']} {truck_state['octane_mode']}"),
        ("TC",         'LOCKED' if truck_state['tc_locked'] else 'ON' if truck_state['tc_on'] else 'OFF'),
        ("Ghost Mode", 'ON' if truck_state['ghost_mode'] else 'OFF'),
        ("Headlights", 'ON' if truck_state['headlights'] else 'OFF'),
        ("High Beams", 'ON' if truck_state['high_beams'] else 'OFF'),
        ("Fog Lights", 'ON' if truck_state['fog_lights'] else 'OFF'),
        ("Hazards",    'ON' if truck_state['hazards'] else 'OFF'),
        ("AC",         'ON' if truck_state['ac_on'] else 'OFF'),
        ("Heat",       'ON' if truck_state['heat_on'] else 'OFF'),
        ("Fan Speed",  truck_state['fan_speed']),
        ("Windows",    f"FL={truck_state['windows']['fl']} FR={truck_state['windows']['fr']}"),
        ("Wipers",     truck_state['wipers']),
        ("Mood",       get_mood()),
        ("Tier",       f"{tier_state['current']} — {get_tier_label()}"),
        ("Profile",    driver_profiles[current_profile]['name']),
        ("Road",       road_memory[current_road]['name'] if current_road else 'None logged'),
        ("Music",      music_state['current_song'] if music_state['playing'] else 'Off'),
        ("Weather",    f"{weather['temp']}F — {weather['condition']}"),
        ("Best 0-60",  f"{personal_bests['best_0_60']}s" if personal_bests['best_0_60'] else 'None logged'),
        ("Launches",   personal_bests['launch_count']),
        ("Legacy",     'ACTIVE' if legacy['active'] else 'OFF'),
        ("Time",       datetime.now().strftime('%I:%M %p — %A')),
    ]:
        print(f"  {label+':':<14}{val}")
    print("─────────────────────────────────────────\n")

# ── SAFETY MONITOR ───────────────────────
def safety_monitor():
    last_oil = last_bat = last_eth = last_qual = 0
    while True:
        now = time.time(); oil = truck_state['oil_temp']
        v = truck_state['battery_main']; eth = truck_state['ethanol']; boost = truck_state['boost']

        if oil > 235 and now - last_oil > 30:
            msg = f"Oil is at {oil}. I had to step in. Closing cutouts."
            print(f"\n[ARCHER] {msg}"); speak(msg)
            truck_state['exhaust'] = 0; truck_state['tc_locked'] = True
            arduino_send("EXHAUST:0"); arduino_send("TC_LOCK"); arduino_send("COOL_ON")
            log_moment('warning', f"Oil hit {oil}F — Archer stepped in"); last_oil = now
        elif oil > 220 and now - last_oil > 60:
            msg = f"Oil is climbing — {oil} degrees. Keep an eye on it."
            print(f"\n[ARCHER] {msg}"); speak(msg); last_oil = now
        elif oil < 210 and truck_state['tc_locked'] and truck_state['cool_on']:
            msg = "Oil is back down. Everything is yours."
            print(f"\n[ARCHER] {msg}"); speak(msg)
            truck_state['tc_locked'] = False; truck_state['cool_on'] = False
            arduino_send("TC_RELEASE"); arduino_send("COOL_OFF")

        if v < 11.8 and now - last_bat > 60:
            msg = "Battery dropping. Connecting auxiliary."
            print(f"\n[ARCHER] {msg}"); speak(msg); arduino_send("AUXBAT_ON"); last_bat = now

        if eth < 30 and boost > 5 and now - last_eth > 120:
            msg = "Low ethanol under boost. Knock risk is real right now."
            print(f"\n[ARCHER] {msg}"); speak(msg); last_eth = now

        if awareness['drive_quality'] < 60 and now - last_qual > 300:
            msg = "Drive quality is down. Lot of hard events this session."
            print(f"\n[ARCHER] {msg}"); speak(msg); last_qual = now

        time.sleep(5)

# ── WEB DISPLAY HTML ─────────────────────
DISPLAY_HTML = '''<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="Archer">
<meta name="theme-color" content="#cc0000">
<link rel="manifest" href="/manifest.json">
<title>Archer</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#000; color:#fff; font-family:'Courier New',monospace; height:100vh; overflow:hidden; user-select:none; }
#display { width:100vw; height:100vh; display:flex; flex-direction:column; }

/* HEADER */
#header { background:#111; border-bottom:2px solid #cc0000; padding:4px 10px; display:flex; justify-content:space-between; align-items:center; height:34px; flex-shrink:0; }
#header h1 { color:#cc0000; font-size:16px; font-weight:bold; letter-spacing:4px; }
.hdr-right { display:flex; align-items:center; gap:8px; }
#profile-name { color:#555; font-size:10px; letter-spacing:2px; }
#clients-badge { background:#cc0000; color:#fff; font-size:9px; padding:1px 5px; border-radius:8px; letter-spacing:1px; }
#audio-btn,#mic-btn { font-size:10px; color:#555; cursor:pointer; letter-spacing:1px; padding:2px 4px; border:1px solid #222; border-radius:3px; }

/* TABS */
#mode-tabs { display:flex; background:#0a0a0a; border-bottom:1px solid #1a1a1a; height:26px; flex-shrink:0; }
.tab { flex:1; text-align:center; font-size:9px; letter-spacing:1px; color:#444; line-height:26px; cursor:pointer; border-right:1px solid #1a1a1a; transition:all 0.2s; }
.tab:last-child { border-right:none; }
.tab.active { color:#cc0000; border-bottom:2px solid #cc0000; }

/* CONTENT */
#content { flex:1; overflow:hidden; position:relative; min-height:0; }
.mode-screen { display:none; width:100%; height:100%; padding:6px; overflow-y:auto; }
.mode-screen.active { display:flex; flex-direction:column; gap:6px; }

/* DATA GRID */
.data-grid { display:grid; grid-template-columns:1fr 1fr; gap:5px; }
.data-box { background:#0d0d0d; border:1px solid #1a1a1a; border-radius:4px; padding:6px 8px; }
.data-label { font-size:8px; letter-spacing:2px; color:#555; margin-bottom:2px; }
.data-value { font-size:20px; font-weight:bold; color:#fff; }
.good { color:#00cc44; } .warn { color:#ffaa00; } .danger { color:#cc0000; }

/* GAUGE */
.gauge-wrap { display:flex; flex-direction:column; align-items:center; gap:2px; }
.gauge-label { font-size:8px; color:#555; letter-spacing:2px; }
.gauge-val   { font-size:11px; font-weight:bold; }
canvas.gauge { display:block; }

/* GRAPH */
.graph-wrap { background:#0d0d0d; border:1px solid #1a1a1a; border-radius:4px; padding:4px 6px; }
.graph-title { font-size:8px; color:#555; letter-spacing:1px; margin-bottom:2px; display:flex; justify-content:space-between; }
canvas.graph { width:100%; border-radius:2px; }

/* VIRTUAL TRUCK */
#truck-svg { width:100%; max-width:320px; margin:0 auto; display:block; }

/* HEALTH */
.health-item { display:flex; align-items:center; justify-content:space-between; padding:5px 3px; border-bottom:1px solid #0d0d0d; }
.health-name { font-size:9px; color:#777; letter-spacing:1px; flex:1; }
.health-val  { font-size:11px; font-weight:bold; flex:1; text-align:center; }
.health-dot  { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.dot-good { background:#00cc44; box-shadow:0 0 5px #00cc44; }
.dot-warn { background:#ffaa00; box-shadow:0 0 5px #ffaa00; }
.dot-danger { background:#cc0000; box-shadow:0 0 5px #cc0000; }

/* SHOW MODE */
#mode-show { align-items:center; justify-content:center; border:2px solid #cc0000; position:relative; }
#show-pulse { position:absolute; inset:0; border:2px solid transparent; animation:pulse-border 1s ease-in-out infinite; pointer-events:none; }
@keyframes pulse-border { 0%,100%{border-color:#cc0000;} 50%{border-color:transparent;} }
.show-title   { font-size:13px; color:#cc0000; letter-spacing:4px; margin-bottom:16px; }
.show-message { font-size:18px; color:#fff; text-align:center; padding:0 16px; line-height:1.6; max-width:280px; }
.show-data    { position:absolute; bottom:10px; left:0; right:0; display:flex; justify-content:space-around; font-size:9px; color:#555; }

/* ARCHER BAR */
#archer-bar { background:#0a0a0a; border-top:1px solid #1a1a1a; padding:4px 10px; height:28px; display:flex; align-items:center; gap:6px; flex-shrink:0; }
#archer-indicator { width:5px; height:5px; background:#cc0000; border-radius:50%; flex-shrink:0; animation:blink 2s ease-in-out infinite; }
@keyframes blink { 0%,100%{opacity:1;} 50%{opacity:0.2;} }
#archer-msg { font-size:10px; color:#666; letter-spacing:0.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

/* VEHICLE CONTROL BUTTONS */
.vc-btn {
  background:#0d0d0d;border:1px solid #1a1a1a;border-radius:8px;
  padding:10px 4px 6px;cursor:pointer;color:#888;font-family:"Courier New",monospace;
  font-size:7px;letter-spacing:1px;display:flex;flex-direction:column;
  align-items:center;gap:3px;transition:all 0.15s;width:100%;
}
.vc-btn:active { transform:scale(0.94); }
.vc-btn.active { background:#1a0000;border-color:#cc0000;color:#cc0000; }
.vc-btn-red    { background:#1a0000;border-color:#cc0000;color:#cc0000; }
.vc-btn-green  { background:#001a00;border-color:#00cc44;color:#00cc44; }
.vc-btn-purple { background:#0a0010;border-color:#8800ff;color:#aa44ff; }

/* SMOKE ANIMATION */
@keyframes smokeRise {
  0%   { transform:translateY(0)   scale(1);   opacity:0.6; }
  60%  { transform:translateY(-120%) scale(1.8); opacity:0.3; }
  100% { transform:translateY(-220%) scale(2.6); opacity:0; }
}

/* WARNING */
#warning-banner { display:none; background:#cc0000; color:#fff; text-align:center; font-size:10px; letter-spacing:2px; padding:2px; animation:flash 0.5s ease-in-out infinite; flex-shrink:0; }
@keyframes flash { 0%,100%{opacity:1;} 50%{opacity:0.6;} }
#warning-banner.visible { display:block; }

/* FOOTER */
#footer { background:#060606; border-top:1px solid #111; padding:2px 10px; display:flex; justify-content:space-between; align-items:center; height:20px; flex-shrink:0; }
.footer-item { font-size:8px; color:#333; letter-spacing:1px; }
.footer-item.active { color:#cc0000; }
</style>
</head>
<body>
<div id="display">

<div id="warning-banner">&#9888; WARNING &mdash; <span id="warning-text"></span></div>

<div id="radar-banner" style="display:none;padding:3px 10px;font-size:10px;letter-spacing:2px;font-family:monospace;text-align:center;flex-shrink:0">
  <span id="radar-band">Ka</span> &bull; <span id="radar-bars">●●●○○</span> &bull; <span id="radar-dir">FRONT</span>
</div>

<div id="header">
  <h1>ARCHER</h1>
  <div class="hdr-right">
    <span id="clients-badge">0 ONLINE</span>
    <span id="audio-btn" onclick="connectAudio()">&#128263; AUDIO</span>
    <span id="mic-btn" onclick="startMic()">&#127908; MIC</span>
    <a href="/terminal" target="_blank" style="font-size:9px;color:#333;letter-spacing:1px;text-decoration:none;padding:2px 4px;border:1px solid #1a1a1a;border-radius:3px">TERMINAL</a>
        <div id="profile-name">AYDEN</div>
  </div>
</div>

<div id="mode-tabs">
  <div class="tab active" onclick="setMode('default')">DASH</div>
  <div class="tab" onclick="setMode('perf')">PERF</div>
  <div class="tab" onclick="setMode('graphs')">GRAPHS</div>
  <div class="tab" onclick="setMode('truck')">TRUCK</div>
  <div class="tab" onclick="setMode('health')">HEALTH</div>
  <div class="tab" onclick="setMode('show')">SHOW</div>
  <div class="tab" onclick="setMode('build')">BUILD</div>
  <div class="tab" onclick="setMode('live')">LIVE</div>
  <div class="tab" onclick="setMode('cams')">CAMS</div>
  <div class="tab" id="claw-tab" onclick="setMode('claw')" style="display:none">CLAW</div>
  <div class="tab" onclick="setMode('music')">MUSIC</div>
</div>

<div id="content">

  <!-- DEFAULT DASH — FULL SCREEN TRUCK DASHBOARD -->
  <div id="mode-default" class="mode-screen active" style="padding:0;gap:0;background:#000">

    <!-- TOP ROW: Speed + RPM big numbers -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px;padding:6px 6px 4px">
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:10px 8px;text-align:center">
        <div style="font-size:8px;color:#555;letter-spacing:3px;margin-bottom:2px">SPEED</div>
        <div id="dd-speed" style="font-size:52px;font-weight:900;color:#fff;line-height:1;letter-spacing:-3px;font-family:monospace">0</div>
        <div style="font-size:9px;color:#444;letter-spacing:2px">MPH</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:10px 8px;text-align:center">
        <div style="font-size:8px;color:#555;letter-spacing:3px;margin-bottom:2px">RPM</div>
        <div id="dd-rpm" style="font-size:52px;font-weight:900;color:#fff;line-height:1;letter-spacing:-3px;font-family:monospace">750</div>
        <div style="font-size:9px;color:#444;letter-spacing:2px">×1000</div>
      </div>
    </div>

    <!-- RPM BAR -->
    <div style="padding:0 6px 4px">
      <div style="background:#111;border-radius:3px;height:10px;overflow:hidden;position:relative">
        <div id="dd-rpm-bar" style="height:100%;border-radius:3px;background:linear-gradient(90deg,#00cc44 0%,#ffaa00 60%,#cc0000 85%,#ff0000 100%);width:12%;transition:width 0.15s"></div>
        <div style="position:absolute;right:15%;top:0;width:2px;height:100%;background:#cc0000;opacity:0.6"></div>
      </div>
    </div>

    <!-- BOOST BIG -->
    <div style="padding:0 6px 4px">
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between">
        <div>
          <div style="font-size:8px;color:#555;letter-spacing:3px">BOOST</div>
          <div style="display:flex;align-items:baseline;gap:4px">
            <span id="dd-boost" style="font-size:36px;font-weight:900;color:#ff6600;font-family:monospace;line-height:1">0</span>
            <span style="font-size:11px;color:#555">PSI</span>
          </div>
        </div>
        <div style="flex:1;margin:0 12px">
          <div style="background:#111;border-radius:3px;height:8px;overflow:hidden">
            <div id="dd-boost-bar" style="height:100%;border-radius:3px;background:#ff6600;width:0%;transition:width 0.15s"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:7px;color:#333;margin-top:2px">
            <span>-10</span><span>0</span><span>+10</span><span>+20</span><span>+30</span>
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-size:8px;color:#555;letter-spacing:2px">GEAR</div>
          <div id="dd-gear" style="font-size:28px;font-weight:900;color:#fff;font-family:monospace;line-height:1">1</div>
        </div>
      </div>
    </div>

    <!-- STATS ROW: Oil / Battery / E85 / Mode -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:2px;padding:0 6px 4px">
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 4px;text-align:center">
        <div style="font-size:7px;color:#555;letter-spacing:2px">OIL</div>
        <div id="dd-oil" style="font-size:18px;font-weight:bold;color:#ffaa00;font-family:monospace">195°</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 4px;text-align:center">
        <div style="font-size:7px;color:#555;letter-spacing:2px">BATT</div>
        <div id="dd-bat" style="font-size:18px;font-weight:bold;color:#00cc44;font-family:monospace">14.2V</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 4px;text-align:center">
        <div style="font-size:7px;color:#555;letter-spacing:2px">E85</div>
        <div id="dd-eth" style="font-size:18px;font-weight:bold;color:#00aaff;font-family:monospace">82%</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 4px;text-align:center">
        <div style="font-size:7px;color:#555;letter-spacing:2px">MODE</div>
        <div id="dd-mode" style="font-size:11px;font-weight:bold;color:#cc0000;font-family:monospace;letter-spacing:1px">SPORT</div>
      </div>
    </div>

    <!-- ARCHER MESSAGE -->
    <div style="padding:0 6px 4px">
      <div style="background:#080808;border:1px solid #1a1a1a;border-radius:5px;padding:8px 10px;min-height:36px;display:flex;align-items:center">
        <span style="font-size:8px;color:#cc0000;letter-spacing:2px;margin-right:8px;flex-shrink:0">ARCHER</span>
        <span id="dd-msg" style="font-size:11px;color:#888;font-style:italic;line-height:1.4">Ready.</span>
      </div>
    </div>

    <!-- DRIVE SCORE + CONDITIONS -->
    <div style="display:grid;grid-template-columns:auto 1fr;gap:6px;padding:0 6px 6px;align-items:center">
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 10px;text-align:center">
        <div style="font-size:7px;color:#555;letter-spacing:2px">SCORE</div>
        <div id="dd-score" style="font-size:24px;font-weight:900;color:#00cc44;font-family:monospace;line-height:1">A</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:5px;padding:6px 10px">
        <div style="font-size:7px;color:#555;letter-spacing:2px;margin-bottom:3px">CONDITIONS</div>
        <div id="dd-conditions" style="font-size:10px;color:#666">Salem MO — Loading...</div>
      </div>
    </div>

  </div>

  <!-- PERF MODE -->
  <div id="mode-perf" class="mode-screen">
    <div style="text-align:center;padding:8px 0 2px">
      <div style="font-size:9px;color:#555;letter-spacing:3px">RPM</div>
      <div id="p-rpm" style="font-size:56px;font-weight:bold;color:#fff;line-height:1;letter-spacing:-2px">750</div>
    </div>
    <div style="padding:0 4px">
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#555;margin-bottom:2px"><span>RPM</span><span id="p-rpm-pct">12%</span></div>
      <div style="background:#111;border-radius:2px;height:7px;overflow:hidden"><div id="p-rpm-bar" style="height:100%;border-radius:2px;background:linear-gradient(90deg,#00cc44,#ffaa00,#cc0000);width:12%;transition:width 0.3s"></div></div>
    </div>
    <div style="padding:0 4px">
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#555;margin-bottom:2px"><span>BOOST</span><span id="p-boost-val">0 PSI</span></div>
      <div style="background:#111;border-radius:2px;height:7px;overflow:hidden"><div id="p-boost-bar" style="height:100%;border-radius:2px;background:#ff6600;width:0%;transition:width 0.3s"></div></div>
    </div>
    <div style="padding:0 4px">
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#555;margin-bottom:2px"><span>E85</span><span id="p-eth-val">82%</span></div>
      <div style="background:#111;border-radius:2px;height:7px;overflow:hidden"><div id="p-eth-bar" style="height:100%;border-radius:2px;background:#00aaff;width:82%;transition:width 0.3s"></div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px">
      <div class="data-box" style="text-align:center"><div class="data-label">OIL</div><div class="data-value" id="p-oil" style="font-size:16px">195F</div></div>
      <div class="data-box" style="text-align:center"><div class="data-label">BEST 0-60</div><div class="data-value" id="p-best" style="font-size:16px;color:#cc0000">--</div></div>
      <div class="data-box" style="text-align:center"><div class="data-label">DRIVE Q</div><div class="data-value" id="p-quality" style="font-size:16px">100</div></div>
    </div>
    <div style="padding:0 4px">
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#555;margin-bottom:2px"><span>DRIVE QUALITY</span><span id="p-quality-pct">100%</span></div>
      <div style="background:#111;border-radius:2px;height:7px;overflow:hidden"><div id="p-quality-bar" style="height:100%;border-radius:2px;background:#00cc44;width:100%;transition:width 0.3s"></div></div>
    </div>
  </div>

  <!-- GRAPHS MODE -->
  <div id="mode-graphs" class="mode-screen">
    <div class="graph-wrap">
      <div class="graph-title"><span>RPM HISTORY</span><span id="g-rpm-peak" style="color:#ffaa00">PEAK: 0</span></div>
      <canvas class="graph" id="graph-rpm" height="55"></canvas>
    </div>
    <div class="graph-wrap">
      <div class="graph-title"><span>BOOST HISTORY</span><span id="g-boost-peak" style="color:#ff6600">PEAK: 0 PSI</span></div>
      <canvas class="graph" id="graph-boost" height="55"></canvas>
    </div>
    <div class="graph-wrap">
      <div class="graph-title"><span>OIL TEMP HISTORY</span><span id="g-oil-peak" style="color:#cc0000">PEAK: 0F</span></div>
      <canvas class="graph" id="graph-oil" height="55"></canvas>
    </div>
    <div class="graph-wrap">
      <div class="graph-title"><span>BATTERY HISTORY</span><span id="g-bat-last" style="color:#00cc44">NOW: 13.8V</span></div>
      <canvas class="graph" id="graph-bat" height="55"></canvas>
    </div>
  </div>

  <!-- VIRTUAL TRUCK MODE -->
  <div id="mode-truck" class="mode-screen" style="padding:0;background:#000;overflow-y:auto">

    <!-- LOCKED VIEW — shown to non-Tier-1 -->
    <div id="truck-locked" style="display:none;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:40px 20px;text-align:center">
      <div style="font-size:40px;margin-bottom:16px">&#128274;</div>
      <div style="font-size:11px;color:#cc0000;letter-spacing:3px;margin-bottom:8px">ACCESS DENIED</div>
      <div style="font-size:9px;color:#333;letter-spacing:1px">TIER 1 ONLY</div>
    </div>

    <!-- CONTROL VIEW — Tier 1 only -->
    <div id="truck-controls" style="display:flex;flex-direction:column;gap:0">

      <!-- Header -->
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px 6px;border-bottom:1px solid #111">
        <div>
          <div style="font-size:8px;color:#444;letter-spacing:3px">VEHICLE CONTROL</div>
          <div style="font-size:13px;color:#fff;letter-spacing:1px;font-weight:bold">2026 VISION</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:8px;color:#444;letter-spacing:2px">DRIVER</div>
          <div style="font-size:11px;color:#cc0000;letter-spacing:2px" id="vc-driver">AYDEN</div>
        </div>
      </div>

      <!-- Live stats strip -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#111;border-bottom:1px solid #111">
        <div style="background:#000;padding:8px 6px;text-align:center">
          <div style="font-size:7px;color:#444;letter-spacing:1px">RPM</div>
          <div style="font-size:16px;font-weight:bold;color:#fff" id="vc-rpm">750</div>
        </div>
        <div style="background:#000;padding:8px 6px;text-align:center">
          <div style="font-size:7px;color:#444;letter-spacing:1px">BOOST</div>
          <div style="font-size:16px;font-weight:bold;color:#ff6600" id="vc-boost">0</div>
        </div>
        <div style="background:#000;padding:8px 6px;text-align:center">
          <div style="font-size:7px;color:#444;letter-spacing:1px">OIL</div>
          <div style="font-size:16px;font-weight:bold;color:#fff" id="vc-oil">195F</div>
        </div>
        <div style="background:#000;padding:8px 6px;text-align:center">
          <div style="font-size:7px;color:#444;letter-spacing:1px">E85</div>
          <div style="font-size:16px;font-weight:bold;color:#00aaff" id="vc-eth">82%</div>
        </div>
      </div>

      <!-- Section: LIGHTING -->
      <div style="padding:8px 12px 4px">
        <div style="font-size:7px;color:#444;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #111;padding-bottom:4px">LIGHTING</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px">
          <button id="btn-headlights" class="vc-btn" onclick="vcCmd('headlights')">
            <span style="font-size:18px">&#128161;</span>
            <span>LIGHTS</span>
          </button>
          <button id="btn-highbeams" class="vc-btn" onclick="vcCmd('high beams')">
            <span style="font-size:18px">&#9728;&#65039;</span>
            <span>HI BEAM</span>
          </button>
          <button id="btn-foglights" class="vc-btn" onclick="vcCmd('fog lights')">
            <span style="font-size:18px">&#127807;</span>
            <span>FOG</span>
          </button>
          <button id="btn-hazards" class="vc-btn" onclick="vcCmd('hazards')">
            <span style="font-size:18px">&#9888;&#65039;</span>
            <span>HAZARDS</span>
          </button>
        </div>
      </div>

      <!-- Section: WINDOWS & CLIMATE -->
      <div style="padding:6px 12px 4px">
        <div style="font-size:7px;color:#444;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #111;padding-bottom:4px">COMFORT</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px">
          <button id="btn-windows" class="vc-btn" onclick="vcCmd('windows down')">
            <span style="font-size:18px">&#129695;</span>
            <span>WINDOWS</span>
          </button>
          <button id="btn-ac" class="vc-btn" onclick="vcCmd('AC on')">
            <span style="font-size:18px">&#10052;&#65039;</span>
            <span>A/C</span>
          </button>
          <button id="btn-heat" class="vc-btn" onclick="vcCmd('heat on')">
            <span style="font-size:18px">&#128293;</span>
            <span>HEAT</span>
          </button>
          <button id="btn-seatheat" class="vc-btn" onclick="vcCmd('seat heat on')">
            <span style="font-size:18px">&#129681;</span>
            <span>SEAT HT</span>
          </button>
        </div>
      </div>

      <!-- Section: PERFORMANCE -->
      <div style="padding:6px 12px 4px">
        <div style="font-size:7px;color:#444;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #111;padding-bottom:4px">PERFORMANCE</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px">
          <button id="btn-tc" class="vc-btn" onclick="vcCmd('TC off')">
            <span style="font-size:18px">&#128694;</span>
            <span>TC OFF</span>
          </button>
          <button id="btn-ghost" class="vc-btn" onclick="vcCmd('ghost mode')">
            <span style="font-size:18px">&#128123;</span>
            <span>GHOST</span>
          </button>
          <button id="btn-exhaust" class="vc-btn" onclick="showExhaustSlider()">
            <span style="font-size:18px">&#128168;</span>
            <span>EXHAUST</span>
          </button>
          <button id="btn-drivemode" class="vc-btn" onclick="vcCmd('sport mode')">
            <span style="font-size:18px">&#127950;</span>
            <span>SPORT</span>
          </button>
        </div>

        <!-- Exhaust slider — hidden until tapped -->
        <div id="exhaust-slider-wrap" style="display:none;margin-top:8px;padding:8px;background:#0d0d0d;border-radius:6px;border:1px solid #1a1a1a">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <span style="font-size:8px;color:#555;letter-spacing:2px">EXHAUST LEVEL</span>
            <span style="font-size:11px;color:#ff6600;font-weight:bold" id="exhaust-val">30%</span>
          </div>
          <input type="range" min="0" max="100" value="30" id="exhaust-range"
            style="width:100%;accent-color:#cc0000"
            oninput="document.getElementById('exhaust-val').textContent=this.value+'%'"
            onchange="vcCmd('exhaust '+this.value)"/>
        </div>
      </div>

      <!-- Section: QUICK ACTIONS -->
      <div style="padding:6px 12px 8px">
        <div style="font-size:7px;color:#444;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #111;padding-bottom:4px">QUICK ACTIONS</div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px">
          <button class="vc-btn vc-btn-red" onclick="vcCmd('dyno')">
            <span style="font-size:18px">&#9889;</span>
            <span>DYNO</span>
          </button>
          <button class="vc-btn vc-btn-green" onclick="vcCmd('ready to run')">
            <span style="font-size:18px">&#9989;</span>
            <span>LAUNCH CHECK</span>
          </button>
          <button class="vc-btn vc-btn-purple" onclick="vcCmd('flex')">
            <span style="font-size:18px">&#127381;</span>
            <span>SHOW MODE</span>
          </button>
          <button class="vc-btn" onclick="vcCmd('horn')">
            <span style="font-size:18px">&#128227;</span>
            <span>HORN</span>
          </button>
          <button class="vc-btn" onclick="vcCmd('wipers on')">
            <span style="font-size:18px">&#127783;&#65039;</span>
            <span>WIPERS</span>
          </button>
          <button class="vc-btn" onclick="vcCmd('engine off')">
            <span style="font-size:18px">&#128308;</span>
            <span>ENGINE OFF</span>
          </button>
        </div>
      </div>

    </div>
  </div>

  <!-- HEALTH -->
  <div id="mode-health" class="mode-screen">
    <div style="font-size:9px;color:#cc0000;letter-spacing:3px;padding:3px 0;border-bottom:1px solid #cc0000;margin-bottom:3px">SYSTEM HEALTH</div>
    <div id="health-list"></div>
  </div>

  <!-- SHOW MODE -->
  <div id="mode-show" class="mode-screen">
    <div id="show-pulse"></div>
    <div class="show-title">SHOW MODE</div>
    <div class="show-message" id="show-msg">Alright. Watch this.</div>
    <div class="show-data">
      <span id="show-rpm">750 RPM</span>
      <span id="show-oil">195F</span>
      <span id="show-eth">E82%</span>
    </div>
  </div>

  <!-- BUILD MODE -->
  <div id="mode-build" class="mode-screen">
    <div style="font-size:9px;color:#cc0000;letter-spacing:3px;padding:3px 0;border-bottom:1px solid #cc0000;margin-bottom:6px">2026 VISION BUILD TRACKER</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:6px">
      <div class="data-box" style="text-align:center">
        <div class="data-label">TOTAL SPENT</div>
        <div class="data-value good" id="b-spent" style="font-size:16px">/bin/sh</div>
      </div>
      <div class="data-box" style="text-align:center">
        <div class="data-label">PARTS TRACKED</div>
        <div class="data-value" id="b-parts" style="font-size:16px">0</div>
      </div>
      <div class="data-box" style="text-align:center">
        <div class="data-label">EST HP</div>
        <div class="data-value danger" id="b-hp" style="font-size:16px">556</div>
      </div>
      <div class="data-box" style="text-align:center">
        <div class="data-label">ODOMETER</div>
        <div class="data-value" id="b-odo" style="font-size:16px">0</div>
      </div>
    </div>
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin-bottom:4px">FAULT CODES</div>
    <div id="b-faults" style="font-size:10px;color:#00cc44;padding:4px 0">No active codes</div>
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin:6px 0 4px">BUILD TIMELINE</div>
    <div style="background:#0d0d0d;border-radius:4px;padding:8px">
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#555;margin-bottom:4px">
        <span>2026 START</span><span>2031 TARGET</span>
      </div>
      <div style="background:#111;border-radius:2px;height:6px;overflow:hidden">
        <div id="b-progress" style="height:100%;border-radius:2px;background:linear-gradient(90deg,#cc0000,#ff6600);width:2%;transition:width 1s"></div>
      </div>
      <div style="font-size:8px;color:#444;margin-top:3px;text-align:center" id="b-progress-label">Year 1 of 5</div>
    </div>
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin:6px 0 4px">WEATHER CONDITIONS</div>
    <div id="b-conditions" style="font-size:10px;color:#777;line-height:1.6">Loading...</div>
  </div>

  <!-- LIVE DATA MODE -->
  <div id="mode-live" class="mode-screen">
    <div style="font-size:9px;color:#cc0000;letter-spacing:3px;padding:3px 0;border-bottom:1px solid #cc0000;margin-bottom:4px">LIVE SENSOR DATA</div>

    <!-- Engine row -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:4px">
      <div class="data-box"><div class="data-label">AFR</div><div class="data-value" id="l-afr" style="font-size:18px">14.7</div></div>
      <div class="data-box"><div class="data-label">TIMING</div><div class="data-value" id="l-timing" style="font-size:18px">18°</div></div>
      <div class="data-box"><div class="data-label">KNOCK</div><div class="data-value good" id="l-knock" style="font-size:18px">0</div></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:4px">
      <div class="data-box"><div class="data-label">TPS</div><div class="data-value" id="l-tps" style="font-size:18px">0%</div></div>
      <div class="data-box"><div class="data-label">OIL PSI</div><div class="data-value" id="l-oilpsi" style="font-size:18px">45</div></div>
      <div class="data-box"><div class="data-label">INTAKE</div><div class="data-value" id="l-intake" style="font-size:18px">75F</div></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:4px">
      <div class="data-box"><div class="data-label">TRANS</div><div class="data-value" id="l-trans" style="font-size:18px">160F</div></div>
      <div class="data-box"><div class="data-label">ALT V</div><div class="data-value" id="l-alt" style="font-size:18px">14.2V</div></div>
      <div class="data-box"><div class="data-label">GEAR</div><div class="data-value" id="l-gear" style="font-size:18px">1</div></div>
    </div>

    <!-- G-Force -->
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin:4px 0 3px">G-FORCE</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-bottom:4px">
      <div class="data-box"><div class="data-label">LONG NOW</div><div class="data-value" id="l-glong" style="font-size:16px">0.0g</div></div>
      <div class="data-box"><div class="data-label">PEAK LONG</div><div class="data-value danger" id="l-gpeak" style="font-size:16px">0.0g</div></div>
      <div class="data-box"><div class="data-label">SPEED</div><div class="data-value" id="l-speed" style="font-size:16px">0 MPH</div></div>
    </div>

    <!-- Drag timer -->
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin:4px 0 3px">DRAG TIMER</div>
    <div style="background:#0d0d0d;border-radius:4px;padding:8px;border:1px solid #1a1a1a">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-size:9px;color:#555" id="l-drag-stage">IDLE</span>
        <span style="font-size:11px;color:#cc0000;font-weight:bold" id="l-drag-best">BEST: --</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:3px">
        <div style="text-align:center"><div style="font-size:7px;color:#333">60FT</div><div style="font-size:10px;color:#fff" id="l-60ft">--</div></div>
        <div style="text-align:center"><div style="font-size:7px;color:#333">330</div><div style="font-size:10px;color:#fff" id="l-330ft">--</div></div>
        <div style="text-align:center"><div style="font-size:7px;color:#333">660</div><div style="font-size:10px;color:#fff" id="l-660ft">--</div></div>
        <div style="text-align:center"><div style="font-size:7px;color:#333">ET</div><div style="font-size:10px;color:#cc0000;font-weight:bold" id="l-et">--</div></div>
        <div style="text-align:center"><div style="font-size:7px;color:#333">MPH</div><div style="font-size:10px;color:#cc0000;font-weight:bold" id="l-mph">--</div></div>
      </div>
    </div>
  </div>

  <!-- CAMERAS MODE -->
  <div id="mode-cams" class="mode-screen">
    <div style="font-size:9px;color:#cc0000;letter-spacing:3px;padding:3px 0;border-bottom:1px solid #cc0000;margin-bottom:6px">SURVEILLANCE SYSTEM</div>

    <!-- Status bar -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-bottom:6px">
      <div class="data-box" style="text-align:center">
        <div class="data-label">STATUS</div>
        <div id="cam-status" style="font-size:11px;font-weight:bold;color:#555">DISARMED</div>
      </div>
      <div class="data-box" style="text-align:center">
        <div class="data-label">VALET EVENTS</div>
        <div id="cam-valet" style="font-size:11px;font-weight:bold;color:#fff">0</div>
      </div>
      <div class="data-box" style="text-align:center">
        <div class="data-label">PARKING</div>
        <div id="cam-parking" style="font-size:11px;font-weight:bold;color:#555">OFF</div>
      </div>
    </div>

    <!-- Camera feeds -->
    <div style="font-size:8px;color:#555;letter-spacing:2px;margin-bottom:4px">CAMERA FEEDS</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px">
      <div id="cam-front-wrap" style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:4px;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden">
        <div style="font-size:7px;color:#333;letter-spacing:1px;position:absolute;top:4px;left:6px">FRONT</div>
        <div id="cam-front-status" style="font-size:8px;color:#333">OFFLINE</div>
        <iframe id="cam-front-feed" src="" style="display:none;width:100%;height:100%;border:none" allow="camera"></iframe>
      </div>
      <div id="cam-rear-wrap" style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:4px;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden">
        <div style="font-size:7px;color:#333;letter-spacing:1px;position:absolute;top:4px;left:6px">REAR</div>
        <div id="cam-rear-status" style="font-size:8px;color:#333">OFFLINE</div>
        <iframe id="cam-rear-feed" src="" style="display:none;width:100%;height:100%;border:none" allow="camera"></iframe>
      </div>
      <div id="cam-cab-wrap" style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:4px;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden">
        <div style="font-size:7px;color:#333;letter-spacing:1px;position:absolute;top:4px;left:6px">CAB</div>
        <div id="cam-cab-status" style="font-size:8px;color:#333">OFFLINE</div>
        <iframe id="cam-cab-feed" src="" style="display:none;width:100%;height:100%;border:none" allow="camera"></iframe>
      </div>
      <div id="cam-left-wrap" style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:4px;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden">
        <div style="font-size:7px;color:#333;letter-spacing:1px;position:absolute;top:4px;left:6px">LEFT</div>
        <div id="cam-left-status" style="font-size:8px;color:#333">OFFLINE</div>
        <iframe id="cam-left-feed" src="" style="display:none;width:100%;height:100%;border:none" allow="camera"></iframe>
      </div>
    </div>

    <!-- Arm/disarm buttons -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px">
      <button class="vc-btn vc-btn-red" onclick="sendCmd('arm surveillance')">
        <span style="font-size:16px">&#128274;</span><span>ARM</span>
      </button>
      <button class="vc-btn" onclick="sendCmd('disarm')">
        <span style="font-size:16px">&#128275;</span><span>DISARM</span>
      </button>
    </div>

    <!-- Parking location input -->
    <div style="margin-top:5px;display:flex;gap:5px">
      <input id="park-loc" placeholder="Parking location..." style="flex:1;background:#0d0d0d;border:1px solid #1a1a1a;border-radius:6px;padding:8px;color:#fff;font-family:monospace;font-size:10px"/>
      <button class="vc-btn" style="width:80px" onclick="parkHere()"><span>PARK HERE</span></button>
    </div>
  </div>

  <!-- OPENCLAW MODE — Tier 1 and 2 only -->
  <div id="mode-claw" class="mode-screen">
    <div style="display:flex;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid #00cc44;margin-bottom:6px">
      <div style="font-size:9px;color:#00cc44;letter-spacing:3px">OPENCLAW AGENT</div>
      <div id="claw-status-dot" style="width:6px;height:6px;border-radius:50%;background:#333"></div>
      <div id="claw-status-text" style="font-size:8px;color:#333;letter-spacing:1px">OFFLINE</div>
    </div>

    <!-- Chat history -->
    <div id="claw-history" style="flex:1;overflow-y:auto;font-size:10px;line-height:1.7;min-height:120px;max-height:220px;padding:2px 0;margin-bottom:6px">
      <div style="color:#333;font-size:9px">OpenClaw can browse the web, check email, send messages, search for parts prices and more. Only you and your passenger can use this.</div>
    </div>

    <!-- Quick actions -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:6px">
      <button class="vc-btn" onclick="clawQuick('check my email')"><span style="font-size:14px">📧</span><span>EMAIL</span></button>
      <button class="vc-btn" onclick="clawQuick('latest news')"><span style="font-size:14px">📰</span><span>NEWS</span></button>
      <button class="vc-btn" onclick="clawQuick('search RockAuto for LSA parts prices')"><span style="font-size:14px">🔍</span><span>PARTS</span></button>
      <button class="vc-btn" onclick="clawQuick('what is the weather forecast for Salem MO this week')"><span style="font-size:14px">🌦</span><span>FORECAST</span></button>
      <button class="vc-btn" onclick="clawQuick('check if I have any reminders today')"><span style="font-size:14px">🔔</span><span>REMINDERS</span></button>
      <button class="vc-btn" onclick="clawQuick('track my latest package')"><span style="font-size:14px">📦</span><span>TRACKING</span></button>
    </div>

    <!-- Input -->
    <div style="display:flex;gap:5px">
      <input id="claw-input" placeholder="Tell OpenClaw to do something..." style="flex:1;background:#0d0d0d;border:1px solid #1a1a1a;border-radius:6px;padding:8px;color:#fff;font-family:monospace;font-size:10px;outline:none;min-width:0"/>
      <button onclick="clawSend()" style="background:#001a00;border:1px solid #00cc44;color:#00cc44;font-family:monospace;font-size:9px;padding:8px 10px;border-radius:6px;cursor:pointer;white-space:nowrap;letter-spacing:1px">GO</button>
    </div>
  </div>

  <!-- MUSIC (Spotify) -->
  <div id="mode-music" class="mode-screen">
    <div id="t1-spotify-disconnected" style="text-align:center;padding:24px 8px">
      <div style="font-size:32px;margin-bottom:8px">🎵</div>
      <div style="font-size:12px;color:#fff;letter-spacing:2px;margin-bottom:4px">SPOTIFY</div>
      <div style="font-size:10px;color:#555;margin-bottom:12px;letter-spacing:1px">Connect to control playback</div>
      <button onclick="t1ConnectSpotify()" style="background:#1db954;border:none;border-radius:20px;padding:8px 20px;color:#fff;font-size:11px;font-weight:bold;cursor:pointer;letter-spacing:2px;font-family:monospace">CONNECT</button>
    </div>
    <div id="t1-spotify-connected" style="display:none;flex-direction:column;gap:4px">
      <div style="display:flex;align-items:center;gap:8px;background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:8px 10px">
        <img id="t1-np-art" src="" style="width:44px;height:44px;border-radius:4px;object-fit:cover;display:none;border:1px solid #1a1a1a">
        <div id="t1-np-disc" style="width:44px;height:44px;border-radius:50%;background:radial-gradient(circle,#330000,#0a0000);border:2px solid #cc0000;flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div id="t1-np-title" style="font-size:12px;color:#fff;font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Loading...</div>
          <div id="t1-np-artist" style="font-size:10px;color:#555;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px">—</div>
          <div style="background:#111;border-radius:2px;height:3px;overflow:hidden;margin-top:4px"><div id="t1-np-fill" style="height:100%;background:#cc0000;width:0%;transition:width 0.5s"></div></div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;flex-shrink:0">
          <div style="display:flex;gap:6px">
            <button onclick="t1SpotifyPrev()" style="background:#111;border:1px solid #222;color:#888;border-radius:3px;padding:4px 7px;cursor:pointer;font-size:12px">⏮</button>
            <button id="t1-play-btn" onclick="t1SpotifyToggle()" style="background:#cc0000;border:none;color:#fff;border-radius:3px;padding:4px 10px;cursor:pointer;font-size:13px">▶</button>
            <button onclick="t1SpotifyNext()" style="background:#111;border:1px solid #222;color:#888;border-radius:3px;padding:4px 7px;cursor:pointer;font-size:12px">⏭</button>
          </div>
          <input type="range" id="t1-vol" min="0" max="100" value="50" oninput="t1SpotifyVolume(this.value)" style="width:100%;accent-color:#cc0000">
        </div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:8px 10px">
        <div style="font-size:8px;color:#333;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #1a1a1a;padding-bottom:3px">PLAYLISTS</div>
        <div id="t1-playlist-list" style="font-size:10px;color:#555">Loading...</div>
      </div>
      <div style="background:#0a0a0a;border:1px solid #1a1a1a;border-radius:6px;padding:8px 10px">
        <div style="font-size:8px;color:#333;letter-spacing:3px;margin-bottom:6px;border-bottom:1px solid #1a1a1a;padding-bottom:3px">UP NEXT</div>
        <div id="t1-queue-list" style="font-size:10px;color:#555">Loading...</div>
      </div>
    </div>
  </div>

</div><!-- end content -->

<div id="archer-bar">
  <div id="archer-indicator"></div>
  <div id="archer-msg">Online. Everything looks good.</div>
</div>

<div id="footer">
  <div class="footer-item" id="f-tc">TC ON</div>
  <div class="footer-item" id="f-weather">70F CLEAR</div>
  <div class="footer-item" id="f-road">NO ROAD</div>
  <div class="footer-item" id="f-score">SCORE 100</div>
  <div class="footer-item" id="f-time">--:--</div>
</div>

</div><!-- end display -->

<script>
// ── SESSION ID + DEVICE FINGERPRINT ────────
const SID = Math.random().toString(36).slice(2);

// Generate a stable device fingerprint from browser properties
function getFingerprint() {
    const stored = localStorage.getItem('archer_fp');
    if (stored) return stored;
    const raw = [
        navigator.userAgent,
        navigator.language,
        screen.width + 'x' + screen.height,
        screen.colorDepth,
        new Date().getTimezoneOffset(),
        navigator.hardwareConcurrency || 0,
        navigator.deviceMemory || 0,
    ].join('|');
    // Simple hash
    let hash = 0;
    for (let i = 0; i < raw.length; i++) {
        hash = ((hash << 5) - hash) + raw.charCodeAt(i);
        hash |= 0;
    }
    const fp = Math.abs(hash).toString(36) + raw.length.toString(36);
    localStorage.setItem('archer_fp', fp);
    return fp;
}
const FP = getFingerprint();

// ── MODE SWITCHING ────────────────────────
let currentMode = 'default';
function setMode(mode) {
    currentMode = mode;
    document.querySelectorAll('.mode-screen').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    const screen = document.getElementById('mode-' + mode);
    if (screen) screen.classList.add('active');
    document.querySelectorAll('.tab').forEach(t => {
        const label = t.textContent.toLowerCase();
        if (label === mode || (mode === 'default' && label === 'dash') ||
            (mode === 'perf' && label === 'perf') ||
            (mode === 'graphs' && label === 'graphs') ||
            (mode === 'truck' && label === 'truck') ||
            (mode === 'health' && label === 'health') ||
            (mode === 'show' && label === 'show'))
            t.classList.add('active');
    });
}

// ── GAUGE DRAWING ─────────────────────────
function drawGauge(canvasId, value, max, color) {
    const c = document.getElementById(canvasId);
    if (!c) return;
    const ctx = c.getContext('2d');
    const w = c.width, h = c.height;
    const cx = w / 2, cy = h - 4;
    const r  = Math.min(w, h * 2) / 2 - 6;
    ctx.clearRect(0, 0, w, h);

    // Track
    ctx.beginPath();
    ctx.arc(cx, cy, r, Math.PI, 0, false);
    ctx.strokeStyle = '#1a1a1a';
    ctx.lineWidth   = 8;
    ctx.stroke();

    // Fill
    const pct   = Math.min(value / max, 1);
    const angle = Math.PI + pct * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, r, Math.PI, angle, false);
    ctx.strokeStyle = color;
    ctx.lineWidth   = 8;
    ctx.stroke();

    // Needle
    const nx = cx + (r - 2) * Math.cos(angle);
    const ny = cy + (r - 2) * Math.sin(angle);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(nx, ny);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth   = 2;
    ctx.stroke();
}

// ── GRAPH DRAWING ─────────────────────────
function drawGraph(canvasId, data, color, minVal, maxVal) {
    const c = document.getElementById(canvasId);
    if (!c || !data || data.length < 2) return;
    const ctx = c.getContext('2d');
    const w = c.offsetWidth || c.width;
    const h = c.height;
    c.width = w;
    ctx.clearRect(0, 0, w, h);

    const range = maxVal - minVal || 1;

    // Grid lines
    ctx.strokeStyle = '#1a1a1a';
    ctx.lineWidth   = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = (i / 4) * h;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }

    // Line
    ctx.beginPath();
    data.forEach((v, i) => {
        const x = (i / (data.length - 1)) * w;
        const y = h - ((v - minVal) / range) * h;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth   = 1.5;
    ctx.stroke();

    // Fill under line
    const last  = data.length - 1;
    const lastX = (last / last) * w;
    const lastY = h - ((data[last] - minVal) / range) * h;
    ctx.lineTo(lastX, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = color + '22';
    ctx.fill();

    // Spike dots — highlight peaks
    const peak = Math.max(...data);
    data.forEach((v, i) => {
        if (v === peak && v > minVal + (range * 0.7)) {
            const x = (i / (data.length - 1)) * w;
            const y = h - ((v - minVal) / range) * h;
            ctx.beginPath();
            ctx.arc(x, y, 3, 0, Math.PI * 2);
            ctx.fillStyle = '#fff';
            ctx.fill();
        }
    });
}

// ── VIRTUAL TRUCK UPDATE ──────────────────
let turnBlinkInterval = null;
let turnBlinkState    = false;

function setEl(id, prop, val) {
    const el = document.getElementById(id);
    if (el) el.style[prop] = val;
}

function updateTruck(d) {
    // Headlights
    const hl = d.headlights ? '1' : '0';
    setEl('hl-near',   'opacity', hl);
    setEl('hl-far',    'opacity', hl);
    setEl('tail-light','opacity', hl);
    setEl('highbeam',  'opacity', d.high_beams ? '1' : '0');

    // Underglow — scales with exhaust %
    const ugOp = d.exhaust > 0 ? Math.min(1, d.exhaust / 60).toFixed(2) : '0';
    setEl('underglow-fx', 'opacity', ugOp);

    // Ghost mode
    setEl('ghost-fx', 'opacity', d.ghost_mode ? '0.55' : '0');

    // Exhaust smoke
    setEl('smoke-wrap', 'opacity', d.boost > 5 || d.exhaust > 50 ? '1' : '0');

    // Turn signals — blink logic
    const hazards = d.hazards;
    const leftOn  = d.turn_left  || hazards;
    const rightOn = d.turn_right || hazards;

    if ((leftOn || rightOn) && !turnBlinkInterval) {
        turnBlinkInterval = setInterval(() => {
            turnBlinkState = !turnBlinkState;
            const nearOp = (leftOn  && turnBlinkState) ? '1' : '0';
            const farOp  = (rightOn && turnBlinkState) ? '1' : '0';
            setEl('turn-near', 'opacity', nearOp);
            setEl('turn-far',  'opacity', farOp);
        }, 500);
    } else if (!leftOn && !rightOn && turnBlinkInterval) {
        clearInterval(turnBlinkInterval);
        turnBlinkInterval = null;
        setEl('turn-near', 'opacity', '0');
        setEl('turn-far',  'opacity', '0');
    }

    // Stats
    const te = document.getElementById('t-exh');
    if (te) te.textContent = d.exhaust + '%';
    const tg = document.getElementById('t-ghost');
    if (tg) { tg.textContent = d.ghost_mode ? 'ON' : 'OFF'; tg.style.color = d.ghost_mode ? '#00cc44' : '#555'; }
    const tt = document.getElementById('t-tc');
    if (tt) { tt.textContent = d.tc_on ? 'ON' : 'OFF'; tt.style.color = d.tc_on ? '#00cc44' : '#cc0000'; }
    const tl = document.getElementById('t-lights');
    if (tl) { tl.textContent = d.headlights ? 'ON' : 'OFF'; tl.style.color = d.headlights ? '#ffffaa' : '#555'; }
}

// ── MAIN UPDATE ───────────────────────────
function updateDisplay(d) {
    // Header
    document.getElementById('profile-name').textContent = (d.profile || 'AYDEN').toUpperCase();
    const cb = document.getElementById('clients-badge');
    if (cb) cb.textContent = (d.connected_clients || 1) + ' ONLINE';

    // Warning
    const wb = document.getElementById('warning-banner');
    if (d.warning) {
        wb.classList.add('visible');
        document.getElementById('warning-text').textContent = (d.warning_msg || '').toUpperCase().replace(/_/g, ' ');
    } else { wb.classList.remove('visible'); }

    // Default dash
    const oilEl = document.getElementById('d-oil');
    if (oilEl) { oilEl.textContent = d.oil_temp + 'F'; oilEl.className = 'data-value ' + (d.oil_temp > 225 ? 'danger' : d.oil_temp > 215 ? 'warn' : 'good'); }
    const rpmEl = document.getElementById('d-rpm');
    if (rpmEl) { rpmEl.textContent = d.rpm; rpmEl.className = 'data-value ' + (d.rpm > 5500 ? 'danger' : d.rpm > 3500 ? 'warn' : ''); }
    const bEl = document.getElementById('d-boost');
    if (bEl) { bEl.textContent = d.boost + ' PSI'; bEl.className = 'data-value ' + (d.boost > 12 ? 'danger' : d.boost > 8 ? 'warn' : ''); }
    const eEl = document.getElementById('d-eth');
    if (eEl) { eEl.textContent = d.ethanol + '%'; eEl.className = 'data-value ' + (d.ethanol > 80 ? 'good' : d.ethanol > 50 ? 'warn' : 'danger'); }
    const batEl = document.getElementById('d-bat');
    if (batEl) { batEl.textContent = d.battery + 'V'; batEl.className = 'data-value ' + (d.battery > 13.0 ? 'good' : d.battery > 12.0 ? 'warn' : 'danger'); }
    const exEl = document.getElementById('d-exh');
    if (exEl) exEl.textContent = d.exhaust + '%';

    // NEW FULL DASHBOARD
    const ddRpm = document.getElementById('dd-rpm');
    if (ddRpm) { ddRpm.textContent = d.rpm; ddRpm.style.color = d.rpm > 5500 ? '#ff0000' : d.rpm > 4000 ? '#ffaa00' : '#ffffff'; }
    const ddSpeed = document.getElementById('dd-speed');
    if (ddSpeed) ddSpeed.textContent = d.speed || 0;
    const ddRpmBar = document.getElementById('dd-rpm-bar');
    if (ddRpmBar) ddRpmBar.style.width = Math.min(100, Math.round(d.rpm / 62)) + '%';
    const ddBoost = document.getElementById('dd-boost');
    if (ddBoost) { ddBoost.textContent = d.boost; ddBoost.style.color = d.boost > 15 ? '#ff0000' : d.boost > 8 ? '#ffaa00' : '#ff6600'; }
    const ddBoostBar = document.getElementById('dd-boost-bar');
    if (ddBoostBar) { const bp = Math.min(100, Math.max(0, Math.round(((d.boost + 10) / 40) * 100))); ddBoostBar.style.width = bp + '%'; ddBoostBar.style.background = d.boost > 15 ? '#ff0000' : '#ff6600'; }
    const ddGear = document.getElementById('dd-gear');
    if (ddGear) ddGear.textContent = d.gear || 1;
    const ddOil = document.getElementById('dd-oil');
    if (ddOil) { ddOil.textContent = d.oil_temp + '°'; ddOil.style.color = d.oil_temp > 225 ? '#ff0000' : d.oil_temp > 210 ? '#ffaa00' : '#ffaa00'; }
    const ddBat = document.getElementById('dd-bat');
    if (ddBat) { ddBat.textContent = d.battery + 'V'; ddBat.style.color = d.battery > 13.0 ? '#00cc44' : d.battery > 12.0 ? '#ffaa00' : '#cc0000'; }
    const ddEth = document.getElementById('dd-eth');
    if (ddEth) { ddEth.textContent = d.ethanol + '%'; ddEth.style.color = d.ethanol > 75 ? '#00cc44' : d.ethanol > 50 ? '#00aaff' : '#ffaa00'; }
    const ddMode = document.getElementById('dd-mode');
    if (ddMode) ddMode.textContent = (d.drive_mode || 'SPORT').toUpperCase();
    const ddMsg = document.getElementById('dd-msg');
    if (ddMsg && d.last_archer_msg) ddMsg.textContent = d.last_archer_msg;
    const ddScore = document.getElementById('dd-score');
    if (ddScore) { const s = d.drive_score || 100; ddScore.textContent = s >= 90 ? 'A' : s >= 80 ? 'B' : s >= 70 ? 'C' : s >= 60 ? 'D' : 'F'; ddScore.style.color = s >= 80 ? '#00cc44' : s >= 60 ? '#ffaa00' : '#cc0000'; }
    const ddCond = document.getElementById('dd-conditions');
    if (ddCond && d.weather) ddCond.textContent = 'Salem MO — ' + d.weather;

    // Gauges
    drawGauge('gauge-rpm',   d.rpm,   6200, d.rpm > 5500 ? '#cc0000' : d.rpm > 4000 ? '#ffaa00' : '#00cc44');
    drawGauge('gauge-boost', d.boost, 15,   d.boost > 12 ? '#cc0000' : '#ff6600');
    const gr = document.getElementById('gval-rpm');   if (gr) gr.textContent = d.rpm;
    const gb = document.getElementById('gval-boost'); if (gb) gb.textContent = d.boost + ' PSI';

    // Perf mode
    const pRpm = document.getElementById('p-rpm');
    if (pRpm) { pRpm.textContent = d.rpm; pRpm.style.color = d.rpm > 5500 ? '#cc0000' : d.rpm > 4000 ? '#ffaa00' : '#fff'; }
    const rpmPct = Math.min(100, Math.round(d.rpm / 62));
    const rpmBar = document.getElementById('p-rpm-bar'); if (rpmBar) rpmBar.style.width = rpmPct + '%';
    const rpmPctEl = document.getElementById('p-rpm-pct'); if (rpmPctEl) rpmPctEl.textContent = rpmPct + '%';
    const boostPct = Math.min(100, Math.round((d.boost / 15) * 100));
    const boostBar = document.getElementById('p-boost-bar'); if (boostBar) boostBar.style.width = boostPct + '%';
    const boostVal = document.getElementById('p-boost-val'); if (boostVal) boostVal.textContent = d.boost + ' PSI';
    const ethBar = document.getElementById('p-eth-bar'); if (ethBar) ethBar.style.width = d.ethanol + '%';
    const ethVal = document.getElementById('p-eth-val'); if (ethVal) ethVal.textContent = d.ethanol + '%';
    const pOil = document.getElementById('p-oil'); if (pOil) pOil.textContent = d.oil_temp + 'F';
    const pBest = document.getElementById('p-best'); if (pBest) pBest.textContent = d.best_060 > 0 ? d.best_060.toFixed(1) + 's' : '--';
    const pQ = document.getElementById('p-quality'); if (pQ) pQ.textContent = d.drive_quality;
    const qPct = d.drive_quality;
    const qBar = document.getElementById('p-quality-bar');
    if (qBar) { qBar.style.width = qPct + '%'; qBar.style.background = qPct > 80 ? '#00cc44' : qPct > 60 ? '#ffaa00' : '#cc0000'; }
    const qPctEl = document.getElementById('p-quality-pct'); if (qPctEl) qPctEl.textContent = qPct + '%';

    // Graphs
    if (d.spike_history) {
        const sh = d.spike_history;
        drawGraph('graph-rpm',   sh.rpm,     '#ffaa00', 500, 6500);
        drawGraph('graph-boost', sh.boost,   '#ff6600', 0,   16);
        drawGraph('graph-oil',   sh.oil,     '#cc0000', 180, 240);
        drawGraph('graph-bat',   sh.battery, '#00cc44', 11,  15);

        const rPeak = Math.max(...(sh.rpm || [0]));
        const bPeak = Math.max(...(sh.boost || [0]));
        const oPeak = Math.max(...(sh.oil || [0]));
        const batLast = sh.battery && sh.battery.length ? sh.battery[sh.battery.length - 1] : 0;

        const grp = document.getElementById('g-rpm-peak');   if (grp) grp.textContent = 'PEAK: ' + rPeak;
        const gbp = document.getElementById('g-boost-peak'); if (gbp) gbp.textContent = 'PEAK: ' + bPeak + ' PSI';
        const gop = document.getElementById('g-oil-peak');   if (gop) gop.textContent = 'PEAK: ' + oPeak + 'F';
        const gbl = document.getElementById('g-bat-last');   if (gbl) gbl.textContent = 'NOW: ' + batLast + 'V';
    }

    // Virtual truck
    updateTruck(d);
    updateControlPanel(d);
    updateLiveTab(d);
    updateCamsTab(d);
    updateRadar(d);
    updateStatusExtras(d);
    showClawTab(d.device_tier !== undefined ? d.device_tier : (d.tier || 4));
    updateClawStatus(d.openclaw_connected || false);

    // Health
    const items = [
        { name:'OIL TEMP',   value:d.oil_temp+'F',        ok:d.oil_temp<225,       warn:d.oil_temp>215 },
        { name:'COOLANT',    value:d.coolant+'F',          ok:d.coolant<220,        warn:false },
        { name:'BATTERY',    value:d.battery+'V',          ok:d.battery>12.5,       warn:d.battery>12.0 },
        { name:'BOOST',      value:d.boost+' PSI',         ok:true,                 warn:false },
        { name:'E85',        value:d.ethanol+'%',          ok:d.ethanol>50,         warn:d.ethanol>30 },
        { name:'DRIVE QUAL', value:d.drive_quality+'/100', ok:d.drive_quality>70,   warn:d.drive_quality>50 },
        { name:'TC',         value:d.tc_on?'ON':'OFF',     ok:d.tc_on,              warn:false },
        { name:'EXHAUST',    value:d.exhaust+'%',          ok:true,                 warn:false },
    ];
    const hl = document.getElementById('health-list');
    if (hl) hl.innerHTML = items.map(item => `
        <div class="health-item">
            <div class="health-name">${item.name}</div>
            <div class="health-val" style="color:${item.ok ? (item.warn ? '#ffaa00' : '#00cc44') : '#cc0000'}">${item.value}</div>
            <div class="health-dot ${item.ok ? 'dot-good' : 'dot-danger'}"></div>
        </div>`).join('');

    // Show mode
    const sr = document.getElementById('show-rpm'); if (sr) sr.textContent = d.rpm + ' RPM';
    const so = document.getElementById('show-oil'); if (so) so.textContent = d.oil_temp + 'F';
    const se = document.getElementById('show-eth'); if (se) se.textContent = 'E' + d.ethanol + '%';

    // Build tab
    const bSpent = document.getElementById('b-spent');
    if (bSpent) bSpent.textContent = '$' + (d.build_spent || 0).toLocaleString();
    const bParts = document.getElementById('b-parts');
    if (bParts) bParts.textContent = d.build_parts || 0;
    const bHp = document.getElementById('b-hp');
    if (bHp) { bHp.textContent = (d.est_hp || 556) + ' HP'; bHp.style.color = d.est_hp > 700 ? '#00cc44' : d.est_hp > 600 ? '#ffaa00' : '#cc0000'; }
    const bOdo = document.getElementById('b-odo');
    if (bOdo) bOdo.textContent = (d.odometer || 0).toLocaleString() + ' mi';
    const bFaults = document.getElementById('b-faults');
    if (bFaults) { bFaults.textContent = d.fault_count > 0 ? d.fault_count + ' ACTIVE CODE(S)' : 'No active codes'; bFaults.style.color = d.fault_count > 0 ? '#cc0000' : '#00cc44'; }
    const bCond = document.getElementById('b-conditions');
    if (bCond) bCond.textContent = (d.weather || '--') + ' — ' + (d.road || 'no road loaded');
    // Progress bar — year 1-5
    const bProg = document.getElementById('b-progress');
    const bProgL = document.getElementById('b-progress-label');
    const year = new Date().getFullYear();
    const pct  = Math.min(100, Math.max(2, ((year - 2026) / 5) * 100));
    if (bProg) bProg.style.width = pct + '%';
    if (bProgL) bProgL.textContent = 'Year ' + Math.max(1, year - 2025) + ' of 5';

    // Night mode
    if (d.night_mode) document.body.style.filter = 'brightness(0.5)';
    else document.body.style.filter = '';

    // Color theme
    if (d.color_theme && d.color_theme !== 'red') {
        const themes = {blue:'#0066cc', green:'#00cc44', white:'#ffffff', orange:'#ff6600', purple:'#8800ff'};
        const c = themes[d.color_theme] || '#cc0000';
        document.querySelectorAll('.danger, #archer-indicator').forEach(el => el.style.color = c);
    }

    // Archer message
    if (d.archer_msg) document.getElementById('archer-msg').textContent = d.archer_msg;

    // Footer
    const tcF = document.getElementById('f-tc');
    if (tcF) { tcF.textContent = d.tc_on ? 'TC ON' : 'TC OFF'; tcF.className = 'footer-item' + (d.tc_on ? '' : ' active'); }
    const fw = document.getElementById('f-weather'); if (fw) fw.textContent = d.weather || '--';
    const fr = document.getElementById('f-road'); if (fr) fr.textContent = (d.road || 'NO ROAD').substring(0, 18);
    // Drive score in footer
    const fscore = document.getElementById('f-score');
    if (fscore) {
        fscore.textContent = 'SCORE ' + (d.drive_score || 100) + (d.drive_grade ? ' ' + d.drive_grade : '');
        fscore.style.color = d.drive_score >= 90 ? '#00cc44' : d.drive_score >= 70 ? '#ffaa00' : '#cc0000';
    }
    // Weather alert flash
    if (d.weather_alert) {
        const wb = document.getElementById('warning-banner');
        if (wb) { wb.classList.add('visible'); document.getElementById('warning-text').textContent = 'WEATHER ALERT — CHECK CONDITIONS'; }
    }
    const ft = document.getElementById('f-time');
    if (ft) { const n = new Date(); ft.textContent = n.getHours().toString().padStart(2,'0') + ':' + n.getMinutes().toString().padStart(2,'0'); }

    // Auto switch show mode
    if (d.display_mode === 'show' && currentMode !== 'show') setMode('show');
    if (d.archer_msg && d.display_mode === 'show') { const sm = document.getElementById('show-msg'); if (sm) sm.textContent = d.archer_msg; }
}

// ── POLL DATA ─────────────────────────────
function poll() {
    fetch('/display_data?sid=' + SID + '&fp=' + FP)
        .then(r => r.json())
        .then(d => updateDisplay(d))
        .catch(() => {});
}
setInterval(poll, 500);
poll();

// ── AUDIO ─────────────────────────────────
let lastMsg = '', audioReady = false;
function checkAudio() {
    fetch('/display_data?sid=' + SID)
        .then(r => r.json())
        .then(d => {
            if (d.archer_msg && d.archer_msg !== lastMsg && audioReady) {
                lastMsg = d.archer_msg;
                const u = new SpeechSynthesisUtterance(d.archer_msg);
                u.rate = 0.95; u.pitch = 0.8; u.volume = 1.0;
                const voices   = window.speechSynthesis.getVoices();
                const priority = ['Google UK English Male','Google US English Male','Microsoft David - English (United States)','Daniel','Aaron'];
                let picked = null;
                for (const name of priority) { picked = voices.find(v => v.name === name); if (picked) break; }
                if (!picked) picked = voices.find(v => v.lang.startsWith('en') && (v.name.toLowerCase().includes('male') || v.name.toLowerCase().includes('david')));
                if (picked) u.voice = picked;
                window.speechSynthesis.speak(u);
                const btn = document.getElementById('audio-btn');
                if (btn) { btn.textContent = '&#128266; SPEAKING'; setTimeout(() => { btn.textContent = '&#128266; AUDIO ON'; btn.style.color = '#00cc44'; }, 2000); }
            }
        }).catch(() => {});
}
function connectAudio() {
    if (audioReady) return;
    const u = new SpeechSynthesisUtterance(''); u.volume = 0;
    window.speechSynthesis.speak(u);
    audioReady = true;
    lastMsg = document.getElementById('archer-msg').textContent;
    const btn = document.getElementById('audio-btn');
    if (btn) { btn.textContent = '&#128266; AUDIO ON'; btn.style.color = '#00cc44'; }
}
window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
setInterval(checkAudio, 500);
document.addEventListener('click', function initAudio() { connectAudio(); document.removeEventListener('click', initAudio); }, { once: true });

// ── MICROPHONE ────────────────────────────
let micRecognition = null, micActive = false;
function startMic() {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
        document.getElementById('mic-btn').textContent = 'NOT SUPPORTED'; return;
    }
    if (micActive) { if (micRecognition) micRecognition.stop(); return; }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    micRecognition = new SR();
    micRecognition.lang = 'en-US'; micRecognition.continuous = false; micRecognition.interimResults = false;
    micRecognition.onstart = () => { micActive = true; document.getElementById('mic-btn').textContent = '&#128308; LISTENING'; document.getElementById('mic-btn').style.color = '#cc0000'; };
    micRecognition.onresult = (event) => {
        const cmd = event.results[0][0].transcript;
        document.getElementById('archer-msg').textContent = 'You: ' + cmd;
        document.getElementById('mic-btn').textContent = '&#8987; THINKING';
        document.getElementById('mic-btn').style.color = '#ffaa00';
        fetch('/voice_command', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command:cmd}) })
            .then(r => r.json())
            .then(d => { if (d.response) document.getElementById('archer-msg').textContent = d.response; })
            .catch(() => {})
            .finally(() => { document.getElementById('mic-btn').textContent = '&#127908; MIC'; document.getElementById('mic-btn').style.color = '#555'; micActive = false; });
    };
    micRecognition.onerror = micRecognition.onend = () => { document.getElementById('mic-btn').textContent = '&#127908; MIC'; document.getElementById('mic-btn').style.color = '#555'; micActive = false; };
    micRecognition.start();
}

// ── SEND COMMAND FROM DISPLAY BUTTONS ───
function sendCmd(cmd) {
    fetch('/voice_command', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({command: cmd})
    })
    .then(r => r.json())
    .then(d => {
        if (d.response) {
            document.getElementById('archer-msg').textContent = d.response;
            if (audioReady) {
                const u = new SpeechSynthesisUtterance(d.response);
                u.rate = 0.95; u.pitch = 0.8;
                window.speechSynthesis.speak(u);
            }
        }
    })
    .catch(() => {});
}

// ── UPDATE VEHICLE TAB STATS ─────────────
function updateVehicleTab(d) {
    const voil = document.getElementById('vt-oil');
    if (voil) voil.textContent = d.oil_temp + 'F';
    const voilb = document.getElementById('vt-oil-bar');
    if (voilb) { const p = Math.min(100, Math.max(0, ((d.oil_temp - 150) / 90) * 100)); voilb.style.width = p + '%'; voilb.style.background = d.oil_temp > 225 ? '#cc0000' : d.oil_temp > 210 ? '#ffaa00' : '#00cc44'; }
    const vrpm = document.getElementById('vt-rpm');
    if (vrpm) vrpm.textContent = d.rpm;
    const vrpmb = document.getElementById('vt-rpm-bar');
    if (vrpmb) { vrpmb.style.width = Math.min(100, Math.round(d.rpm / 62)) + '%'; }
    const vb = document.getElementById('vt-boost');
    if (vb) { vb.textContent = d.boost; vb.style.color = d.boost > 12 ? '#cc0000' : d.boost > 6 ? '#ffaa00' : '#ff6600'; }
    const ve = document.getElementById('vt-eth');
    if (ve) { ve.textContent = d.ethanol + '%'; ve.style.color = d.ethanol > 70 ? '#00cc44' : d.ethanol > 40 ? '#ffaa00' : '#cc0000'; }
    const vs = document.getElementById('vt-status');
    if (vs) {
        if (d.ghost_mode) { vs.textContent = 'GHOST'; vs.style.color = '#8844ff'; }
        else if (d.boost > 5) { vs.textContent = 'BOOSTING'; vs.style.color = '#ff6600'; }
        else if (d.rpm > 4000) { vs.textContent = 'PUSHING'; vs.style.color = '#ffaa00'; }
        else if (d.headlights) { vs.textContent = 'LIGHTS ON'; vs.style.color = '#ffffaa'; }
        else { vs.textContent = 'READY'; vs.style.color = '#00cc44'; }
    }
}

// ── VEHICLE CONTROL PANEL ───────────────
function vcCmd(cmd) {
    const btn = event && event.currentTarget;
    if (btn) { btn.style.opacity = '0.5'; setTimeout(() => btn.style.opacity = '1', 300); }
    fetch('/voice_command', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({command: cmd})
    })
    .then(r => r.json())
    .then(d => {
        if (d.response) {
            document.getElementById('archer-msg').textContent = d.response;
            if (audioReady) {
                const u = new SpeechSynthesisUtterance(d.response);
                u.rate = 0.95; u.pitch = 0.8;
                window.speechSynthesis.speak(u);
            }
        }
    })
    .catch(() => {});
}

function showExhaustSlider() {
    const w = document.getElementById('exhaust-slider-wrap');
    if (w) w.style.display = w.style.display === 'none' ? 'block' : 'none';
}

function updateControlPanel(d) {
    // Show/hide based on DEVICE fingerprint tier
    const locked   = document.getElementById('truck-locked');
    const controls = document.getElementById('truck-controls');
    const deviceTier = d.device_tier !== undefined ? d.device_tier : 4;
    if (locked && controls) {
        if (deviceTier <= 1) {
            locked.style.display   = 'none';
            controls.style.display = 'flex';
        } else {
            // Show locked screen with register option if not registered
            locked.style.display   = 'flex';
            controls.style.display = 'none';
            if (!d.device_registered) {
                let regBtn = document.getElementById('register-btn');
                if (!regBtn) {
                    regBtn = document.createElement('button');
                    regBtn.id = 'register-btn';
                    regBtn.textContent = 'REGISTER THIS DEVICE';
                    regBtn.style.cssText = 'margin-top:16px;background:#1a0000;border:1px solid #cc0000;color:#cc0000;font-family:monospace;font-size:9px;letter-spacing:2px;padding:8px 16px;border-radius:6px;cursor:pointer';
                    regBtn.onclick = function() {
                        const name = prompt('Device name (e.g. Ayden Phone):');
                        if (!name) return;
                        fetch('/register_device', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({fingerprint: FP, name: name, tier: 1})
                        }).then(r => r.json()).then(d => {
                            if (d.ok) alert('Device registered as ' + d.name + ' — Tier ' + d.tier);
                        });
                    };
                    locked.appendChild(regBtn);
                }
            }
        }
    }

    // Live stats
    const vcRpm = document.getElementById('vc-rpm');   if (vcRpm) vcRpm.textContent = d.rpm;
    const vcB   = document.getElementById('vc-boost'); if (vcB)   { vcB.textContent = d.boost; vcB.style.color = d.boost > 12 ? '#cc0000' : '#ff6600'; }
    const vcO   = document.getElementById('vc-oil');   if (vcO)   { vcO.textContent = d.oil_temp + 'F'; vcO.style.color = d.oil_temp > 225 ? '#cc0000' : d.oil_temp > 210 ? '#ffaa00' : '#fff'; }
    const vcE   = document.getElementById('vc-eth');   if (vcE)   { vcE.textContent = d.ethanol + '%'; vcE.style.color = d.ethanol > 70 ? '#00cc44' : d.ethanol > 40 ? '#ffaa00' : '#cc0000'; }
    const vcD   = document.getElementById('vc-driver'); if (vcD)  vcD.textContent = (d.profile || 'AYDEN').toUpperCase();

    // Button active states
    const setActive = (id, on) => { const el = document.getElementById(id); if (el) { el.classList.toggle('active', on); } };
    setActive('btn-headlights', d.headlights);
    setActive('btn-highbeams',  d.high_beams);
    setActive('btn-foglights',  d.fog_lights);
    setActive('btn-hazards',    d.hazards);
    setActive('btn-tc',         !d.tc_on);
    setActive('btn-ghost',      d.ghost_mode);
    setActive('btn-ac',         d.ac_on);
    setActive('btn-heat',       d.heat_on);
    setActive('btn-windows',    d.windows && d.windows.fl === 'down');

    // Sync exhaust slider
    const er = document.getElementById('exhaust-range');
    const ev = document.getElementById('exhaust-val');
    if (er && !er.matches(':active')) { er.value = d.exhaust; }
    if (ev) ev.textContent = d.exhaust + '%';
}

// ── LIVE DATA TAB UPDATE ─────────────────
function updateLiveTab(d) {
    if (!d.sensor_data) return;
    const s = d.sensor_data;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    const col = (id, c)   => { const el = document.getElementById(id); if (el) el.style.color = c; };

    set('l-afr',    s.afr);
    col('l-afr',    s.afr < 11.5 ? '#cc0000' : s.afr < 13.0 ? '#00cc44' : '#ffaa00');
    set('l-timing', s.timing_deg + '°');
    col('l-timing', s.timing_deg < 12 ? '#cc0000' : '#fff');
    set('l-knock',  s.knock_count);
    col('l-knock',  s.knock_count > 0 ? '#cc0000' : '#00cc44');
    set('l-tps',    s.throttle_pct + '%');
    set('l-oilpsi', s.oil_pressure);
    col('l-oilpsi', s.oil_pressure < 25 ? '#cc0000' : s.oil_pressure < 35 ? '#ffaa00' : '#fff');
    set('l-intake', s.intake_temp + 'F');
    set('l-trans',  s.trans_temp + 'F');
    col('l-trans',  s.trans_temp > 200 ? '#cc0000' : s.trans_temp > 185 ? '#ffaa00' : '#fff');
    set('l-alt',    s.alternator_v + 'V');
    col('l-alt',    s.alternator_v < 13.0 ? '#cc0000' : '#00cc44');
    set('l-gear',   s.gear_pos);
    set('l-glong',  (d.gforce_now || 0).toFixed(2) + 'g');
    set('l-gpeak',  (d.gforce_peak || 0).toFixed(2) + 'g');
    set('l-speed',  (s.speed_mph || 0) + ' MPH');

    // Drag timer
    set('l-drag-stage', (d.drag_stage || 'idle').toUpperCase());
    if (d.drag_best_et) set('l-drag-best', 'BEST: ' + d.drag_best_et + 's @ ' + d.drag_best_mph + 'MPH');
    const splits = d.sensor_data && d.drag_stage === 'running' ? {} : {};
}

// ── CAMERAS TAB UPDATE ───────────────────
function updateCamsTab(d) {
    const status = document.getElementById('cam-status');
    if (status) { status.textContent = d.surveillance ? 'ARMED' : 'DISARMED'; status.style.color = d.surveillance ? '#cc0000' : '#555'; }
    const valet = document.getElementById('cam-valet');
    if (valet) { valet.textContent = d.valet_events || 0; valet.style.color = d.valet_events > 0 ? '#ffaa00' : '#fff'; }
    const park = document.getElementById('cam-parking');
    if (park) { park.textContent = d.parking_active ? (d.parking_loc || 'ACTIVE').toUpperCase().substring(0,10) : 'OFF'; park.style.color = d.parking_active ? '#00cc44' : '#555'; }

    // Update camera feed iframes if URLs set
    if (d.cameras) {
        ['front','rear','cab','left','right'].forEach(cam => {
            const info   = d.cameras[cam];
            const wrap   = document.getElementById('cam-' + cam + '-wrap');
            const feed   = document.getElementById('cam-' + cam + '-feed');
            const status = document.getElementById('cam-' + cam + '-status');
            if (!info) return;
            if (info.url && feed) {
                if (feed.src !== info.url) feed.src = info.url;
                feed.style.display   = 'block';
                if (status) status.style.display = 'none';
            } else {
                if (feed) feed.style.display = 'none';
                if (status) { status.style.display = 'block'; status.textContent = info.status.toUpperCase(); }
            }
        });
    }
}

function parkHere() {
    const loc = document.getElementById('park-loc');
    const val = loc ? loc.value.trim() : '';
    sendCmd('parked at ' + val);
    if (loc) loc.value = '';
}

// ── RADAR ALERT DISPLAY ─────────────────
const RADAR_COLORS = {
    clear:    null,
    weak:     '#ffaa00',
    moderate: '#ff6600',
    strong:   '#cc0000',
    laser:    '#ff00ff',
};
const RADAR_BG = {
    clear:    null,
    weak:     '#1a0e00',
    moderate: '#1a0800',
    strong:   '#1a0000',
    laser:    '#1a001a',
};
let radarFlash = false;
let radarFlashInterval = null;

function updateRadar(d) {
    const banner = document.getElementById('radar-banner');
    if (!banner) return;
    const level = d.radar_alert || 'clear';
    if (level === 'clear') {
        banner.style.display = 'none';
        if (radarFlashInterval) { clearInterval(radarFlashInterval); radarFlashInterval = null; }
        return;
    }
    banner.style.display = 'block';
    banner.style.background = RADAR_BG[level] || '#1a0000';
    banner.style.color       = RADAR_COLORS[level] || '#ff6600';
    banner.style.border      = '1px solid ' + (RADAR_COLORS[level] || '#ff6600');

    const band   = document.getElementById('radar-band');
    const bars   = document.getElementById('radar-bars');
    const dir    = document.getElementById('radar-dir');
    if (band) band.textContent = (d.radar_band || 'Ka') + ' BAND';
    if (dir)  dir.textContent  = (d.radar_direction || 'FRONT').toUpperCase();
    const str = d.radar_strength || 0;
    if (bars) bars.textContent = '●'.repeat(str) + '○'.repeat(Math.max(0,5-str));

    // Flash on strong/laser
    if ((level === 'strong' || level === 'laser') && !radarFlashInterval) {
        radarFlashInterval = setInterval(() => {
            radarFlash = !radarFlash;
            banner.style.opacity = radarFlash ? '1' : '0.3';
        }, 200);
    }
}

// ── RECORDS / RIVALRY IN HEADER ──────────
function updateStatusExtras(d) {
    // Passenger mode indicator
    const prof = document.getElementById('profile-name');
    if (prof) {
        let label = (d.profile || 'AYDEN').toUpperCase();
        if (d.passenger_mode) label += ' + PASS';
        if (d.trailer_connected) label += ' + TOW';
        prof.textContent = label;
    }

    // Fuel range in footer
    const fw = document.getElementById('f-weather');
    if (fw) fw.textContent = d.weather + ' | ' + (d.fuel_range || '--') + 'mi range';

    // Rival in show mode
    if (d.rival) {
        const sr = document.getElementById('show-rpm');
        if (sr && d.record_best_et) sr.textContent = 'BEST: ' + d.record_best_et + 's';
    }
}

// ── OPENCLAW DISPLAY ─────────────────────
function showClawTab(tier) {
    const tab = document.getElementById('claw-tab');
    if (tab) tab.style.display = (tier <= 2) ? 'block' : 'none';
}

function updateClawStatus(connected) {
    const dot  = document.getElementById('claw-status-dot');
    const text = document.getElementById('claw-status-text');
    if (dot)  dot.style.background = connected ? '#00cc44' : '#333';
    if (text) { text.textContent = connected ? 'ONLINE' : 'OFFLINE'; text.style.color = connected ? '#00cc44' : '#333'; }
}

function clawAppend(msg, who) {
    const hist = document.getElementById('claw-history');
    if (!hist) return;
    const div = document.createElement('div');
    div.style.cssText = 'margin-bottom:5px;padding:4px 0;border-bottom:1px solid #0d0d0d';
    const label = who === 'you' ? '<span style="color:#cc0000;font-size:8px">YOU</span>' : '<span style="color:#00cc44;font-size:8px">CLAW</span>';
    div.innerHTML = label + '<br><span style="color:#aaa">' + msg + '</span>';
    hist.appendChild(div);
    hist.scrollTop = hist.scrollHeight;
}

function clawSend() {
    const inp  = document.getElementById('claw-input');
    const task = inp ? inp.value.trim() : '';
    if (!task) return;
    clawAppend(task, 'you');
    if (inp) inp.value = '';
    clawAppend('Working...', 'claw');
    fetch('/voice_command', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:task})})
    .then(r=>r.json())
    .then(d=>{
        const hist = document.getElementById('claw-history');
        if (hist && hist.lastChild) hist.removeChild(hist.lastChild);
        clawAppend(d.response || 'Done.', 'claw');
        if (audioReady && d.response) {
            const u = new SpeechSynthesisUtterance(d.response);
            u.rate = 0.95; u.pitch = 0.8;
            window.speechSynthesis.speak(u);
        }
    })
    .catch(()=>clawAppend('Error.','claw'));
}

function clawQuick(task) {
    const inp = document.getElementById('claw-input');
    if (inp) { inp.value = task; clawSend(); }
}

document.addEventListener('keydown', e=>{
    if (document.activeElement && document.activeElement.id === 'claw-input' && e.key === 'Enter') clawSend();
});

// ── PWA ───────────────────────────────────
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
</script>
</body>
</html>'''

# ── FLASK ROUTES ─────────────────────────

@display_app.route('/nav/save_place', methods=['POST'])
def nav_save_place():
    from flask import request as _req
    data    = _req.get_json()
    name    = data.get('name', '').strip().lower()
    lat     = data.get('lat')
    lon     = data.get('lon')
    address = data.get('address', '')
    if not name or lat is None or lon is None:
        return jsonify({'error': 'Need name, lat, lon'}), 400
    nav_places[name] = {'lat': float(lat), 'lon': float(lon), 'address': address}
    save_state()
    print(f'[NAV] Saved place "{name}" → {lat},{lon}')
    return jsonify({'ok': True, 'name': name})

@display_app.route('/nav/places')
def nav_list_places():
    return jsonify({k: v for k, v in nav_places.items()})

@display_app.route('/navigate')
def navigate_endpoint():
    from flask import request as _req
    lat       = _req.args.get('lat', '').strip()
    lon       = _req.args.get('lon', '').strip()
    dest_text = _req.args.get('dest', '').strip()
    dest_lat  = _req.args.get('dest_lat', '').strip()
    dest_lon  = _req.args.get('dest_lon', '').strip()
    dest_name = _req.args.get('dest_name', '').strip()

    def geocode(query, user_lat=None, user_lon=None):
        import math
        def dist(la1, lo1, la2, lo2):
            dL = math.radians(float(la2)-float(la1)); dl = math.radians(float(lo2)-float(lo1))
            a  = math.sin(dL/2)**2 + math.cos(math.radians(float(la1)))*math.cos(math.radians(float(la2)))*math.sin(dl/2)**2
            return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        def do_search(url):
            req = urllib.request.Request(url, headers={'User-Agent': 'Archer-Truck-AI/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                places = json.loads(r.read())
            if not places: return None
            if user_lat and user_lon:
                places.sort(key=lambda p: dist(user_lat, user_lon, p['lat'], p['lon']))
            p = places[0]
            d_mi = dist(user_lat, user_lon, p['lat'], p['lon']) if user_lat else 0
            name = p.get('display_name', query).split(',')[0]
            print(f'[NAV] Found "{name}" — {d_mi:.1f} mi away')
            return {'lat': p['lat'], 'lon': p['lon'], 'name': name}

        enc = urllib.parse.quote(query)
        GEOCODE_KEY = os.environ.get('GEOCODE_API_KEY', '')

        # Search with progressively larger radius: ~7mi → ~21mi → ~55mi
        for radius in [0.1, 0.3, 0.8]:
            vbox = ''
            if user_lat and user_lon:
                vbox = f'&viewbox={float(user_lon)-radius},{float(user_lat)+radius},{float(user_lon)+radius},{float(user_lat)-radius}&bounded=1'
            try:
                if GEOCODE_KEY:
                    result = do_search(f'https://geocode.maps.co/search?q={enc}&api_key={GEOCODE_KEY}&limit=5{vbox}')
                else:
                    result = do_search(f'https://nominatim.openstreetmap.org/search?q={enc}&format=json&limit=5&countrycodes=us{vbox}')
                if result:
                    return result
            except Exception as e:
                print(f'[NAV] Geocode radius={radius} failed: {e}')

        return None

    # Geocode if no coords provided
    if not dest_lat or not dest_lon:
        if not dest_text:
            return jsonify({'error': 'No destination provided'}), 400
        # Check saved places first
        key = dest_text.lower().strip()
        if key in nav_places:
            p = nav_places[key]
            dest_lat, dest_lon = str(p['lat']), str(p['lon'])
            dest_name = p.get('address') or key.title()
            print(f'[NAV] Using saved place "{key}"')
        else:
            place = geocode(dest_text, user_lat=lat or None, user_lon=lon or None)
            # If full address fails, retry with just the street name (strip leading number)
            if not place:
                import re as _re
                street_only = _re.sub(r'^\d+\s+', '', dest_text).strip()
                if street_only != dest_text:
                    print(f'[NAV] Retrying without house number: "{street_only}"')
                    place = geocode(street_only, user_lat=lat or None, user_lon=lon or None)
            if not place:
                return jsonify({'error': f"Can't find \"{dest_text}\". Say \"save this as [name]\" at your destination to save it."}), 404
            dest_lat, dest_lon = place['lat'], place['lon']
            dest_name = place['name']

    print(f'[NAV] Routing to {dest_name} ({dest_lat},{dest_lon}) from ({lat},{lon})')

    # Route via OSRM (free, no key) if GPS provided
    steps = []
    total_dist_m = 0
    total_dur_s  = 0
    if lat and lon:
        try:
            osrm = (f'https://router.project-osrm.org/route/v1/driving/'
                    f'{lon},{lat};{dest_lon},{dest_lat}?overview=false&steps=true')
            osrm_req = urllib.request.Request(osrm, headers={'User-Agent': 'Archer-Truck-AI/1.0'})
            with urllib.request.urlopen(osrm_req, timeout=12) as r:
                rd = json.loads(r.read())
            if rd.get('code') == 'Ok':
                route = rd['routes'][0]
                total_dist_m = route.get('distance', 0)
                total_dur_s  = route.get('duration', 0)
                for leg in route.get('legs', []):
                    for step in leg.get('steps', []):
                        mv = step.get('maneuver', {})
                        loc = mv.get('location', [0, 0])
                        steps.append({
                            'maneuver':    mv.get('type', 'continue'),
                            'modifier':    mv.get('modifier', ''),
                            'instruction': step.get('name', ''),
                            'distance':    round(step.get('distance', 0)),
                            'duration':    round(step.get('duration', 0)),
                            'lat':         loc[1],
                            'lon':         loc[0],
                        })
        except Exception as e:
            print(f'[NAV] OSRM failed: {e}')
    print(f'[NAV] Route to {dest_name}: {round(total_dist_m*0.000621371,1)} mi, {round(total_dur_s/60)} min, {len(steps)} steps')
    return jsonify({
        'ok':                True,
        'destination':       dest_name,
        'dest_lat':          dest_lat,
        'dest_lon':          dest_lon,
        'steps':             steps,
        'total_distance_mi': round(total_dist_m * 0.000621371, 1),
        'total_duration_min':round(total_dur_s / 60),
    })

DANGEROUS_COMMANDS = ['engine off', 'shut down', 'tc off', 'tc lock', 'sys.exit',
                      'shutdown', 'kill engine', 'reboot', 'delete profile']

@display_app.route('/voice_command', methods=['POST'])
def voice_command_endpoint():
    from flask import request as flask_request
    try:
        tier = get_request_tier(flask_request)
        data     = flask_request.get_json()
        command  = data.get('command', '').strip()
        log_only = data.get('log_only', False)
        if not command:
            return jsonify({'response': ''})
        if log_only:
            if not command.startswith('[NAV ERROR]'):
                print(f"[YOU — DISPLAY MIC] {command}")
            else:
                print(command)
            return jsonify({'response': ''})
        # Block dangerous commands from unauthenticated / low-tier callers
        if tier > 1 and any(d in command.lower() for d in DANGEROUS_COMMANDS):
            return jsonify({'response': 'Not authorized.'}), 403
        if tier >= 4:
            return jsonify({'response': 'Read only in valet mode.'}), 403
        response = handle_command(command)
        if response is None:
            response = ask_archer(command)
        if response:
            print(f"[YOU — DISPLAY MIC] {command}")
            print(f"[ARCHER] {response}")
            speak(response)
            last_archer_msg['text'] = response
        return jsonify({'response': response or ''})
    except Exception as e:
        return jsonify({'response': 'Give me a second.'})


@display_app.route('/drag/stage', methods=['POST'])
def drag_stage_route():
    start_drag_run()
    return jsonify({'ok': True, 'stage': drag_timer['stage']})

@display_app.route('/drag/launch', methods=['POST'])
def drag_launch_route():
    msg = launch_drag()
    return jsonify({'ok': msg is None, 'stage': drag_timer['stage'], 'msg': msg or 'Launched.'})


@display_app.route('/build/update', methods=['POST'])
def build_update_route():
    data = request.get_json() or {}
    bool_keys = {'cold_air_intake','long_tube_headers','full_exhaust','intake_manifold',
                 'throttle_body_upgrade','cam_swap','heads_upgrade','wideband_o2',
                 'electric_fan','underdrive_pulley','custom_tune'}
    int_keys  = {'cam_level','heads_level'}
    for k, v in data.items():
        if k in bool_keys:
            build_specs[k] = bool(v)
        elif k in int_keys:
            build_specs[k] = int(v)
        elif k in build_specs:
            build_specs[k] = v
    save_state()
    return jsonify({'ok': True, 'build_specs': dict(build_specs), 'power': estimate_power_from_parts()})

def _resolve_location_from_nws(lat, lon):
    print(f'[GPS] resolving location for {lat:.4f},{lon:.4f}')
    try:
        hdr = {'User-Agent': 'Archer/1.0 archer@ayden.dev'}
        pts_url = f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}'
        with urllib.request.urlopen(urllib.request.Request(pts_url, headers=hdr), timeout=10) as r:
            pts = json.loads(r.read())
        rel   = pts.get('properties', {}).get('relativeLocation', {}).get('properties', {})
        city  = rel.get('city', '')
        state = rel.get('state', '')
        print(f'[GPS] NWS returned city={city!r} state={state!r}')
        if city and state:
            resolved = f'{city}, {state}'
            location_data['location_name'] = resolved
            _save_location_cache(lat, lon, resolved)
            speak(f'Location locked. {resolved}.')
            print(f'[GPS] location set to {resolved}')
        else:
            fallback = f'{lat:.3f}°, {lon:.3f}°'
            location_data['location_name'] = fallback
            _save_location_cache(lat, lon, fallback)
    except Exception as e:
        print(f'[GPS] NWS location resolve failed: {e}')
        fallback = f'{lat:.3f}°, {lon:.3f}°'
        location_data['location_name'] = fallback
        _save_location_cache(lat, lon, fallback)

@display_app.route('/location/update', methods=['POST'])
def location_update_route():
    global _nws_station_url, _nws_forecast_url
    data = request.get_json() or {}
    lat  = data.get('lat')
    lon  = data.get('lon')
    name = data.get('name', '')
    if lat is not None and lon is not None:
        old_lat = location_data.get('lat')
        old_lon = location_data.get('lon')
        location_data['lat'] = float(lat)
        location_data['lon'] = float(lon)
        print(f'[GPS] received lat={lat}, lon={lon}  old=({old_lat},{old_lon})')
        if name:
            location_data['location_name'] = name
            print(f'[GPS] name from browser: {name}')
        if old_lat is None or old_lon is None or (abs(float(lat) - old_lat) + abs(float(lon) - old_lon) > 0.07):
            if not name:
                location_data['location_name'] = ''
            _nws_station_url = None
            _nws_forecast_url = None
            weather['last_update'] = 0
            threading.Thread(target=_resolve_location_from_nws, args=(float(lat), float(lon)), daemon=True).start()
    return jsonify({'ok': True, 'lat': location_data.get('lat'), 'lon': location_data.get('lon'),
                    'name': location_data.get('location_name', '')})

@display_app.route('/build/part/search')
def build_part_search():
    name    = request.args.get('name', '')
    pn      = request.args.get('pn', '')
    results = web_search_parts(name, pn)
    return jsonify({'results': results, 'query_name': name, 'query_pn': pn})

@display_app.route('/build/part/add', methods=['POST'])
def build_part_add():
    data = request.get_json() or {}
    part = {
        'id':          str(uuid.uuid4())[:8],
        'name':        data.get('name', 'Unknown Part'),
        'part_number': data.get('part_number', ''),
        'category':    data.get('category', 'other'),
        'hp_gain':     float(data.get('hp_gain', 0)),
        'tq_gain':     float(data.get('tq_gain', 0)),
        'description': data.get('description', ''),
        'status':      data.get('status', 'ordered'),
        'cost':        float(data.get('cost', 0)),
        'date_added':  datetime.now().strftime('%B %d %Y'),
        'notes':       data.get('notes', ''),
    }
    build_tracker['parts'].append(part)
    _recalc_build_spent()
    save_state()
    return jsonify({'ok': True, 'part': part, 'power': estimate_power_from_parts(),
                    'parts': list(build_tracker['parts'])})

@display_app.route('/build/part/update', methods=['POST'])
def build_part_update():
    data   = request.get_json() or {}
    pid    = data.get('id')
    part   = next((p for p in build_tracker['parts'] if p.get('id') == pid), None)
    if not part:
        return jsonify({'ok': False, 'error': 'Part not found'})
    for k in ('status', 'hp_gain', 'tq_gain', 'cost', 'notes', 'name', 'part_number'):
        if k in data:
            part[k] = float(data[k]) if k in ('hp_gain','tq_gain','cost') else data[k]
    _recalc_build_spent()
    save_state()
    return jsonify({'ok': True, 'part': part, 'power': estimate_power_from_parts(),
                    'parts': list(build_tracker['parts'])})

@display_app.route('/build/part/remove', methods=['POST'])
def build_part_remove():
    pid = (request.get_json() or {}).get('id')
    build_tracker['parts'] = [p for p in build_tracker['parts'] if p.get('id') != pid]
    _recalc_build_spent()
    save_state()
    return jsonify({'ok': True, 'power': estimate_power_from_parts(),
                    'parts': list(build_tracker['parts'])})

@display_app.route('/register_device', methods=['POST'])
def register_device_endpoint():
    from flask import request as flask_request
    data        = flask_request.get_json()
    fingerprint = data.get('fingerprint', '')
    name        = data.get('name', 'Unknown')
    tier        = max(2, min(4, int(data.get('tier', 2))))  # self-register max Tier 2
    if not fingerprint:
        return jsonify({'ok': False, 'error': 'No fingerprint'})
    register_device(fingerprint, name, tier)
    return jsonify({'ok': True, 'name': name, 'tier': tier})

@display_app.route('/device_tier', methods=['POST'])
def device_tier_endpoint():
    from flask import request as flask_request
    data        = flask_request.get_json()
    fingerprint = data.get('fingerprint', '')
    tier        = get_device_tier(fingerprint)
    registered  = fingerprint in trusted_devices
    name        = trusted_devices.get(fingerprint, {}).get('name', 'Unknown')
    return jsonify({'tier': tier, 'registered': registered, 'name': name})


# ── TIER ROUTES ON MAIN APP (for ngrok remote access) ───
@display_app.route('/display')
@display_app.route('/ayden')
@display_app.route('/tier1')
def tier1_page():
    from flask import Response as FR
    resp = FR(get_tier_html(1), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@display_app.route('/pass')
@display_app.route('/passenger')
@display_app.route('/tier2')
def tier2_page():
    from flask import Response as FR
    return FR(get_tier_html(2), mimetype='text/html')

@display_app.route('/family')
@display_app.route('/tier3')
def tier3_page():
    from flask import Response as FR
    return FR(get_tier_html(3), mimetype='text/html')

@display_app.route('/valet')
@display_app.route('/tier4')
def tier4_page():
    from flask import Response as FR
    return FR(get_tier_html(4), mimetype='text/html')

# ── TERMINAL ACCESS CONTROL ─────────────────────────────
TERMINAL_ALLOWED_TIERS = [1]  # only Tier 1 by default — add 2,3,4 to unlock

def get_request_tier(request):
    """Resolve the tier for any request using MAC auth cookie, fingerprint, or both."""
    import hashlib as _hl
    # 1. MAC auth cookie (primary system)
    cookie_val = request.cookies.get('archer_auth', '')
    if cookie_val:
        try:
            parts = cookie_val.split(':')
            if len(parts) == 3:
                c_tier, c_name, c_token = parts
                cookie_secret = os.environ.get('ARCHER_SECRET', 'archer2500hd')
                expected = _hl.sha256(f'{c_name}{c_tier}{cookie_secret}'.encode()).hexdigest()[:16]
                if c_token == expected:
                    return int(c_tier)
        except Exception:
            pass
    # 2. Fingerprint system (legacy / in-cabin devices)
    fp = request.args.get('fp') or request.cookies.get('archer_fp', 'unknown')
    return get_device_tier(fp)

def terminal_access_check(request):
    tier = get_request_tier(request)
    return tier in TERMINAL_ALLOWED_TIERS, tier

def require_tier1(request):
    """Returns (is_tier1: bool, tier: int)."""
    tier = get_request_tier(request)
    return tier == 1, tier

# ── REAL SHELL EXECUTION ─────────────────────────────────
import subprocess, select, os as _os
import platform as _plt
if _plt.system() != 'Windows':
    try:
        import pty as pty
    except ImportError:
        pty = None
else:
    pty = None

@display_app.route('/terminal')
def terminal_page():
    from flask import request as req, Response as FR
    allowed, tier = terminal_access_check(req)
    if not allowed:
        return FR(f"""<!DOCTYPE html><html><body style="background:#000;color:#cc0000;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="text-align:center"><div style="font-size:32px;margin-bottom:16px">🔒</div>
        <div style="font-size:14px;letter-spacing:3px">ACCESS DENIED</div>
        <div style="font-size:10px;color:#333;margin-top:8px;letter-spacing:2px">TIER {tier} — TERMINAL REQUIRES TIER 1</div></div>
        </body></html>""", mimetype='text/html')

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
  <div id="header-title">⚡ ARCHER TERMINAL</div>
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
    from flask import Response as FR
    return FR(terminal_html, mimetype='text/html')

# ── TERMINAL EXEC ENDPOINT ───────────────────────────────
import re as _re
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

@display_app.route('/terminal/exec', methods=['POST'])
def terminal_exec():
    from flask import request as req
    allowed, tier = terminal_access_check(req)
    if not allowed:
        return jsonify({'error': 'Access denied — Tier 1 only'})
    data = req.get_json() or {}
    cmd  = data.get('cmd', '').strip()
    if not cmd:
        return jsonify({'stdout': '', 'stderr': ''})
    if cmd.strip() in ('/help', 'help'):
        maint_state = 'ON' if system_health['maintenance'] else 'OFF'
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
            "\nType any shell command to run it on the server.\n"
        )
        return jsonify({'stdout': help_text, 'stderr': '', 'returncode': 0})

    # Built-in: maintenance mode toggle
    cmd_lower = cmd.strip().lower()
    if cmd_lower in ('maintenance on', 'maintenance mode on', 'maint on'):
        system_health['maintenance'] = True
        return jsonify({'stdout': 'MAINTENANCE MODE ON — all visitors redirected to maintenance page.', 'stderr': '', 'returncode': 0})
    if cmd_lower in ('maintenance off', 'maintenance mode off', 'maint off'):
        system_health['maintenance'] = False
        return jsonify({'stdout': 'MAINTENANCE MODE OFF — normal access restored.', 'stderr': '', 'returncode': 0})
    if cmd_lower in ('maintenance status', 'maint status', 'maintenance'):
        state = 'ON' if system_health['maintenance'] else 'OFF'
        return jsonify({'stdout': f'Maintenance mode: {state}', 'stderr': '', 'returncode': 0})
    if _DANGEROUS.search(cmd):
        return jsonify({'error': 'Blocked: command matches a dangerous pattern'})
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15,
            cwd='/app'
        )
        return jsonify({'stdout': result.stdout, 'stderr': result.stderr, 'returncode': result.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timed out (15s limit)'})
    except Exception as e:
        return jsonify({'error': str(e)})

# ── LOG STREAM (SSE) ─────────────────────────────────────
@display_app.route('/terminal/log_stream')
def terminal_log_stream():
    from flask import request as req, Response as FR
    allowed, _ = terminal_access_check(req)
    if not allowed:
        return FR('', status=403)
    def generate():
        # Send current buffer as snapshot
        with _log_lock:
            snapshot = list(_log_buffer)
        yield f"data: {json.dumps({'snapshot': snapshot})}\n\n"
        last_len = len(snapshot)
        while True:
            time.sleep(0.4)
            with _log_lock:
                current = list(_log_buffer)
            cur_len = len(current)
            if cur_len > last_len:
                for line in current[last_len:]:
                    yield f"data: {json.dumps({'line': line})}\n\n"
            elif cur_len < last_len:
                # Buffer was trimmed (maxlen eviction) — re-snapshot
                yield f"data: {json.dumps({'snapshot': current})}\n\n"
            last_len = cur_len
    return FR(generate(), mimetype='text/event-stream',
              headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── PI TERMINAL STATUS ───────────────────────────────────
pi_tunnel_url = {'url': None, 'online': False, 'last_seen': None}

@display_app.route('/terminal/pi_status')
def pi_status():
    return jsonify(pi_tunnel_url)

@display_app.route('/terminal/pi_register', methods=['POST'])
def pi_register():
    """Pi calls this when it connects to register its tunnel URL"""
    from flask import request as req
    data = req.get_json() or {}
    token = data.get('token', '')
    pi_token = os.environ.get('ARCHER_PI_TOKEN', 'archer2026')
    if token != pi_token:
        return jsonify({'error': 'Invalid token'}), 403
    pi_tunnel_url['url']       = data.get('url')
    pi_tunnel_url['online']    = True
    pi_tunnel_url['last_seen'] = datetime.now().strftime('%I:%M %p')
    print(f"[PI] Connected — tunnel: {pi_tunnel_url['url']}")
    return jsonify({'status': 'registered'})

@display_app.route('/terminal/pi_disconnect', methods=['POST'])
def pi_disconnect():
    pi_tunnel_url['online'] = False
    pi_tunnel_url['url']    = None
    print('[PI] Disconnected')
    return jsonify({'status': 'ok'})


# ── SYSTEM HEALTH TRACKING ──────────────────────────────
system_health = {
    'obd_connected':   True,
    'voice_active':    True,
    'last_obd_update': time.time(),
    'failures':        [],
    'start_time':      time.time(),
    'boot_complete':   False,
    'maintenance':     False,   # toggled via terminal: "maintenance on/off"
    'boot_tokens':     {},      # one-time tokens: token -> expiry (unix time)
}

def log_system_failure(component, reason):
    system_health['failures'].append({
        'time':      time.strftime('%H:%M:%S'),
        'component': component,
        'reason':    reason,
    })
    if len(system_health['failures']) > 50:
        system_health['failures'] = system_health['failures'][-50:]

def get_system_status():
    issues = []
    now = time.time()
    if now - system_health['last_obd_update'] > 10:
        issues.append('OBD_TIMEOUT')
    if not system_health['obd_connected']:
        issues.append('OBD_DISCONNECTED')
    if not system_health['voice_active']:
        issues.append('VOICE_OFFLINE')
    return issues

@display_app.route('/limited')
def limited_mode():
    issues = get_system_status()
    d = truck_state
    rpm      = d.get('rpm', 0)
    speed    = d.get('speed', 0)
    oil      = d.get('oil_temp', 0)
    bat      = d.get('battery', 0)
    msg      = last_archer_msg.get('text', 'Limited mode active.')
    issue_str = ' - '.join(issues) if issues else 'MANUAL OVERRIDE'
    rpm_pct  = min(100, int((rpm / 6200) * 100))
    rpm_color = '#ff0000' if rpm > 5000 else '#ffaa00' if rpm > 3500 else '#00cc44'
    oil_color = '#ff0000' if oil > 225 else '#ffaa00'
    bat_color = '#ff0000' if bat < 12 else '#ffaa00' if bat < 13 else '#00cc44'
    issues_html = ''.join(f'<div class="issues-item">x {i.replace("_"," ")}</div>' for i in (issues if issues else [issue_str]))

    html = f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=480">
<meta http-equiv="refresh" content="5">
<title>Archer Limited</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Share+Tech+Mono&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:480px;height:272px;overflow:hidden;background:#000;color:#fff;font-family:'Share Tech Mono',monospace}}
.wrap{{width:480px;height:272px;display:grid;grid-template-rows:34px 1fr 32px}}
.top{{background:#0a0000;border-bottom:2px solid #cc0000;display:flex;align-items:center;justify-content:space-between;padding:0 12px}}
.top-title{{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:5px;color:#cc0000}}
.top-status{{font-size:12px;color:#ff6600;letter-spacing:2px;animation:blink 1s step-end infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:0.3}}}}
.main{{display:grid;grid-template-columns:1fr 1fr;padding:8px;gap:8px}}
.left{{display:flex;flex-direction:column;align-items:center;justify-content:center;border-right:2px solid #1a1a1a;padding-right:8px}}
.speed-num{{font-family:'Bebas Neue',sans-serif;font-size:96px;color:#fff;line-height:1;letter-spacing:-4px}}
.speed-unit{{font-size:13px;color:#444;letter-spacing:4px;margin-top:-4px}}
.rpm-wrap{{width:100%;margin-top:6px}}
.rpm-row{{display:flex;justify-content:space-between;font-size:12px;color:#555;margin-bottom:3px}}
.bar-bg{{background:#111;height:12px;border-radius:2px;overflow:hidden;border:1px solid #222;position:relative}}
.bar-fill{{height:100%;border-radius:2px}}
.bar-redline{{position:absolute;right:15%;top:0;bottom:0;width:2px;background:#ff0000;opacity:0.6}}
.right{{display:flex;flex-direction:column;gap:8px;padding-left:4px}}
.stat-row{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
.stat{{background:#0a0a0a;border:1px solid #1a1a1a;border-radius:3px;padding:6px;text-align:center}}
.stat-val{{font-family:'Bebas Neue',sans-serif;font-size:20px;line-height:1}}
.stat-lbl{{font-size:10px;color:#444;letter-spacing:2px;margin-top:1px}}
.issues-box{{background:#0d0000;border:1px solid #330000;border-radius:3px;padding:6px 8px}}
.issues-title{{font-size:10px;color:#cc0000;letter-spacing:2px;margin-bottom:3px}}
.issues-item{{font-size:11px;color:#664444;letter-spacing:1px}}
.bottom{{background:#050000;border-top:2px solid #1a1a1a;display:flex;align-items:center;padding:0 12px;gap:8px}}
.archer-tag{{font-size:11px;color:#cc0000;letter-spacing:2px;flex-shrink:0}}
.archer-msg{{font-size:12px;color:#555;font-style:italic;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.limited-badge{{font-size:11px;color:#ff6600;letter-spacing:1px;flex-shrink:0;animation:blink 1.5s step-end infinite}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <div class="top-title">ARCHER</div>
    <div class="top-status">WARNING LIMITED MODE</div>
  </div>
  <div class="main">
    <div class="left">
      <div class="speed-num">{speed}</div>
      <div class="speed-unit">MPH</div>
      <div class="rpm-wrap">
        <div class="rpm-row"><span>RPM</span><span style="color:{rpm_color}">{rpm}</span></div>
        <div class="bar-bg">
          <div class="bar-fill" style="width:{rpm_pct}%;background:{rpm_color}"></div>
          <div class="bar-redline"></div>
        </div>
      </div>
    </div>
    <div class="right">
      <div class="stat-row">
        <div class="stat"><div class="stat-val" style="color:{oil_color}">{oil}F</div><div class="stat-lbl">OIL TEMP</div></div>
        <div class="stat"><div class="stat-val" style="color:{bat_color}">{bat}V</div><div class="stat-lbl">BATTERY</div></div>
      </div>
      <div class="issues-box">
        <div class="issues-title">SYSTEMS OFFLINE</div>
        {issues_html}
      </div>
    </div>
  </div>
  <div class="bottom">
    <span class="archer-tag">ARCHER</span>
    <span class="archer-msg">{msg}</span>
    <span class="limited-badge">LIMITED</span>
  </div>
</div>
</body></html>"""
    return html

@display_app.route('/system_health')
def system_health_api():
    issues = get_system_status()
    return jsonify({
        'status':   'degraded' if issues else 'ok',
        'issues':   issues,
        'failures': system_health['failures'][-10:],
    })

@display_app.route('/health')
def health_endpoint():
    """Comprehensive system health snapshot consumed by archer_init.html fetchVersionInfo().
    Returns: build_ts, git_hash, uptime_seconds, obd_status, gps_fix,
             weather_age_s, active_dtc_count, memory_mb, overall_status.
    No auth required (boot page uses it before session is established).
    """
    import resource as _resource
    now = time.time()
    uptime_s = round(now - system_health['start_time'], 1)

    # OBD status
    if obd2_display.get('connected'):
        obd_status = 'live'
    elif beamng_state.get('connected'):
        obd_status = 'beamng'
    elif sim_random_enabled:
        obd_status = 'sim'
    else:
        obd_status = 'offline'

    # GPS fix quality
    gps_lat = location_data.get('lat')
    gps_lon = location_data.get('lon')
    gps_fix = bool(gps_lat and gps_lon)

    # Last OBD update age
    last_obd_age = round(now - system_health.get('last_obd_update', now), 1)

    # Weather data age
    wx_last = weather.get('last_update', 0)
    weather_age_s = round(now - wx_last, 0) if wx_last else None

    # Active DTC count
    active_dtc_count = sum(1 for f in fault_codes if f.get('status', 'active') == 'active')

    # Process memory (RSS) in MB — graceful fallback
    try:
        mem_mb = round(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        mem_mb = None

    # Build metadata from env / git
    build_ts  = os.environ.get('ARCHER_BUILD_TS', '')
    git_hash  = os.environ.get('ARCHER_GIT_HASH', '')
    if not git_hash:
        try:
            import subprocess as _sp
            git_hash = _sp.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'],
                stderr=_sp.DEVNULL, timeout=2
            ).decode().strip()
        except Exception:
            git_hash = 'unknown'

    # Overall status determination
    issues = get_system_status()
    if any(f.get('severity') == 'critical' for f in fault_codes if f.get('status') == 'active'):
        overall = 'critical'
    elif issues or active_dtc_count > 0:
        overall = 'degraded'
    else:
        overall = 'ok'

    return jsonify({
        'overall':         overall,
        'uptime_seconds':  uptime_s,
        'obd_status':      obd_status,
        'last_obd_age_s':  last_obd_age,
        'gps_fix':         gps_fix,
        'gps_lat':         gps_lat,
        'gps_lon':         gps_lon,
        'weather_age_s':   weather_age_s,
        'weather_temp':    weather.get('temp'),
        'weather_cond':    weather.get('condition'),
        'active_dtc_count': active_dtc_count,
        'memory_mb':       mem_mb,
        'build_ts':        build_ts,
        'git_hash':        git_hash,
        'issues':          issues,
        'maintenance':     system_health.get('maintenance', False),
    })


@display_app.route('/logs')
def logs_endpoint():
    """Return last 50 structured log entries for the Tier 1 debug panel."""
    from flask import request as freq
    ok, tier = require_tier1(freq)
    if not ok:
        return jsonify({'error': 'Tier 1 required'}), 403
    with _archer_log_lock:
        entries = list(_archer_log_buffer)[-50:]
    return jsonify({'logs': entries, 'total': len(_archer_log_buffer)})

@display_app.route('/sensor_history')
def sensor_history_endpoint():
    """Return historical data for a sensor. ?sensor=rpm&minutes=60"""
    from flask import request as freq
    sensor  = freq.args.get('sensor', 'rpm').lower()
    minutes = min(60, max(1, int(freq.args.get('minutes', 10))))
    cutoff  = time.time() - (minutes * 60)
    if sensor not in sensor_history:
        return jsonify({'error': f'Unknown sensor: {sensor}', 'available': list(sensor_history.keys())}), 400
    with _sensor_history_lock:
        entries = [{'timestamp': e['ts'], 'value': e['v']}
                   for e in sensor_history[sensor] if e.get('t', 0) >= cutoff]
    return jsonify({'sensor': sensor, 'minutes': minutes, 'data': entries, 'count': len(entries)})

@display_app.route('/trip_stats')
def trip_stats_endpoint():
    """Return comprehensive trip statistics since engine start."""
    rpm     = truck_state.get('rpm', 0)
    start   = trip_stats.get('start_time')
    elapsed = (time.time() - start) / 60.0 if start else 0.0  # minutes
    avg_spd = (trip_stats['distance_miles'] / (elapsed / 60.0)) if elapsed > 0 else 0

    # Peak values from awareness
    peak_rpm   = awareness.get('peak_rpm', 0)
    peak_speed = truck_state.get('speed', 0)

    # Count high-RPM events (stored in spike_history)
    high_rpm_events = sum(1 for r in spike_history.get('rpm', []) if r > 4500)

    # Hard braking events from awareness
    hard_brake = awareness.get('hard_brake_count', 0)

    score, grade, _ = calculate_drive_score()

    return jsonify({
        'trip_distance_miles':  round(trip_stats['distance_miles'], 2),
        'trip_time_minutes':    round(elapsed, 1),
        'avg_speed_mph':        round(avg_spd, 1),
        'max_speed_mph':        truck_state.get('peak_speed', peak_speed),
        'avg_rpm':              round(sum(spike_history.get('rpm', [0])) / max(1, len(spike_history.get('rpm', [1]))), 0),
        'peak_rpm':             peak_rpm,
        'fuel_used_gallons':    round(trip_stats['fuel_used_gal'], 3),
        'avg_mpg':              round(trip_stats['avg_mpg'], 1),
        'instant_mpg':          round(trip_stats['instant_mpg'], 1),
        'hard_braking_events':  hard_brake,
        'high_rpm_events':      high_rpm_events,
        'engine_running':       rpm > 400,
        'drive_score':          score,
        'drive_grade':          grade,
    })

# ── MAC ADDRESS AUTH SYSTEM ─────────────────────────────
import json as _json_mac

MAC_DB_FILE = 'mac_whitelist.json'

# Default whitelist — add your own MAC as Tier 1
# Format: 'AA:BB:CC:DD:EE:FF': {'tier': 1, 'name': 'Ayden'}
DEFAULT_WHITELIST = {
    'OWNER_MAC_HERE': {'tier': 1, 'name': 'Ayden'},
}

# One-time code system — generated per person
# Format: 'CODE123': {'tier': 2, 'name': 'Jake', 'used': False, 'expires': timestamp}
one_time_codes = {}

# ── MASTER SIGN-IN CODE ───────────────────────────────────────────────────────
# Persistent Tier 1 code that Ayden controls. Auto-enabled when no Tier 1
# devices are registered so he can always get back in.
import random as _rand_master
_master_code = os.environ.get('ARCHER_MASTER_CODE', '250022')
_master_code_enabled = True   # toggled from Tier 1 dashboard

def _check_master_auto_enable():
    """Keep master code enabled if no Tier 1 devices are registered."""
    global _master_code_enabled
    try:
        wl = load_mac_whitelist()
        has_tier1 = any(v.get('tier') == 1 for v in wl.values())
        if not has_tier1:
            _master_code_enabled = True
    except Exception:
        pass

def generate_one_time_code(name, tier):
    """Generate a 6-digit one-time registration code."""
    import random as _random
    code = str(_random.randint(100000, 999999))
    one_time_codes[code] = {
        'name':    name,
        'tier':    int(tier),
        'used':    False,
        'created': time.time(),
        'expires': time.time() + 86400,  # 24 hour expiry
    }
    print(f'[AUTH] Generated code {code} for {name} (Tier {tier})')
    return code

def validate_one_time_code(code):
    """Validate and consume a one-time code."""
    entry = one_time_codes.get(code)
    if not entry:
        return None
    if entry['used']:
        return None
    if time.time() > entry['expires']:
        del one_time_codes[code]
        return None
    entry['used'] = True
    return entry

def cleanup_expired_codes():
    """Remove expired codes."""
    now = time.time()
    expired = [c for c, e in one_time_codes.items() if now > e['expires']]
    for c in expired:
        del one_time_codes[c]


def load_mac_whitelist():
    try:
        if os.path.exists(MAC_DB_FILE):
            with open(MAC_DB_FILE, 'r') as f:
                return _json_mac.load(f)
    except Exception:
        pass
    return dict(DEFAULT_WHITELIST)

def save_mac_whitelist(whitelist):
    try:
        with open(MAC_DB_FILE, 'w') as f:
            _json_mac.dump(whitelist, f, indent=2)
    except Exception as e:
        print(f'[MAC] Save failed: {e}')

def get_client_mac(request_obj):
    """Get MAC address of connecting client from Pi ARP table."""
    client_ip = request_obj.remote_addr
    try:
        result = subprocess.run(['arp', '-n', client_ip], capture_output=True, text=True, timeout=2)
        for line in result.stdout.splitlines():
            parts = line.split()
            for part in parts:
                if ':' in part and len(part) == 17:
                    return part.upper()
    except Exception:
        pass
    return None

def get_tier_for_mac(mac):
    """Returns tier info for a MAC address or None if unknown."""
    if not mac:
        return None
    whitelist = load_mac_whitelist()
    return whitelist.get(mac.upper())

@display_app.route('/')
def index():
    """Main entry — MAC first, cookie fallback, registration last."""
    from flask import request as freq, make_response
    import hashlib as _hashlib

    # 0. Owner PIN bypass (for HuggingFace where ARP doesn't work)
    owner_pin = os.environ.get('ARCHER_OWNER_PIN', '')
    if owner_pin and freq.args.get('pin') == owner_pin:
        cookie_secret = os.environ.get('ARCHER_SECRET', 'archer2500hd')
        import hashlib as _hl2
        token = _hl2.sha256(f'Ayden1{cookie_secret}'.encode()).hexdigest()[:16]
        resp = make_response()
        resp.set_cookie('archer_auth', f'1:Ayden:{token}', max_age=60*60*24*365, httponly=True, samesite='Lax')
        from flask import Response as FR
        r2 = FR(get_tier_html(1), mimetype='text/html')
        r2.headers['Cache-Control'] = 'no-store'
        r2.set_cookie('archer_auth', f'1:Ayden:{token}', max_age=60*60*24*365, httponly=True, samesite='Lax')
        return r2

    # 1. Try MAC detection
    mac = get_client_mac(freq)
    tier_info = get_tier_for_mac(mac)

    # 2. Cookie fallback if MAC not found
    if not tier_info:
        cookie_val = freq.cookies.get('archer_auth', '')
        if cookie_val:
            try:
                parts = cookie_val.split(':')
                if len(parts) == 3:
                    c_tier, c_name, c_token = parts
                    cookie_secret = os.environ.get('ARCHER_SECRET', 'archer2500hd')
                    expected = _hashlib.sha256(f'{c_name}{c_tier}{cookie_secret}'.encode()).hexdigest()[:16]
                    if c_token == expected:
                        tier_info = {'tier': int(c_tier), 'name': c_name}
                        print(f'[AUTH] Cookie auth: {c_name} Tier {c_tier}')
            except Exception:
                pass

    # 3. Route to correct tier
    if tier_info:
        tier = tier_info['tier']
        name = tier_info.get('name', '')
        if tier == 1:
            from flask import Response as FR
            resp = FR(get_tier_html(1), mimetype='text/html')
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return resp
        elif tier == 2:
            return get_tier_html(2, name=name)
        elif tier == 3:
            return get_tier_html(3, name=name)
        elif tier == 4:
            return get_tier_html(4, name=name)

    # 4. Unknown — show fan page (sign in from there)
    _check_master_auto_enable()
    from flask import redirect as _redir
    return _redir('/fans')

def registration_page(mac=None):
    """Show registration page for unknown devices."""
    mac_display = mac or 'Unknown'
    return f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Archer — Sign In</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Share+Tech+Mono&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden}}
body{{background:#000;color:#fff;font-family:'Share Tech Mono',monospace;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;position:relative}}
/* Animated background canvas */
#bg-canvas{{position:fixed;inset:0;z-index:0;pointer-events:none}}
/* Radial glow behind card */
.bg-glow{{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:400px;height:400px;
  background:radial-gradient(ellipse at center,rgba(180,0,0,0.08) 0%,transparent 70%);
  border-radius:50%;animation:glowpulse 4s ease-in-out infinite;pointer-events:none;z-index:0}}
@keyframes glowpulse{{0%,100%{{opacity:0.6;transform:translate(-50%,-50%) scale(1)}}50%{{opacity:1;transform:translate(-50%,-50%) scale(1.15)}}}}
.wrap{{width:100%;max-width:340px;display:flex;flex-direction:column;align-items:center;gap:20px;position:relative;z-index:1}}
.logo{{font-family:'Bebas Neue',sans-serif;font-size:56px;letter-spacing:6px;color:#cc0000;line-height:1;text-shadow:0 0 30px rgba(204,0,0,0.4)}}
.sub{{font-size:10px;color:#555;letter-spacing:3px;text-align:center}}
.card{{background:rgba(8,8,8,0.92);border:1px solid #1a1a1a;border-top:2px solid #cc0000;border-radius:14px;padding:28px 24px;width:100%;display:flex;flex-direction:column;align-items:center;gap:16px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}}
.card-title{{font-family:'Bebas Neue',sans-serif;font-size:18px;color:#888;letter-spacing:3px;text-align:center}}
.card-hint{{font-size:11px;color:#555;letter-spacing:0.5px;text-align:center;line-height:1.7}}
/* Large phone-friendly boxes */
@media(max-width:420px){{
  .code-box{{width:46px;height:62px;font-size:32px}}
  .card{{padding:24px 16px}}
}}
/* ── CODE BOXES ─────────────────────────── */
.code-boxes{{display:flex;gap:10px;justify-content:center;width:100%}}
.code-box{{width:44px;height:56px;background:#0d0d0d;border:1px solid #222;border-radius:8px;
  color:#fff;font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:0;outline:none;
  text-align:center;transition:border-color 0.2s,box-shadow 0.2s,transform 0.15s;
  caret-color:transparent;-webkit-appearance:none}}
.code-box:focus{{border-color:#cc0000;box-shadow:0 0 0 2px rgba(204,0,0,0.2)}}
.code-box.filled{{border-color:#552200;background:#150500}}
/* Shake animation for wrong code */
@keyframes codeshake{{0%,100%{{transform:translateX(0)}}15%{{transform:translateX(-8px)}}30%{{transform:translateX(8px)}}45%{{transform:translateX(-6px)}}60%{{transform:translateX(6px)}}75%{{transform:translateX(-3px)}}90%{{transform:translateX(3px)}}}}
.code-boxes.shake .code-box{{animation:codeshake 0.5s ease-out;border-color:#cc0000}}
/* Success animation */
@keyframes codesuccess{{0%{{transform:scale(1)}}40%{{transform:scale(1.12)}}100%{{transform:scale(1)}}}}
.code-boxes.success .code-box{{animation:codesuccess 0.4s ease-out;border-color:#00cc44;background:#001a00;color:#00cc44}}
.submit-btn{{background:#cc0000;border:none;border-radius:8px;padding:14px;color:#fff;font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:4px;cursor:pointer;width:100%;transition:background 0.15s,transform 0.1s}}
.submit-btn:hover{{background:#dd0000}}
.submit-btn:active{{background:#aa0000;transform:scale(0.98)}}
.submit-btn:disabled{{background:#440000;cursor:default;opacity:0.6}}
.error{{color:#cc0000;font-size:10px;letter-spacing:1px;text-align:center;height:14px;opacity:0;transition:opacity 0.25s}}
.error.on{{opacity:1}}
.fan-note{{font-size:10px;color:#444;letter-spacing:1px;text-align:center}}
.fan-link{{color:#666;text-decoration:none;border-bottom:1px solid #333;padding-bottom:1px;transition:color 0.2s}}
.fan-link:hover{{color:#aaa}}
</style>
</head><body>
<canvas id="bg-canvas"></canvas>
<div class="bg-glow"></div>
<div class="wrap">
  <div class="logo">ARCHER</div>
  <div class="sub">2006 GMC SIERRA 2500HD</div>

  <div class="card">
    <div class="card-title">ENTER ACCESS CODE</div>
    <div class="card-hint">Ayden will give you a 6-digit code.</div>
    <div class="code-boxes" id="code-boxes">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" autocomplete="one-time-code" id="cb0">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" id="cb1">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" id="cb2">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" id="cb3">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" id="cb4">
      <input class="code-box" type="text" inputmode="numeric" maxlength="1" pattern="[0-9]" id="cb5">
    </div>
    <div class="error" id="error-msg">Incorrect code — try again</div>
    <button class="submit-btn" id="submit-btn" onclick="submitCode()">SIGN IN</button>
  </div>

  <div class="fan-note">Just here for the show? <a href="/fans" class="fan-link">Fan page →</a></div>
</div>

<script>
// ── ANIMATED BACKGROUND ───────────────────
(function() {{
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, particles = [];
  function resize() {{
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }}
  resize();
  window.addEventListener('resize', resize);
  // Sparse floating particles (dim red)
  for (let i = 0; i < 30; i++) {{
    particles.push({{
      x: Math.random() * 1000,
      y: Math.random() * 1000,
      vy: -0.1 - Math.random() * 0.2,
      vx: (Math.random() - 0.5) * 0.08,
      r:  0.5 + Math.random() * 1.5,
      a:  Math.random() * 0.3
    }});
  }}
  function frame() {{
    ctx.clearRect(0, 0, W, H);
    particles.forEach(p => {{
      p.x = (p.x + p.vx * W / 1000) % W;
      p.y = (p.y + p.vy * H / 1000 + H) % H;
      ctx.beginPath();
      ctx.arc(p.x / 1000 * W, p.y / 1000 * H, p.r, 0, Math.PI*2);
      ctx.fillStyle = `rgba(180,0,0,${{p.a.toFixed(2)}})`;
      ctx.fill();
    }});
    requestAnimationFrame(frame);
  }}
  frame();
}})();

// ── 6-BOX CODE INPUT ──────────────────────
const boxes = Array.from({{length:6}}, (_,i) => document.getElementById('cb'+i));
const boxWrap = document.getElementById('code-boxes');
const errMsg  = document.getElementById('error-msg');
const submitBtn = document.getElementById('submit-btn');

// Auto-focus first box on load
boxes[0] && boxes[0].focus();

function getCode() {{
  return boxes.map(b => b.value).join('');
}}

function clearBoxes() {{
  boxes.forEach(b => {{ b.value=''; b.classList.remove('filled'); }});
  boxes[0].focus();
}}

function setBoxesState(state) {{
  boxWrap.classList.remove('shake','success');
  void boxWrap.offsetWidth; // reflow
  if (state) boxWrap.classList.add(state);
}}

boxes.forEach((box, i) => {{
  box.addEventListener('input', e => {{
    // Allow only digits
    box.value = box.value.replace(/\\D/g,'').slice(-1);
    box.classList.toggle('filled', box.value !== '');
    if (box.value && i < 5) {{ boxes[i+1].focus(); }}
    if (getCode().length === 6) submitCode();
  }});

  box.addEventListener('keydown', e => {{
    if (e.key === 'Backspace' && !box.value && i > 0) {{
      boxes[i-1].value = '';
      boxes[i-1].classList.remove('filled');
      boxes[i-1].focus();
      e.preventDefault();
    }}
    if (e.key === 'Enter') submitCode();
    // Left/Right arrow navigation
    if (e.key === 'ArrowLeft'  && i > 0) {{ boxes[i-1].focus(); e.preventDefault(); }}
    if (e.key === 'ArrowRight' && i < 5) {{ boxes[i+1].focus(); e.preventDefault(); }}
  }});

  // Paste support: paste 6 digits across all boxes
  box.addEventListener('paste', e => {{
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text').replace(/\\D/g,'').slice(0,6);
    text.split('').forEach((ch, j) => {{
      if (boxes[j]) {{ boxes[j].value = ch; boxes[j].classList.add('filled'); }}
    }});
    const nextEmpty = boxes.findIndex(b => !b.value);
    const focusIdx = nextEmpty === -1 ? 5 : nextEmpty;
    boxes[focusIdx].focus();
    if (text.length === 6) submitCode();
  }});
}});

async function submitCode() {{
  const code = getCode();
  if (code.length !== 6) return;
  submitBtn.disabled = true;
  submitBtn.textContent = '...';
  try {{
    const r = await fetch('/register_mac', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{code, mac: '{mac_display}'}})
    }});
    const d = await r.json();
    if (d.success) {{
      setBoxesState('success');
      submitBtn.textContent = 'OK!';
      await new Promise(res => setTimeout(res, 600));
      window.location.href = d.redirect;
    }} else {{
      setBoxesState('shake');
      errMsg.classList.add('on');
      await new Promise(res => setTimeout(res, 500));
      clearBoxes();
      submitBtn.disabled = false;
      submitBtn.textContent = 'SIGN IN';
      setTimeout(() => errMsg.classList.remove('on'), 2500);
    }}
  }} catch(err) {{
    setBoxesState('shake');
    clearBoxes();
    submitBtn.disabled = false;
    submitBtn.textContent = 'SIGN IN';
  }}
}}
</script>
</body></html>"""

@display_app.route('/register_mac', methods=['POST'])
def register_mac():
    """Register a new device using a one-time code."""
    from flask import request as freq, make_response
    import hashlib as _hashlib
    data = freq.json or {}
    code = data.get('code', '').strip()
    mac  = data.get('mac', '').upper()

    if not code:
        return jsonify({'success': False, 'error': 'Missing code'})

    # Check master Tier 1 code first
    entry = None
    if _master_code_enabled and _master_code and code == _master_code:
        entry = {'name': 'Ayden', 'tier': 1}

    # Fall back to one-time code
    if not entry:
        entry = validate_one_time_code(code)
    if not entry:
        return jsonify({'success': False, 'error': 'Invalid or expired code'})

    tier = entry['tier']
    name = entry['name']

    # Save MAC if we have one
    if mac and mac != 'UNKNOWN':
        whitelist = load_mac_whitelist()
        whitelist[mac] = {
            'tier':          tier,
            'name':          name,
            'registered_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_seen':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        save_mac_whitelist(whitelist)
        print(f'[AUTH] Registered MAC {mac} as {name} (Tier {tier})')

    # Set auth cookie regardless
    cookie_secret = os.environ.get('ARCHER_SECRET', 'archer2500hd')
    token = _hashlib.sha256(f'{name}{tier}{cookie_secret}'.encode()).hexdigest()[:16]
    cookie_val = f'{tier}:{name}:{token}'

    redirects = {1: '/', 2: '/passenger', 3: '/family', 4: '/valet'}
    resp = make_response(jsonify({'success': True, 'redirect': redirects.get(tier, '/'), 'name': name, 'tier': tier}))
    resp.set_cookie('archer_auth', cookie_val, max_age=60*60*24*365, httponly=True, samesite='Lax')
    return resp

@display_app.route('/deregister_mac', methods=['POST'])
def deregister_mac():
    """Remove a MAC from the whitelist (Tier 1 only)."""
    from flask import request as freq
    data = freq.json or {}
    mac  = data.get('mac', '').upper()
    whitelist = load_mac_whitelist()
    if mac in whitelist and whitelist[mac]['tier'] != 1:
        del whitelist[mac]
        save_mac_whitelist(whitelist)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Not found or protected'})

@display_app.route('/registered_devices')
def registered_devices():
    """List all registered devices (Tier 1 only)."""
    from flask import request as freq
    ok, tier = require_tier1(freq)
    if not ok:
        return jsonify({'error': 'Tier 1 required', 'tier': tier}), 403
    whitelist = load_mac_whitelist()
    devices = [{'mac': mac, 'tier': info['tier'], 'name': info['name']}
               for mac, info in whitelist.items()]
    return jsonify({'devices': devices})


@display_app.route('/devices')
def devices_page():
    """Tier 1 only — manage registered devices and generate codes."""
    from flask import request as freq
    ok, tier = require_tier1(freq)
    if not ok:
        return f'<html><body style="background:#000;color:#cc0000;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center"><div style="font-size:32px">🔒</div><div style="font-size:14px;letter-spacing:3px;margin-top:12px">ACCESS DENIED — TIER 1 ONLY</div></div></body></html>', 403
    whitelist = load_mac_whitelist()
    cleanup_expired_codes()
    active_codes = [(c, e) for c, e in one_time_codes.items() if not e['used']]
    
    devices_html = ''.join(f"""
        <div class="device-row">
          <div>
            <div class="d-name">{info['name']}</div>
            <div class="d-meta">Tier {info['tier']} — {mac}</div>
          </div>
          <button onclick="removeDevice('{mac}')" class="d-remove">REMOVE</button>
        </div>""" for mac, info in whitelist.items() if info['tier'] != 1)

    codes_html = ''.join(f"""
        <div class="code-row">
          <div>
            <div class="c-name">{entry['name']} — Tier {entry['tier']}</div>
            <div class="c-code">{code}</div>
            <div class="c-meta">Expires in {max(0,int((entry['expires']-__import__('time').time())/3600))}h</div>
          </div>
          <button onclick="revokeCode('{code}')" class="d-remove">REVOKE</button>
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
</script>
</body></html>"""

@display_app.route('/generate_code', methods=['POST'])
def generate_code_route():
    """Generate a one-time invite code (Tier 1 only)."""
    from flask import request as freq
    ok, tier = require_tier1(freq)
    if not ok:
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    data = freq.json or {}
    name = data.get('name', '').strip()
    inv_tier = data.get('tier', 2)
    if not name:
        return jsonify({'success': False, 'error': 'Name required'})
    code = generate_one_time_code(name, inv_tier)
    return jsonify({'success': True, 'code': code, 'name': name, 'tier': inv_tier})

@display_app.route('/revoke_code', methods=['POST'])
def revoke_code():
    """Revoke an unused invite code."""
    from flask import request as freq
    data = freq.json or {}
    code = data.get('code', '')
    if code in one_time_codes:
        del one_time_codes[code]
    return jsonify({'success': True})


# ── MASTER SIGN-IN CODE API ──────────────────────────────
@display_app.route('/sign_in_code/status')
def sign_in_code_status():
    """Return master code status and registered devices — Tier 1 only."""
    _check_master_auto_enable()
    auth = request.cookies.get('archer_auth', '')
    parts = auth.split(':')
    if len(parts) < 3 or parts[0] != '1':
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    try:
        wl = load_mac_whitelist()
        has_tier1 = any(v.get('tier') == 1 for v in wl.values())
    except Exception:
        wl = {}
        has_tier1 = False

    # Update last_seen for the requesting device's MAC (if known)
    try:
        req_mac = get_client_mac(request)
        if req_mac and req_mac in wl:
            wl[req_mac]['last_seen'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_mac_whitelist(wl)
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
        'success':     True,
        'enabled':     _master_code_enabled,
        'code':        _master_code if _master_code_enabled else None,
        'auto_on':     not has_tier1,
        'device_count': len(wl),
        'tier_counts': tier_counts,
        'devices':     devices_list,
    })

@display_app.route('/sign_in_code/toggle', methods=['POST'])
def sign_in_code_toggle():
    """Toggle master sign-in code on or off — Tier 1 only."""
    global _master_code_enabled
    auth = request.cookies.get('archer_auth', '')
    parts = auth.split(':')
    if len(parts) < 3 or parts[0] != '1':
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    _master_code_enabled = not _master_code_enabled
    return jsonify({'success': True, 'enabled': _master_code_enabled})

@display_app.route('/sign_in_code/refresh', methods=['POST'])
def sign_in_code_refresh():
    """Generate a new master sign-in code — Tier 1 only."""
    global _master_code
    auth = request.cookies.get('archer_auth', '')
    parts = auth.split(':')
    if len(parts) < 3 or parts[0] != '1':
        return jsonify({'success': False, 'error': 'Tier 1 required'}), 403
    import random as _r
    _master_code = str(_r.randint(100000, 999999))
    return jsonify({'success': True, 'code': _master_code})


# ── TIER NOTIFICATION SYSTEM ────────────────────────────
import collections

tier_notifications = collections.deque(maxlen=20)  # pending requests from Tier 2+
tier_responses     = {}  # notification_id -> response status

def add_tier_notification(from_name, message, speed=0, ntype='request'):
    nid = str(uuid.uuid4())[:8]
    tier_notifications.appendleft({
        'id':      nid,
        'from':    from_name,
        'message': message,
        'speed':   speed,
        'type':    ntype,
        'time':    time.strftime('%H:%M:%S'),
        'status':  'pending',
    })
    tier_responses[nid] = 'pending'
    print(f'[TIER NOTIFY] {from_name}: {message}')
    return nid

@display_app.route('/notify_tier1', methods=['POST'])
def notify_tier1():
    from flask import request as freq
    data     = freq.json or {}
    from_name = data.get('from', 'Passenger')
    message  = data.get('message', '')
    speed    = data.get('speed', 0)
    nid      = add_tier_notification(from_name, message, speed)
    return jsonify({'ok': True, 'id': nid})

@display_app.route('/tier_notifications')
def get_tier_notifications():
    return jsonify({'notifications': list(tier_notifications)})

@display_app.route('/tier_cancel', methods=['POST'])
def tier_cancel():
    """Tier 2 cancels a pending request — removes it from queue."""
    from flask import request as freq
    data = freq.json or {}
    nid  = data.get('id')
    if nid:
        tier_responses[nid] = 'cancelled'
        for n in tier_notifications:
            if n['id'] == nid:
                n['status'] = 'cancelled'
                break
        print(f'[TIER CANCEL] {nid} cancelled by passenger')
    return jsonify({'ok': True})

@display_app.route('/tier_respond', methods=['POST'])
def tier_respond():
    from flask import request as freq
    data     = freq.json or {}
    nid      = data.get('id')
    response = data.get('response')  # 'approved' or 'denied'
    action   = data.get('action', '')
    if nid and response:
        tier_responses[nid] = response
        for n in tier_notifications:
            if n['id'] == nid:
                n['status'] = response
                break
        # If approved, execute the action
        if response == 'approved' and action:
            if 'sport' in action.lower():
                truck_state['drive_mode'] = 'sport'
            elif 'comfort' in action.lower():
                truck_state['drive_mode'] = 'comfort'
            elif 'eco' in action.lower():
                truck_state['drive_mode'] = 'eco'
            elif 'tow' in action.lower():
                truck_state['drive_mode'] = 'tow'
            print(f'[TIER RESPOND] {nid} -> {response} ({action})')
    return jsonify({'ok': True})

@display_app.route('/tier_response_status')
def tier_response_status():
    from flask import request as freq
    nid = freq.args.get('id')
    if not nid:
        return jsonify({'status': 'unknown'})
    status = tier_responses.get(nid, 'unknown')
    # Find the notification for context
    for n in tier_notifications:
        if n['id'] == nid:
            return jsonify({'status': status, 'message': n['message'], 'from': n['from']})
    return jsonify({'status': status})


# ── SPOTIFY INTEGRATION ─────────────────────────────────
import urllib.parse
import base64

SPOTIFY_CLIENT_ID     = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
SPOTIFY_REDIRECT_URI  = os.environ.get('SPOTIFY_REDIRECT_URI', '')
SPOTIFY_SCOPES        = 'user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative'

spotify_tokens = {
    'access_token':  None,
    'refresh_token': None,
    'expires_at':    0,
}

dj_state = {'enabled': False, 'last_track_id': None, 'intensity_mode': 'auto'}

# Album art URL cache to avoid repeated API calls for the same track
_art_cache: dict = {}   # track_id -> art_url

def _get_art_cached(track_id, album_images):
    """Return album art URL, using in-memory cache to avoid repeated lookups."""
    if track_id and track_id in _art_cache:
        return _art_cache[track_id]
    url = album_images[0].get('url', '') if album_images else ''
    if track_id and url:
        _art_cache[track_id] = url
        # Trim cache to 200 entries
        if len(_art_cache) > 200:
            oldest = next(iter(_art_cache))
            del _art_cache[oldest]
    return url

def _dj_intensity_level():
    """Return driving intensity level: calm / moderate / aggressive based on live data."""
    try:
        rpm   = live_data.get('rpm', 0)
        speed = live_data.get('speed', 0)
        boost = live_data.get('boost', 0)
        if boost > 12 or rpm > 4500 or speed > 85:
            return 'aggressive'
        if boost > 4 or rpm > 2800 or speed > 55:
            return 'moderate'
        return 'calm'
    except Exception:
        return 'calm'

# DJ intensity playlist preferences (playlist name keywords → intensity)
DJ_PLAYLIST_KEYWORDS = {
    'aggressive': ['hype', 'wot', 'boost', 'race', 'trap', 'hard', 'heavy', 'metal', 'rage', 'beast'],
    'moderate':   ['drive', 'road', 'trip', 'cruise', 'mix', 'vibe', 'workout', 'energy'],
    'calm':       ['chill', 'easy', 'relax', 'mellow', 'acoustic', 'lofi', 'lo-fi', 'coffee'],
}

def _dj_comment(song, artist):
    def _bg():
        try:
            prompt = (f'You are Archer, the AI inside a 2006 GMC Sierra 2500HD. '
                      f'A new song just came on: "{song}" by {artist}. '
                      f'Say something short and natural like a radio DJ introducing it — '
                      f'max 2 sentences, 20 words max. Be direct and confident, '
                      f'match the truck personality. No hashtags or emojis.')
            reply = ask_archer(prompt)
            if reply:
                speak(reply)
        except Exception:
            pass
    import threading
    threading.Thread(target=_bg, daemon=True).start()

def _dj_poll_loop():
    time.sleep(15)          # wait for startup before first poll
    _last_intensity = None
    _intensity_change_count = 0
    while True:
        time.sleep(6)
        try:
            if not dj_state['enabled'] or not spotify_tokens['access_token']:
                continue
            data = spotify_api('GET', 'me/player')
            if not data or not data.get('is_playing'):
                continue
            item     = data.get('item') or {}
            track_id = item.get('id')
            if not track_id or track_id == dj_state['last_track_id']:
                # Track didn't change — check if intensity shifted significantly
                current_intensity = _dj_intensity_level()
                if current_intensity != _last_intensity:
                    _intensity_change_count += 1
                    _last_intensity = current_intensity
                    # After 5 consecutive polls with shifted intensity (~30s), announce the change
                    if _intensity_change_count >= 5:
                        _intensity_change_count = 0
                        song   = item.get('name', '')
                        artist = ', '.join(a['name'] for a in item.get('artists', []))
                        if song and current_intensity == 'aggressive':
                            _dj_comment(song, artist)  # Hype it up during aggressive driving
                else:
                    _intensity_change_count = 0
                continue
            dj_state['last_track_id'] = track_id
            _last_intensity = _dj_intensity_level()
            _intensity_change_count = 0
            song   = item.get('name', '')
            artist = ', '.join(a['name'] for a in item.get('artists', []))
            if song:
                _dj_comment(song, artist)
        except Exception:
            pass

import threading as _dj_thread
_dj_thread.Thread(target=_dj_poll_loop, daemon=True).start()

def spotify_refresh():
    """Refresh the Spotify access token using the refresh token."""
    if not spotify_tokens['refresh_token']:
        return False
    try:
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        data  = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': spotify_tokens['refresh_token']}).encode()
        req   = urllib.request.Request('https://accounts.spotify.com/api/token', data=data,
                    headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            spotify_tokens['access_token'] = resp['access_token']
            if resp.get('refresh_token'):
                spotify_tokens['refresh_token'] = resp['refresh_token']
            spotify_tokens['expires_at']   = time.time() + resp.get('expires_in', 3600) - 60
            return True
    except Exception as e:
        print(f'[SPOTIFY] Refresh failed: {e}')
        return False

def spotify_api(method, endpoint, data=None):
    """Make an authenticated Spotify API call."""
    if time.time() > spotify_tokens['expires_at']:
        if not spotify_refresh():
            return None
    token = spotify_tokens['access_token']
    if not token:
        return None
    try:
        url = f'https://api.spotify.com/v1/{endpoint}'
        if method == 'GET':
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        else:
            body = json.dumps(data).encode() if data else b''
            req  = urllib.request.Request(url, data=body, method=method,
                       headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            if not raw or not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
    except urllib.error.HTTPError as e:
        body = e.read()
        print(f'[SPOTIFY] API error {endpoint}: HTTP {e.code} {body[:200]}')
        return None
    except Exception as e:
        print(f'[SPOTIFY] API error {endpoint}: {e}')
        return None

@display_app.route('/spotify/dj', methods=['POST'])
def spotify_dj_toggle():
    dj_state['enabled'] = not dj_state['enabled']
    if dj_state['enabled']:
        dj_state['last_track_id'] = None   # re-announce current song
        speak("DJ mode on. I've got the intro.")
    else:
        speak("DJ mode off.")
    return jsonify({'enabled': dj_state['enabled']})

@display_app.route('/spotify/disconnect')
def spotify_disconnect():
    spotify_tokens['access_token']  = None
    spotify_tokens['refresh_token'] = None
    spotify_tokens['expires_at']    = 0
    return jsonify({'ok': True})

@display_app.route('/spotify/login')
def spotify_login():
    """Redirect to Spotify OAuth."""
    from flask import request as freq
    redirect_uri = SPOTIFY_REDIRECT_URI or f'{freq.scheme}://{freq.host}/spotify/callback'
    params = urllib.parse.urlencode({
        'client_id':     SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri':  redirect_uri,
        'scope':         SPOTIFY_SCOPES,
        'show_dialog':   'true',
    })
    return json.dumps({'redirect': f'https://accounts.spotify.com/authorize?{params}'}), 200, {'Content-Type': 'application/json'}

@display_app.route('/spotify/callback')
def spotify_callback():
    """Handle Spotify OAuth callback."""
    from flask import request as freq
    code  = freq.args.get('code')
    error = freq.args.get('error')
    if error or not code:
        return f'<h2 style="font-family:monospace;color:#cc0000;background:#000;padding:20px">Spotify auth failed: {error}</h2>'
    if spotify_tokens['access_token'] and time.time() < spotify_tokens['expires_at']:
        import uuid as _suuid2
        _bt2 = str(_suuid2.uuid4())
        system_health['boot_tokens'][_bt2] = time.time() + 15
        return f"""<html><head><style>body{{background:#000;color:#00cc44;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px}}</style></head>
<body><div style="font-size:32px">✓</div><div style="font-size:18px;letter-spacing:3px">ALREADY CONNECTED</div>
<script>setTimeout(()=>{{window.location.href='/display?spotify=ok&_bt={_bt2}'}},1000)</script></body></html>"""
    try:
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        redirect_uri = SPOTIFY_REDIRECT_URI or f'{freq.scheme}://{freq.host}/spotify/callback'
        data  = urllib.parse.urlencode({
            'grant_type':   'authorization_code',
            'code':          code,
            'redirect_uri':  redirect_uri,
        }).encode()
        req = urllib.request.Request('https://accounts.spotify.com/api/token', data=data,
                  headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            spotify_tokens['access_token']  = resp['access_token']
            spotify_tokens['refresh_token'] = resp.get('refresh_token')
            spotify_tokens['expires_at']    = time.time() + resp.get('expires_in', 3600) - 60
            print(f'[SPOTIFY] Authenticated. Granted scopes: {resp.get("scope")}')
            import uuid as _suuid
            _bt = str(_suuid.uuid4())
            system_health['boot_tokens'][_bt] = time.time() + 15
            return f"""<html><head><style>body{{background:#000;color:#00cc44;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px}}</style></head>
<body><div style="font-size:32px">✓</div><div style="font-size:18px;letter-spacing:3px">SPOTIFY CONNECTED</div>
<div style="font-size:12px;color:#444">You can close this tab</div>
<script>setTimeout(()=>{{window.location.href='/display?spotify=ok&_bt={_bt}'}},1500)</script></body></html>"""
    except Exception as e:
        print(f'[SPOTIFY] Token exchange failed: {e}')
        return f'<h2 style="font-family:monospace;color:#cc0000;background:#000;padding:20px">Token exchange failed: {e}</h2>'

@display_app.route('/spotify/status')
def spotify_status():
    """Check if Spotify is connected and return current playback."""
    if not spotify_tokens['access_token']:
        return jsonify({'connected': False})
    data = spotify_api('GET', 'me/player')
    if not data:
        return jsonify({'connected': True, 'playing': False, 'track': None})
    item = data.get('item', {})
    artists   = ', '.join(a['name'] for a in item.get('artists', []))
    album     = item.get('album', {})
    track_id  = item.get('id')
    art_url   = _get_art_cached(track_id, album.get('images', []))
    progress  = data.get('progress_ms', 0)
    duration  = item.get('duration_ms', 1) or 1
    progress_pct = round((progress / duration) * 100, 1)
    intensity = _dj_intensity_level()
    return jsonify({
        'connected':      True,
        'playing':        data.get('is_playing', False),
        'track':          item.get('name', ''),
        'track_id':       track_id,
        'artist':         artists,
        'album':          album.get('name', ''),
        'art':            art_url,
        'progress':       progress,
        'progress_pct':   progress_pct,   # 0-100 percent
        'duration':       duration,
        'volume':         data.get('device', {}).get('volume_percent', 50),
        'device':         data.get('device', {}).get('name', ''),
        'dj_enabled':     dj_state['enabled'],
        'dj_intensity':   intensity,       # calm / moderate / aggressive
    })

@display_app.route('/spotify/play', methods=['POST'])
def spotify_play():
    spotify_api('PUT', 'me/player/play')
    return jsonify({'ok': True})

@display_app.route('/spotify/pause', methods=['POST'])
def spotify_pause():
    spotify_api('PUT', 'me/player/pause')
    return jsonify({'ok': True})

@display_app.route('/spotify/next', methods=['POST'])
def spotify_next():
    spotify_api('POST', 'me/player/next')
    return jsonify({'ok': True})

@display_app.route('/spotify/prev', methods=['POST'])
def spotify_prev():
    spotify_api('POST', 'me/player/previous')
    return jsonify({'ok': True})

@display_app.route('/spotify/volume', methods=['POST'])
def spotify_volume():
    from flask import request as freq
    vol = freq.json.get('volume', 50)
    spotify_api('PUT', f'me/player/volume?volume_percent={vol}')
    return jsonify({'ok': True})

@display_app.route('/spotify/seek', methods=['POST'])
def spotify_seek():
    """Seek to a position in the current track."""
    from flask import request as freq
    pos_ms = int((freq.json or {}).get('position_ms', 0))
    spotify_api('PUT', f'me/player/seek?position_ms={pos_ms}')
    return jsonify({'ok': True})

@display_app.route('/spotify/playlists')
def spotify_playlists():
    """Return user playlists with optional intensity filter and driving-intensity suggestion."""
    from flask import request as freq
    intensity_filter = freq.args.get('intensity')   # 'aggressive' | 'moderate' | 'calm'
    search_q         = (freq.args.get('q') or '').lower().strip()
    try:
        data = spotify_api('GET', 'me/playlists?limit=50')
        if not data:
            return jsonify({'playlists': [], 'suggested': None, 'intensity': _dj_intensity_level()})
        playlists = []
        for p in data.get('items', []):
            try:
                tracks_obj   = p.get('tracks')
                tracks_total = tracks_obj.get('total') if isinstance(tracks_obj, dict) else None
                name_lower   = p['name'].lower()
                # Tag playlist with detected intensity
                detected_intensity = None
                for lvl, keywords in DJ_PLAYLIST_KEYWORDS.items():
                    if any(kw in name_lower for kw in keywords):
                        detected_intensity = lvl
                        break
                # Apply filters
                if intensity_filter and detected_intensity != intensity_filter:
                    continue
                if search_q and search_q not in name_lower:
                    continue
                playlists.append({
                    'id':        p['id'],
                    'name':      p['name'],
                    'tracks':    tracks_total,
                    'art':       p['images'][0]['url'] if p.get('images') else '',
                    'intensity': detected_intensity,
                })
            except Exception:
                continue

        # Suggest a playlist that matches current driving intensity
        current_intensity = _dj_intensity_level()
        suggested = next(
            (pl for pl in playlists if pl.get('intensity') == current_intensity),
            None
        )
        return jsonify({
            'playlists':       playlists,
            'total':           len(playlists),
            'intensity':       current_intensity,
            'suggested':       suggested,
            'dj_intensity_mode': dj_state.get('intensity_mode', 'auto'),
        })
    except Exception as e:
        print(f'[SPOTIFY] Playlists error: {e}')
        return jsonify({'error': str(e), 'playlists': []}), 500


@display_app.route('/spotify/dj/intensity', methods=['POST'])
def spotify_dj_intensity():
    """Override DJ intensity mode: auto | calm | moderate | aggressive."""
    from flask import request as freq
    mode = (freq.json or {}).get('mode', 'auto')
    if mode not in ('auto', 'calm', 'moderate', 'aggressive'):
        return jsonify({'error': 'Invalid mode'}), 400
    dj_state['intensity_mode'] = mode
    return jsonify({'intensity_mode': mode, 'current': _dj_intensity_level()})

@display_app.route('/spotify/play_playlist', methods=['POST'])
def spotify_play_playlist():
    from flask import request as freq
    playlist_id = freq.json.get('playlist_id')
    if playlist_id:
        spotify_api('PUT', 'me/player/play', {'context_uri': f'spotify:playlist:{playlist_id}'})
    return jsonify({'ok': True})

@display_app.route('/spotify/queue')
def spotify_queue():
    data = spotify_api('GET', 'me/player/queue')
    if not data:
        return jsonify({'queue': []})
    queue_items = []
    for item in data.get('queue', [])[:8]:
        artists = ', '.join(a['name'] for a in item.get('artists', []))
        queue_items.append({'name': item.get('name',''), 'artist': artists,
                            'duration': item.get('duration_ms', 0)})
    return jsonify({'queue': queue_items})


@display_app.route('/specs')
def spec_sheet():
    specs = get_spec_data()
    html  = f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#000;color:#fff;font-family:monospace;padding:20px}}
h1{{color:#cc0000;font-size:22px;letter-spacing:4px;margin-bottom:4px}}
h2{{color:#555;font-size:10px;letter-spacing:3px;margin-bottom:20px}}
.spec{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #111;font-size:13px}}
.label{{color:#555;letter-spacing:1px}}
.value{{color:#fff;font-weight:bold;text-align:right}}
.record{{color:#cc0000}}
.section{{color:#cc0000;font-size:9px;letter-spacing:3px;margin:16px 0 6px;border-bottom:1px solid #cc0000;padding-bottom:4px}}
</style></head><body>
<h1>ARCHER</h1>
<h2>{specs["vehicle"].upper()}</h2>
<div class="section">ENGINE</div>
<div class="spec"><span class="label">ENGINE</span><span class="value">{specs["engine"]}</span></div>
<div class="spec"><span class="label">TRANSMISSION</span><span class="value">{specs["trans"]}</span></div>
<div class="spec"><span class="label">EST HORSEPOWER</span><span class="value record">{specs["hp_est"]} HP</span></div>
<div class="spec"><span class="label">ETHANOL</span><span class="value">E{specs["ethanol"]}</span></div>
<div class="section">CHASSIS</div>
<div class="spec"><span class="label">SUSPENSION</span><span class="value">{specs["suspension"]}</span></div>
<div class="spec"><span class="label">WHEELS</span><span class="value">{specs["wheels"]}</span></div>
<div class="spec"><span class="label">BRAKES</span><span class="value">{specs["brakes"]}</span></div>
<div class="section">RECORDS</div>
<div class="spec"><span class="label">BEST ET</span><span class="value record">{specs["best_et"] or "--"}s</span></div>
<div class="spec"><span class="label">BEST 0-60</span><span class="value record">{specs["best_060"] or "--"}s</span></div>
<div class="spec"><span class="label">TOP SPEED</span><span class="value record">{specs["top_speed"] or "--"} MPH</span></div>
<div class="section">BUILD</div>
<div class="spec"><span class="label">BUILD STARTED</span><span class="value">{specs["build_year"]}</span></div>
<div class="spec"><span class="label">TOTAL MODS</span><span class="value">{specs["total_mods"]}</span></div>
<div class="spec"><span class="label">COLOR</span><span class="value">{specs["color"]}</span></div>
<br><p style="color:#333;font-size:9px;text-align:center;letter-spacing:2px">POWERED BY ARCHER AI</p>
</body></html>"""
    from flask import Response as FR
    return FR(html, mimetype='text/html')

@display_app.route('/truck.jpg')
def truck_image():
    from flask import Response as FR
    import os
    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'truck.jpg')
    if os.path.exists(img_path):
        with open(img_path, 'rb') as f:
            return FR(f.read(), mimetype='image/jpeg')
    return FR('', status=404)

@display_app.route('/display_data')
def display_data_endpoint():
    from flask import request as flask_request
    session_id  = flask_request.args.get('sid', 'unknown')
    fingerprint = flask_request.args.get('fp', 'unknown')
    ip          = flask_request.remote_addr or 'unknown'
    agent       = flask_request.headers.get('User-Agent', '')[:50]
    if session_id not in connected_clients:
        log_client_connect(session_id, ip, agent)
    else:
        connected_clients[session_id]['last_seen'] = time.time()
    d = get_display_data()
    d['spike_history']     = spike_history
    d['connected_clients'] = len(connected_clients)
    # Override tier with device fingerprint tier
    device_tier = get_device_tier(fingerprint)
    d['device_tier']       = device_tier
    d['device_registered'] = fingerprint in trusted_devices
    d['device_name']       = trusted_devices.get(fingerprint, {}).get('name', '')
    return jsonify(d)

@display_app.route('/audio_stream')
def audio_stream():
    client_queue = queue.Queue()
    def generate():
        with audio_lock:
            audio_clients.append(client_queue)
        try:
            while True:
                try:
                    chunk = client_queue.get(timeout=30)
                    yield chunk
                except Exception:
                    break
        finally:
            with audio_lock:
                if client_queue in audio_clients:
                    audio_clients.remove(client_queue)
    return Response(
        stream_with_context(generate()),
        mimetype='audio/mpeg',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

# ── TIER-SPECIFIC HTML GENERATORS ───────────────────────
def get_tier_html(tier, name=None):
    """Returns tier HTML — loads from file or falls back to basic."""
    import os
    tier_files = {
        2: 'archer_tier2.html',
        3: 'archer_tier3.html',
        4: 'archer_tier4.html',
    }
    if tier == 1:
        if os.path.exists('archer_tier1.html'):
            with open('archer_tier1.html', 'r', encoding='utf-8') as f:
                return f.read()
        return DISPLAY_HTML.replace("'profile-name'>AYDEN", "'profile-name' style='color:#cc0000'>AYDEN ★")
    
    html_file = tier_files.get(tier)
    if html_file and os.path.exists(html_file):
        with open(html_file, 'r', encoding='utf-8') as f:
            html = f.read()
        if name and tier == 2:
            html = html.replace("const passengerName = 'Khloe'", f"const passengerName = '{name}'")
        return html
    
    # Fallback
    return f"""<!DOCTYPE html><html><body style="background:#000;color:#fff;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh">
    <div style="text-align:center"><div style="color:#cc0000;font-size:24px;letter-spacing:4px">ARCHER</div>
    <div style="color:#444;font-size:11px;margin-top:8px">TIER {tier}</div></div></body></html>"""


_BOOT_EXEMPT = {'/boot', '/init', '/boot/status', '/maintenance', '/fans', '/fan', '/fans/ask', '/register', '/', '/static'}

_BOOT_EXEMPT_PREFIXES = ('/static', '/spotify/', '/terminal', '/weather/compare')

@display_app.before_request
def require_boot():
    """Gate every page load behind boot. One-time tokens let the post-boot
    redirect through; everything else (refresh, new tab, reopen) hits boot."""
    from flask import request as _req, redirect as _redir
    if display_app.testing:
        return None
    path = _req.path
    if path in _BOOT_EXEMPT or any(path.startswith(p) for p in _BOOT_EXEMPT_PREFIXES):
        return None
    # Maintenance — browser page loads only; AJAX passes through so terminal works
    maintenance_active = (
        system_health['maintenance'] or
        os.environ.get('MAINTENANCE_MODE', '').strip() in ('1', 'true', 'yes')
    )
    if maintenance_active:
        if 'text/html' in _req.headers.get('Accept', ''):
            return _redir('/maintenance')
        return None
    # Non-HTML requests (AJAX, SSE, etc.) never need boot
    if 'text/html' not in _req.headers.get('Accept', ''):
        return None
    # Validate one-time boot token issued by /boot/status on success.
    # Without a valid token every page load — including refresh and reopen — runs boot.
    now = time.time()
    bt = _req.args.get('_bt', '')
    tokens = system_health['boot_tokens']
    # Purge expired tokens
    expired = [k for k, exp in tokens.items() if exp < now]
    for k in expired:
        tokens.pop(k, None)
    if bt and bt in tokens:
        tokens.pop(bt)   # consume — one-time use
        return None      # let the destination page through
    # No valid token → send to boot, preserving the intended destination
    dest = _req.path
    if _req.query_string:
        # Strip any stale _bt from query before forwarding
        from urllib.parse import urlencode, parse_qs
        qs = {k: v for k, v in parse_qs(_req.query_string.decode('utf-8', errors='replace')).items() if k != '_bt'}
        dest += ('?' + urlencode({k: v[0] for k, v in qs.items()})) if qs else ''
    return _redir(f'/boot?next={dest}')


@display_app.route('/boot/status')
def boot_status():
    """Real system health checks for the boot page. Called by archer_init.html JS.
    ?reveal=N returns only the first N checks so the UI can animate them in one at a time.
    When reveal >= total checks, sets session boot_complete and returns ready=True.
    """
    from flask import request as _req, session as _sess
    reveal = int(_req.args.get('reveal', 0))

    all_checks = []
    uptime_s = round(time.time() - system_health['start_time'], 1)

    # 1. Archer core
    all_checks.append({'id': 'core', 'label': 'ARCHER CORE', 'status': 'ok', 'detail': f'up {uptime_s}s'})

    # 2. Auth system
    secret = os.environ.get('ARCHER_SECRET', '')
    secret_ok = bool(secret) and secret != 'archer2500hd'
    all_checks.append({
        'id': 'auth', 'label': 'AUTH SYSTEM',
        'status': 'ok' if secret_ok else 'warn',
        'detail': 'configured' if secret_ok else 'default key — set ARCHER_SECRET',
    })

    # 3. Vehicle profile / saved state
    save_ok = os.path.exists(SAVE_FILE)
    all_checks.append({
        'id': 'profile', 'label': 'VEHICLE PROFILE',
        'status': 'ok' if save_ok else 'warn',
        'detail': 'state loaded' if save_ok else 'no saved state — fresh start',
    })

    # 4. Sensor link
    if obd2_display['connected']:
        sensor_status, sensor_detail = 'ok', 'OBD live'
    elif beamng_state.get('connected'):
        sensor_status, sensor_detail = 'ok', 'BeamNG bridge'
    elif sim_random_enabled:
        sensor_status, sensor_detail = 'warn', 'simulator mode'
    else:
        sensor_status, sensor_detail = 'warn', 'no sensor data'
    all_checks.append({'id': 'sensors', 'label': 'SENSOR LINK', 'status': sensor_status, 'detail': sensor_detail})

    # 5. AI backend
    hf_ok   = bool(os.environ.get('HF_TOKEN', '').strip())
    groq_ok = bool(os.environ.get('GROQ_API_KEY', '').strip())
    if hf_ok and groq_ok:
        ai_detail = 'HuggingFace + Groq'
    elif hf_ok:
        ai_detail = 'HuggingFace'
    elif groq_ok:
        ai_detail = 'Groq'
    else:
        ai_detail = 'local fallback only'
    all_checks.append({
        'id': 'ai', 'label': 'AI BACKEND',
        'status': 'ok' if (hf_ok or groq_ok) else 'warn',
        'detail': ai_detail,
    })

    # 6. Weather API
    weather_fetched = weather.get('last_update', 0) > 0 and weather.get('temp') is not None
    if weather_fetched:
        w_detail = f"{weather['temp']}F — {weather['condition']}"
        w_status = 'ok'
    else:
        w_status, w_detail = 'warn', 'pending first fetch'
    all_checks.append({'id': 'weather', 'label': 'WEATHER API', 'status': w_status, 'detail': w_detail})

    # 7. Voice / TTS
    if _IS_PI:
        if _PIPER_AVAILABLE and _VOSK_AVAILABLE:
            v_status, v_detail = 'ok', 'piper TTS + vosk STT'
        elif _PIPER_AVAILABLE:
            v_status, v_detail = 'warn', 'piper TTS — no STT'
        elif _VOSK_AVAILABLE:
            v_status, v_detail = 'warn', 'vosk STT — no TTS'
        else:
            v_status, v_detail = 'warn', 'no local voice stack'
    else:
        dectalk_ok = os.path.exists('/opt/dectalk/say')
        if dectalk_ok:
            v_status, v_detail = 'ok', 'DECtalk TTS'
        elif hf_ok:
            v_status, v_detail = 'ok', 'edge-tts (cloud)'
        else:
            v_status, v_detail = 'warn', 'web speech fallback'
    all_checks.append({'id': 'voice', 'label': 'VOICE SYSTEM', 'status': v_status, 'detail': v_detail})

    # 8. Memory store
    mem_ok = os.path.exists(SAVE_FILE)
    try:
        if mem_ok:
            with open(SAVE_FILE, 'r') as _f:
                _json = json.load(_f)
            mem_detail = f"{len(_json)} keys"
            mem_status = 'ok'
        else:
            mem_status, mem_detail = 'warn', 'will create on first save'
    except Exception:
        mem_status, mem_detail = 'fail', 'corrupt save file'
    all_checks.append({'id': 'memory', 'label': 'MEMORY CORE', 'status': mem_status, 'detail': mem_detail})

    # 9. Spotify (optional)
    if SPOTIFY_CLIENT_ID:
        spot_status = 'ok' if spotify_tokens.get('access_token') else 'warn'
        spot_detail = 'authenticated' if spotify_tokens.get('access_token') else 'not linked'
        all_checks.append({'id': 'spotify', 'label': 'SPOTIFY', 'status': spot_status, 'detail': spot_detail})

    total = len(all_checks)
    # Return only the first `reveal` checks; reveal=0 means return all (fallback)
    visible = all_checks[:reveal] if reveal > 0 else all_checks
    all_done = reveal >= total
    all_ok   = all(c['status'] != 'fail' for c in all_checks)
    ready    = all_done and all_ok
    boot_token = ''
    if ready:
        import uuid as _uuid
        system_health['boot_complete'] = True
        boot_token = str(_uuid.uuid4())
        system_health['boot_tokens'][boot_token] = time.time() + 15  # 15s to use it
    return jsonify({
        'checks': visible,
        'total':  total,
        'token':  boot_token,
        'ready':  ready,
        'uptime': uptime_s,
    })


@display_app.route('/boot')
@display_app.route('/init')
def boot_page():
    """Boot/initialization splash — animates then redirects to /."""
    from flask import Response as FR
    if os.path.exists('archer_init.html'):
        with open('archer_init.html', 'r', encoding='utf-8') as f:
            html = f.read()
        return FR(html, mimetype='text/html')
    return FR('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">ARCHER INITIALIZING...</body></html>', mimetype='text/html')


@display_app.route('/tpms', methods=['GET', 'POST'])
def tpms_endpoint():
    """GET  → return current TPMS data for all four wheels.
    POST → update one or more wheel pressures.
           Body: {"fl": 35.5, "fr": 35.0, "rl": 36.0, "rr": 36.0}
           or:   {"wheel": "fl", "psi": 35.5}
    """
    from flask import request as _req
    ok, tier = require_tier1(_req)
    if not ok:
        return jsonify({'error': 'Tier 1 required'}), 403

    if _req.method == 'POST':
        body = _req.get_json(silent=True) or {}
        wheels = ['fl', 'fr', 'rl', 'rr']
        updated = {}
        # Support both {"wheel":"fl","psi":35} and {"fl":35,"fr":35,...}
        single_wheel = body.get('wheel', '').lower()
        if single_wheel in wheels and 'psi' in body:
            try:
                psi = float(body['psi'])
                psi = max(0.0, min(120.0, psi))
                tpms[single_wheel]['psi'] = psi
                if psi < 28:
                    tpms[single_wheel]['status'] = 'critical'
                elif psi < 32:
                    tpms[single_wheel]['status'] = 'low'
                else:
                    tpms[single_wheel]['status'] = 'ok'
                updated[single_wheel] = psi
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid psi value'}), 400
        else:
            for w in wheels:
                if w in body:
                    try:
                        psi = float(body[w])
                        psi = max(0.0, min(120.0, psi))
                        tpms[w]['psi'] = psi
                        tpms[w]['status'] = 'critical' if psi < 28 else ('low' if psi < 32 else 'ok')
                        updated[w] = psi
                    except (ValueError, TypeError):
                        pass
        if updated:
            save_state()
        return jsonify({'updated': updated, 'tpms': tpms})

    # GET — return current readings with status codes
    target = TARGET_PSI.get(truck_state.get('mode', 'street'), 35)
    result = {}
    for w, data in tpms.items():
        psi = data['psi']
        delta = round(psi - target, 1)
        result[w] = {
            'psi':    psi,
            'temp':   data.get('temp', 75),
            'status': data.get('status', 'ok'),
            'target': target,
            'delta':  delta,
        }
    any_low  = any(v['psi'] < 28 for v in result.values())
    any_warn = any(v['psi'] < 32 for v in result.values())
    return jsonify({
        'tpms':    result,
        'target':  target,
        'overall': 'critical' if any_low else ('warn' if any_warn else 'ok'),
    })


@display_app.route('/maintenance')
def maintenance_page():
    """Shown when maintenance mode is active. Redirects to / if maintenance is off."""
    from flask import Response as FR, redirect as _redir
    # If maintenance was turned off, send them back through the normal flow
    maintenance_active = (
        system_health['maintenance'] or
        os.environ.get('MAINTENANCE_MODE', '').strip() in ('1', 'true', 'yes')
    )
    if not maintenance_active:
        return _redir('/')
    if os.path.exists('archer_maintenance.html'):
        with open('archer_maintenance.html', 'r', encoding='utf-8') as f:
            html = f.read()
        return FR(html, mimetype='text/html')
    # Inline fallback
    return FR('''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archer — Maintenance</title>
<style>*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;color:#fff;font-family:monospace;display:flex;align-items:center;
justify-content:center;height:100vh;text-align:center}
.r{color:#cc0000;font-size:28px;letter-spacing:6px;margin-bottom:16px}
.s{color:#444;font-size:11px;letter-spacing:3px}</style></head>
<body><div><div class="r">ARCHER UNDER MAINTENANCE</div>
<div class="s">SYSTEMS TEMPORARILY OFFLINE — CHECK BACK SHORTLY</div></div>
<script>setTimeout(()=>window.location.href='/',30000)</script></body></html>''', mimetype='text/html')


@display_app.route('/weather/compare/data')
def weather_compare_data():
    """Fetch current conditions from multiple APIs in parallel and return comparison JSON."""
    import concurrent.futures as _cf
    global _nws_station_url, _nws_forecast_url
    lat = location_data.get('lat') or 37.6456
    lon = location_data.get('lon') or -91.5362
    hdr = {'User-Agent': 'Archer/1.0 archer@ayden.dev'}

    # Ensure NWS URLs are resolved (may be None if get_weather()'s init failed at startup)
    if not _nws_forecast_url or not _nws_station_url:
        try:
            pts_url = f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}'
            with urllib.request.urlopen(urllib.request.Request(pts_url, headers=hdr), timeout=8) as r:
                pts = json.loads(r.read())
            _nws_forecast_url = pts['properties']['forecastHourly']
            stations_url = pts['properties']['observationStations']
            with urllib.request.urlopen(urllib.request.Request(stations_url, headers=hdr), timeout=8) as r:
                stations = json.loads(r.read())
            _nws_station_url = stations['features'][0]['properties']['stationIdentifier']
        except Exception:
            pass

    def _fetch_nws_obs():
        if not _nws_station_url:
            return None, None
        url = f'https://api.weather.gov/stations/{_nws_station_url}/observations/latest'
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=6) as r:
            props = json.loads(r.read())['properties']
        raw_c = (props.get('temperature') or {}).get('value')
        if raw_c is None:
            return None, None
        temp_f = int(raw_c * 9 / 5 + 32)
        desc = (props.get('textDescription') or '').lower()
        cond = ('Thunderstorm' if 'thunder' in desc else
                'Snow'         if 'snow' in desc else
                'Freezing Rain' if 'freez' in desc or 'sleet' in desc else
                'Fog'          if 'fog' in desc or 'mist' in desc else
                'Rain'         if any(w in desc for w in ('rain','shower','drizzle')) else
                'Overcast'      if 'overcast' in desc else
                'Mostly Cloudy' if 'mostly cloudy' in desc else
                'Cloudy'        if 'cloudy' in desc else
                'Partly Cloudy' if 'partly' in desc or 'mostly' in desc else
                'Clear'         if any(w in desc for w in ('clear','sunny','fair')) else
                desc[:20].title() or 'Cloudy')
        return temp_f, cond

    def _fetch_nws_forecast():
        if not _nws_forecast_url:
            return None, None
        with urllib.request.urlopen(urllib.request.Request(_nws_forecast_url, headers=hdr), timeout=6) as r:
            periods = json.loads(r.read())['properties']['periods']
        try:
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc)
            period = next((p for p in periods
                           if datetime.fromisoformat(p['startTime']) <= now <= datetime.fromisoformat(p['endTime'])),
                          periods[0])
        except Exception:
            period = periods[0]
        short = (period.get('shortForecast') or '').lower()
        cond = ('Thunderstorm' if 'thunder' in short else
                'Snow'         if 'snow' in short else
                'Freezing Rain' if 'freez' in short or 'sleet' in short else
                'Fog'          if 'fog' in short or 'mist' in short else
                'Scattered Showers' if 'vicinity' in short else
                'Rain Showers' if 'shower' in short else
                'Rain'         if any(w in short for w in ('rain','drizzle')) else
                'Mostly Cloudy' if 'mostly cloudy' in short else
                'Cloudy'        if 'cloudy' in short or 'overcast' in short else
                'Partly Cloudy' if 'partly' in short or 'mostly' in short else
                'Clear'        if any(w in short for w in ('clear','sunny','fair')) else
                short[:20].title() or 'Cloudy')
        return period['temperature'], cond

    def _fetch_wttr():
        url = f'https://wttr.in/{lat:.4f},{lon:.4f}?format=j1'
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        cur = d['current_condition'][0]
        temp_f = int(cur['temp_F'])
        desc   = (cur['weatherDesc'][0]['value'] or '').lower()
        cond = (_wmo_condition_from_code := None) or (
            'Thunderstorm' if 'thunder' in desc else
            'Snow'         if 'snow' in desc or 'blizzard' in desc else
            'Freezing Rain' if 'freez' in desc or 'sleet' in desc or 'ice' in desc else
            'Fog'          if 'fog' in desc or 'mist' in desc else
            'Scattered Showers' if 'vicinity' in desc else
            'Rain Showers' if 'shower' in desc else
            'Rain'         if any(w in desc for w in ('rain','drizzle')) else
            'Overcast'      if 'overcast' in desc else
            'Mostly Cloudy' if 'mostly cloudy' in desc else
            'Cloudy'        if 'cloudy' in desc or 'cloud' in desc else
            'Partly Cloudy' if 'partly' in desc or 'mostly' in desc else
            'Clear'        if any(w in desc for w in ('clear','sunny','fair','bright')) else
            desc[:20].title() or 'Cloudy')
        return temp_f, cond

    def _fetch_7timer():
        url = f'http://www.7timer.info/bin/api.pl?lon={lon:.4f}&lat={lat:.4f}&product=civil&output=json'
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        ds = d['dataseries'][0]
        temp_f = int(ds['temp2m'] * 9 / 5 + 32)
        wx = ds.get('weather', '').lower()
        cond = ('Thunderstorm' if 'ts' in wx else
                'Snow'         if 'snow' in wx else
                'Rain'         if 'rain' in wx else
                'Fog'          if 'fog' in wx else
                'Cloudy'       if 'cloudy' in wx or 'overcast' in wx else
                'Partly Cloudy' if 'pcloudy' in wx or 'mcloudy' in wx else
                'Clear'        if 'clear' in wx or 'sunny' in wx else 'Cloudy')
        return temp_f, cond

    # First row: Archer's live reading (cached — no extra HTTP call)
    w = weather
    archer_src = 'Visual Crossing' if _vc_key else ('WUnderground PWS' if _wu_key else 'NWS')
    archer_result = {
        'name':      f'Archer — {archer_src} (live)',
        'temp':      w.get('temp'),
        'condition': w.get('condition'),
        'error':     None if w.get('temp') is not None else 'no data yet',
    }

    weatherapi_key   = os.environ.get('WEATHERAPI_KEY', '')
    openweather_key  = os.environ.get('OPENWEATHER_KEY', '')
    tomorrow_key     = os.environ.get('TOMORROW_KEY', '')
    accuweather_key  = os.environ.get('ACCUWEATHER_KEY', '')
    visualcross_key  = os.environ.get('VISUALCROSSING_KEY', '')

    def _fetch_wunderground_pws():
        if not _wu_key:
            raise RuntimeError('WUNDERGROUND_KEY not set')
        wu_url = (f'https://api.weather.com/v2/pws/observations/nearby'
                  f'?geocode={lat:.4f},{lon:.4f}&limit=1&format=json&units=e&apiKey={_wu_key}')
        with urllib.request.urlopen(urllib.request.Request(wu_url, headers=hdr), timeout=8) as r:
            wu = json.loads(r.read())
        obs  = wu['observations'][0]
        imp  = obs.get('imperial', {})
        temp = int(imp['temp'])
        wx   = (obs.get('wxPhrase') or '').strip()
        cond = _parse_condition(wx) if wx else 'Cloudy'
        return temp, cond

    def _fetch_weatherapi():
        if not weatherapi_key:
            raise RuntimeError('WEATHERAPI_KEY not set')
        url = (f'https://api.weatherapi.com/v1/current.json'
               f'?key={weatherapi_key}&q={lat:.4f},{lon:.4f}&aqi=no')
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        cur   = d['current']
        temp  = int(cur['temp_f'])
        desc  = (cur.get('condition', {}).get('text') or '').lower()
        cond = ('Thunderstorm'   if 'thunder' in desc else
                'Snow'           if 'snow' in desc or 'blizzard' in desc else
                'Freezing Rain'  if 'freez' in desc or 'sleet' in desc or 'ice pellet' in desc else
                'Fog'            if 'fog' in desc or 'mist' in desc else
                'Scattered Showers' if 'vicinity' in desc else
                'Rain Showers'   if 'shower' in desc else
                'Rain'           if 'rain' in desc or 'drizzle' in desc else
                'Overcast'       if 'overcast' in desc else
                'Mostly Cloudy'  if 'mostly cloudy' in desc else
                'Cloudy'         if 'cloudy' in desc or 'cloud' in desc else
                'Partly Cloudy'  if 'partly' in desc or 'mostly' in desc else
                'Clear'          if any(w in desc for w in ('clear','sunny','fair','bright')) else
                desc[:20].title() or 'Cloudy')
        return temp, cond

    def _fetch_openweather():
        if not openweather_key:
            raise RuntimeError('OPENWEATHER_KEY not set')
        url = (f'https://api.openweathermap.org/data/2.5/weather'
               f'?lat={lat:.4f}&lon={lon:.4f}&appid={openweather_key}&units=imperial')
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        temp = int(d['main']['temp'])
        desc = (d['weather'][0].get('description') or '').lower()
        cond = ('Thunderstorm'   if 'thunder' in desc else
                'Snow'           if 'snow' in desc or 'blizzard' in desc else
                'Freezing Rain'  if 'freez' in desc or 'sleet' in desc or 'ice' in desc else
                'Fog'            if 'fog' in desc or 'mist' in desc or 'haze' in desc else
                'Scattered Showers' if 'shower' in desc and 'light' not in desc else
                'Rain Showers'   if 'shower' in desc else
                'Drizzle'        if 'drizzle' in desc else
                'Rain'           if 'rain' in desc else
                'Overcast'       if 'overcast' in desc else
                'Mostly Cloudy'  if 'mostly cloudy' in desc else
                'Cloudy'         if 'cloud' in desc else
                'Partly Cloudy'  if 'partly' in desc or 'few' in desc or 'scattered' in desc else
                'Clear'          if any(w in desc for w in ('clear','sunny','fair')) else
                desc[:20].title() or 'Cloudy')
        return temp, cond

    def _fetch_tomorrow():
        if not tomorrow_key:
            raise RuntimeError('TOMORROW_KEY not set')
        url = (f'https://api.tomorrow.io/v4/weather/realtime'
               f'?location={lat:.4f},{lon:.4f}&units=imperial&apikey={tomorrow_key}')
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        vals = d['data']['values']
        temp = int(vals['temperature'])
        code = int(vals.get('weatherCode', 1000))
        cond = ({
            1000: 'Clear', 1001: 'Cloudy', 1100: 'Clear', 1101: 'Partly Cloudy',
            1102: 'Mostly Cloudy', 2000: 'Fog', 2100: 'Fog',
            4000: 'Drizzle', 4001: 'Rain', 4200: 'Rain', 4201: 'Rain',
            5000: 'Snow', 5001: 'Snow', 5100: 'Snow', 5101: 'Snow',
            6000: 'Freezing Rain', 6001: 'Freezing Rain', 6200: 'Freezing Rain', 6201: 'Freezing Rain',
            7000: 'Freezing Rain', 7101: 'Freezing Rain', 7102: 'Freezing Rain',
            8000: 'Thunderstorm',
        }).get(code, 'Cloudy')
        return temp, cond

    def _fetch_accuweather():
        if not accuweather_key:
            raise RuntimeError('ACCUWEATHER_KEY not set')
        loc_url = (f'https://dataservice.accuweather.com/locations/v1/cities/geoposition/search'
                   f'?q={lat:.4f},{lon:.4f}&apikey={accuweather_key}')
        with urllib.request.urlopen(urllib.request.Request(loc_url, headers=hdr), timeout=8) as r:
            loc = json.loads(r.read())
        loc_key = loc['Key']
        cur_url = (f'https://dataservice.accuweather.com/currentconditions/v1/{loc_key}'
                   f'?apikey={accuweather_key}&details=false')
        with urllib.request.urlopen(urllib.request.Request(cur_url, headers=hdr), timeout=8) as r:
            cur = json.loads(r.read())[0]
        temp = int(cur['Temperature']['Imperial']['Value'])
        desc = (cur.get('WeatherText') or '').lower()
        cond = ('Thunderstorm'   if 'thunder' in desc else
                'Snow'           if 'snow' in desc or 'blizzard' in desc else
                'Freezing Rain'  if 'freez' in desc or 'sleet' in desc or 'ice' in desc else
                'Fog'            if 'fog' in desc or 'mist' in desc else
                'Rain Showers'   if 'shower' in desc else
                'Rain'           if 'rain' in desc or 'drizzle' in desc else
                'Overcast'       if 'overcast' in desc else
                'Mostly Cloudy'  if 'mostly cloudy' in desc else
                'Cloudy'         if 'cloud' in desc else
                'Partly Cloudy'  if 'partly' in desc or 'mostly' in desc else
                'Clear'          if any(w in desc for w in ('clear','sunny','fair','bright')) else
                desc[:20].title() or 'Cloudy')
        return temp, cond

    def _fetch_visualcrossing():
        if not visualcross_key:
            raise RuntimeError('VISUALCROSSING_KEY not set')
        url = (f'https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline'
               f'/{lat:.4f},{lon:.4f}/today'
               f'?unitGroup=us&include=current&key={visualcross_key}&contentType=json')
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=8) as r:
            d = json.loads(r.read())
        cur   = d['currentConditions']
        temp  = int(cur['temp'])
        desc  = (cur.get('conditions') or '').lower()
        cloud = int(cur.get('cloudcover') or 0)
        if any(w in desc for w in ('thunder', 'storm')):          cond = 'Thunderstorm'
        elif any(w in desc for w in ('snow', 'blizzard')):        cond = 'Snow'
        elif any(w in desc for w in ('freez', 'sleet', 'ice')):   cond = 'Freezing Rain'
        elif any(w in desc for w in ('fog', 'mist')):             cond = 'Fog'
        elif 'drizzle' in desc:                                   cond = 'Drizzle'
        elif 'shower' in desc:                                    cond = 'Rain Showers'
        elif 'rain' in desc:                                      cond = 'Rain'
        elif cloud >= 90:                                         cond = 'Overcast'
        elif cloud >= 75:                                         cond = 'Cloudy'
        elif cloud >= 50:                                         cond = 'Mostly Cloudy'
        elif cloud >= 25:                                         cond = 'Partly Cloudy'
        else:                                                     cond = 'Clear'
        return temp, cond

    sources = [
        ('WUnderground PWS (nearest)', lambda: _fetch_wunderground_pws()),
        ('Tomorrow.io',                lambda: _fetch_tomorrow()),
        ('AccuWeather',                lambda: _fetch_accuweather()),
        ('Visual Crossing',            lambda: _fetch_visualcrossing()),
        ('NWS Observation (station)',  lambda: _fetch_nws_obs()),
        ('NWS Hourly Forecast',        lambda: _fetch_nws_forecast()),
        ('WeatherAPI.com',             lambda: _fetch_weatherapi()),
        ('OpenWeather',                lambda: _fetch_openweather()),
        ('wttr.in (aggregator)',       lambda: _fetch_wttr()),
        ('7timer.info (aggregator)',   lambda: _fetch_7timer()),
    ]

    other_results = []
    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fn): name for name, fn in sources}
        for fut, name in futures.items():
            try:
                temp, cond = fut.result(timeout=10)
                other_results.append({'name': name, 'temp': temp, 'condition': cond, 'error': None})
            except Exception as e:
                other_results.append({'name': name, 'temp': None, 'condition': None, 'error': str(e)[:60]})

    order = [s[0] for s in sources]
    other_results.sort(key=lambda r: order.index(r['name']) if r['name'] in order else 999)
    results = [archer_result] + other_results

    return jsonify({'results': results, 'lat': lat, 'lon': lon,
                    'location': location_data.get('location_name', f'{lat:.2f}, {lon:.2f}')})


@display_app.route('/weather/compare')
def weather_compare_page():
    """Side-by-side weather API comparison page."""
    from flask import Response as FR
    html = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Archer — Weather Compare</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Bebas+Neue&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;color:#fff;font-family:'Share Tech Mono',monospace;padding:20px;min-height:100vh}
h1{font-family:'Bebas Neue',sans-serif;color:#cc0000;font-size:32px;letter-spacing:8px;margin-bottom:4px}
.sub{font-size:9px;color:#333;letter-spacing:3px;margin-bottom:24px}
.ref-row{display:flex;gap:10px;margin-bottom:20px;align-items:center;flex-wrap:wrap}
.ref-label{font-size:10px;color:#555;letter-spacing:2px}
input{background:#0d0d0d;border:1px solid #222;color:#ff3333;font-family:'Share Tech Mono',monospace;
  font-size:13px;padding:7px 10px;border-radius:3px;outline:none;width:80px}
input:focus{border-color:#cc0000}
select{background:#0d0d0d;border:1px solid #222;color:#ff3333;font-family:'Share Tech Mono',monospace;
  font-size:11px;padding:7px 10px;border-radius:3px;outline:none;width:160px}
.btn{background:#1a0000;border:1px solid #cc0000;color:#cc0000;font-family:'Share Tech Mono',monospace;
  font-size:11px;letter-spacing:2px;padding:8px 16px;border-radius:3px;cursor:pointer}
.btn:active{background:#330000}
.grid{display:flex;flex-direction:column;gap:8px}
.card{background:#050505;border:1px solid #111;border-radius:4px;padding:12px 14px;
  display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:12px;transition:border-color 0.3s}
.card.best{border-color:#006622}
.card.close{border-color:#664400}
.name{font-size:10px;letter-spacing:2px;color:#666}
.vals{text-align:right}
.temp{font-size:18px;color:#fff;font-weight:bold}
.cond{font-size:9px;color:#555;letter-spacing:1px;margin-top:2px}
.diff{text-align:right;min-width:48px}
.diff-val{font-size:13px;font-weight:bold}
.diff-0{color:#00cc44}.diff-1{color:#88cc00}.diff-2{color:#ffaa00}.diff-3{color:#cc4400}.diff-big{color:#cc0000}
.err{font-size:9px;color:#330000;letter-spacing:1px}
.loading{color:#333;font-size:11px;letter-spacing:3px;padding:20px 0;animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.winner-banner{margin-top:16px;padding:10px 14px;background:#001a00;border:1px solid #006622;
  border-radius:4px;font-size:11px;color:#00cc44;letter-spacing:2px;display:none}
.corner{position:fixed;width:14px;height:14px;border-color:#1a1a1a;border-style:solid;opacity:0.5}
.corner.tl{top:10px;left:10px;border-width:1px 0 0 1px}
.corner.tr{top:10px;right:10px;border-width:1px 1px 0 0}
.corner.bl{bottom:10px;left:10px;border-width:0 0 1px 1px}
.corner.br{bottom:10px;right:10px;border-width:0 1px 1px 0}
</style></head><body>
<div class="corner tl"></div><div class="corner tr"></div>
<div class="corner bl"></div><div class="corner br"></div>

<h1>WEATHER COMPARE</h1>
<div class="sub" id="loc">LOADING LOCATION...</div>

<div class="ref-row">
  <span class="ref-label">YOUR PHONE:</span>
  <input id="ref-temp" type="number" placeholder="temp" min="0" max="130">
  <span class="ref-label">°F</span>
  <select id="ref-cond">
    <option value="">— condition —</option>
    <option>Clear</option><option>Partly Cloudy</option><option>Mostly Cloudy</option><option>Cloudy</option>
    <option>Overcast</option><option>Fog</option><option>Drizzle</option>
    <option>Rain</option><option>Rain Showers</option><option>Scattered Showers</option><option>Thunderstorm</option>
    <option>Snow</option><option>Snow Showers</option><option>Freezing Rain</option>
  </select>
  <button class="btn" onclick="load()">REFRESH</button>
</div>

<div class="grid" id="grid"><div class="loading">FETCHING ALL SOURCES...</div></div>
<div class="winner-banner" id="winner"></div>

<script>
async function load() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '<div class="loading">FETCHING ALL SOURCES...</div>';
  document.getElementById('winner').style.display = 'none';

  const resp = await fetch('/weather/compare/data');
  const data = await resp.json();
  document.getElementById('loc').textContent = (data.location || '').toUpperCase();

  const refTemp = parseFloat(document.getElementById('ref-temp').value);
  const refCond = document.getElementById('ref-cond').value.toLowerCase();

  grid.innerHTML = '';
  let bestDiff = Infinity, bestName = '', bestTempDiff = Infinity;

  data.results.forEach(r => {
    const card = document.createElement('div');
    card.className = 'card';

    if (r.error) {
      card.innerHTML = `<div class="name">${r.name}</div><div class="err">${r.error}</div><div></div>`;
    } else {
      const diff = (!isNaN(refTemp) && r.temp !== null) ? Math.abs(r.temp - refTemp) : null;
      const condMatch = refCond && r.condition ? r.condition.toLowerCase() === refCond : null;

      let diffClass = '';
      if (diff !== null) {
        diffClass = diff === 0 ? 'diff-0' : diff <= 1 ? 'diff-1' : diff <= 2 ? 'diff-2' : diff <= 3 ? 'diff-3' : 'diff-big';
        // Score = temp diff + 4°F penalty for condition mismatch (so wrong condition can't win on temp alone)
        const score = diff + (condMatch === false ? 4 : 0);
        if (score < bestDiff) { bestDiff = score; bestName = r.name; bestTempDiff = diff; }
      }

      const condColor = condMatch === true ? '#00cc44' : condMatch === false ? '#cc4444' : '#444';

      card.innerHTML = `
        <div class="name">${r.name}</div>
        <div class="vals">
          <div class="temp">${r.temp !== null ? r.temp + '°F' : '—'}</div>
          <div class="cond" style="color:${condColor}">${r.condition || '—'}</div>
        </div>
        <div class="diff">
          ${diff !== null ? `<div class="diff-val ${diffClass}">${diff === 0 ? '✓' : (diff > 0 ? '+' : '') + (r.temp - refTemp) + '°'}</div>` : '<div class="diff-val" style="color:#222">—</div>'}
        </div>`;
    }
    grid.appendChild(card);
  });

  // Highlight best match
  if (bestName && !isNaN(refTemp)) {
    [...grid.children].forEach(c => {
      const name = c.querySelector('.name')?.textContent || '';
      if (name === bestName) c.classList.add(bestTempDiff <= 1 ? 'best' : 'close');
    });
    const w = document.getElementById('winner');
    w.textContent = `CLOSEST MATCH: ${bestName.toUpperCase()} — ${bestTempDiff === 0 ? 'EXACT TEMP' : bestTempDiff + '°F OFF'}`;
    w.style.display = 'block';
  }
}

load();
</script>
</body></html>"""
    return FR(html, mimetype='text/html')


@display_app.route('/fans')
@display_app.route('/fan')
def fan_page():
    """Public fan page — injects auth context so JS knows if user is signed in."""
    import os, hashlib as _hl
    from flask import request as flask_request, Response as FR
    # Resolve auth from cookie
    user_info = None
    cookie_val = flask_request.cookies.get('archer_auth', '')
    if cookie_val:
        try:
            parts = cookie_val.split(':')
            if len(parts) == 3:
                c_tier, c_name, c_token = parts
                cookie_secret = os.environ.get('ARCHER_SECRET', 'archer2500hd')
                expected = _hl.sha256(f'{c_name}{c_tier}{cookie_secret}'.encode()).hexdigest()[:16]
                if c_token == expected:
                    user_info = {'tier': int(c_tier), 'name': c_name}
        except Exception:
            pass
    user_json = json.dumps(user_info) if user_info else 'null'
    if os.path.exists('archer_fan.html'):
        with open('archer_fan.html', 'r', encoding='utf-8') as f:
            html = f.read()
        html = html.replace('</head>', f'<script>window.ARCHER_USER={user_json};</script></head>', 1)
        return FR(html, mimetype='text/html')
    return FR('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">ARCHER FAN PAGE</body></html>', mimetype='text/html')


@display_app.route('/register')
def register_page():
    """Registration / sign-in page — linked from fan page."""
    from flask import request as flask_request
    mac = get_client_mac(flask_request)
    return registration_page(mac)


@display_app.route('/fans/ask', methods=['POST'])
def fans_ask():
    """Public read-only fan Q&A — no commands executed, no TTS, no auth required."""
    from flask import request as flask_request
    try:
        data = flask_request.get_json() or {}
        question = (data.get('question') or data.get('command') or '').strip()
        if not question:
            return jsonify({'response': 'Ask me something about Archer!'})
        response = ask_archer(question)
        return jsonify({'response': response or "I'm not sure about that one."})
    except Exception:
        return jsonify({'response': 'Give me a second.'})



# ══════════════════════════════════════════
# SIMULATOR CONTROL PANEL
# ══════════════════════════════════════════

SIM_SCENARIOS = {
    'idle':     {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 82, 'oil_temp': 195, 'coolant_temp': 190, 'battery_main': 13.8},
    'warmup':   {'rpm': 900,  'speed': 0,  'boost': 0,  'ethanol': 82, 'oil_temp': 160, 'coolant_temp': 150, 'battery_main': 14.1},
    'cruise':   {'rpm': 1800, 'speed': 55, 'boost': 2,  'ethanol': 82, 'oil_temp': 200, 'coolant_temp': 195, 'battery_main': 13.8},
    'highway':  {'rpm': 2200, 'speed': 75, 'boost': 4,  'ethanol': 82, 'oil_temp': 205, 'coolant_temp': 200, 'battery_main': 13.9},
    'wot':      {'rpm': 4500, 'speed': 90, 'boost': 18, 'ethanol': 82, 'oil_temp': 215, 'coolant_temp': 210, 'battery_main': 13.5},
    'launch':   {'rpm': 5200, 'speed': 15, 'boost': 22, 'ethanol': 82, 'oil_temp': 220, 'coolant_temp': 215, 'battery_main': 13.2},
    'cooldown': {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 82, 'oil_temp': 230, 'coolant_temp': 220, 'battery_main': 13.8},
    'warning':  {'rpm': 750,  'speed': 0,  'boost': 0,  'ethanol': 20, 'oil_temp': 235, 'coolant_temp': 225, 'battery_main': 11.8},
}

@display_app.route('/dashboard')
def dashboard_page():
    from flask import Response as FR
    if os.path.exists('archer_dashboard.html'):
        with open('archer_dashboard.html', 'r', encoding='utf-8') as f:
            return FR(f.read(), mimetype='text/html')
    return FR('<html><body style="background:#050508;color:#00e5ff;font-family:monospace;text-align:center;padding:40px">ARCHER DASHBOARD — archer_dashboard.html not found</body></html>', mimetype='text/html')

@display_app.route('/mirror')
def mirror_page():
    from flask import Response as FR
    if os.path.exists('archer_mirror.html'):
        with open('archer_mirror.html', 'r', encoding='utf-8') as f:
            return FR(f.read(), mimetype='text/html')
    return FR('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">MIRROR — archer_mirror.html not found</body></html>', mimetype='text/html')

@display_app.route('/hud')
def hud_page():
    from flask import Response as FR
    if os.path.exists('archer_hud.html'):
        with open('archer_hud.html', 'r', encoding='utf-8') as f:
            return FR(f.read(), mimetype='text/html')
    return FR('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">HUD — archer_hud.html not found</body></html>', mimetype='text/html')

@display_app.route('/simulator')
def simulator_page():
    from flask import Response as FR
    if os.path.exists('archer_simulator.html'):
        with open('archer_simulator.html', 'r', encoding='utf-8') as f:
            html = f.read()
        return FR(html, mimetype='text/html')
    return FR('<html><body style="background:#000;color:#cc0000;font-family:monospace;text-align:center;padding:40px">SIMULATOR — archer_simulator.html not found</body></html>', mimetype='text/html')

@display_app.route('/sim/set', methods=['POST'])
def sim_set():
    """Set individual truck_state values from simulator sliders.
    If oil_temp, coolant_temp, or battery_main are explicitly set, auto-disable
    sim random noise so update_awareness doesn't overwrite them every 2 s."""
    global sim_random_enabled
    from flask import request as req
    data = req.get_json() or {}
    allowed = {'rpm', 'speed', 'boost', 'ethanol', 'oil_temp', 'coolant_temp', 'battery_main', 'battery_aux', 'exhaust'}
    noise_keys = {'oil_temp', 'coolant_temp', 'battery_main'}
    updated = {}
    for key, val in data.items():
        if key in allowed and key in truck_state:
            try:
                truck_state[key] = float(val) if '.' in str(val) else int(val)
                updated[key] = truck_state[key]
            except (ValueError, TypeError):
                pass
    # If the user is manually controlling any key that random noise would overwrite,
    # turn off noise automatically so slider values stick.
    if updated.keys() & noise_keys:
        sim_random_enabled = False
    return jsonify({'ok': True, 'updated': updated, 'sim_random': sim_random_enabled})

@display_app.route('/sim/random', methods=['POST'])
def sim_random_toggle():
    """Explicitly enable or disable simulated random noise.
    Body: {"enabled": true} or {"enabled": false}"""
    global sim_random_enabled
    from flask import request as req
    data = req.get_json() or {}
    sim_random_enabled = bool(data.get('enabled', True))
    return jsonify({'ok': True, 'sim_random_enabled': sim_random_enabled})

@display_app.route('/sim/scenario', methods=['POST'])
def sim_scenario():
    """Apply a preset driving scenario to truck_state."""
    from flask import request as req
    data = req.get_json() or {}
    name = data.get('name', '').lower()
    if name not in SIM_SCENARIOS:
        return jsonify({'error': f'Unknown scenario: {name}', 'valid': list(SIM_SCENARIOS.keys())}), 400
    for key, val in SIM_SCENARIOS[name].items():
        if key in truck_state:
            truck_state[key] = val
    return jsonify({'ok': True, 'scenario': name, 'state': {k: truck_state[k] for k in SIM_SCENARIOS[name]}})

@display_app.route('/sim/status')
def sim_status():
    """Return simulator / OBD status."""
    return jsonify({
        'sim_random_enabled': sim_random_enabled,
        'obd_connected':      obd2_display['connected'],
        'obd_mode':           obd2_display['mode'],
        'rpm':          truck_state['rpm'],
        'speed':        truck_state['speed'],
        'boost':        truck_state['boost'],
        'ethanol':      truck_state['ethanol'],
        'oil_temp':     truck_state['oil_temp'],
        'coolant_temp': truck_state['coolant_temp'],
        'battery_main': truck_state['battery_main'],
    })

# ── ARDUINO SERIAL ───────────────────────────────────────
_ARDUINO_KEYWORDS = ('arduino', 'ch340', 'cp210', 'cp2102', 'ftdi', 'uno', 'mega', 'nano')

def arduino_send(cmd):
    """Send a command to the Arduino over serial. Always logs to stdout."""
    print(f'[ARDUINO] → {cmd}')
    if arduino_state['connected'] and arduino_state['conn']:
        try:
            arduino_state['conn'].write((cmd + '\n').encode('utf-8'))
        except Exception as e:
            print(f'[ARDUINO] write error: {e}')
            arduino_state['connected'] = False
            arduino_state['conn']      = None

def arduino_autodetect():
    """Scan serial ports every 5 s for an Arduino (CH340/CP210/FTDI).
    Opens connection at 9600 baud when found, closes on disconnect."""
    while True:
        try:
            import serial
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            found = False
            for p in ports:
                desc = (p.description or '').lower()
                mfr  = (p.manufacturer or '').lower()
                if any(kw in desc or kw in mfr for kw in _ARDUINO_KEYWORDS):
                    found = True
                    if not arduino_state['connected']:
                        try:
                            conn = serial.Serial(p.device, 9600, timeout=1)
                            arduino_state['connected'] = True
                            arduino_state['port']      = p.device
                            arduino_state['conn']      = conn
                            print(f'[ARDUINO] Connected on {p.device}')
                        except Exception as e:
                            print(f'[ARDUINO] connect error: {e}')
                    break
            if not found and arduino_state['connected']:
                arduino_state['connected'] = False
                arduino_state['port']      = None
                try:
                    arduino_state['conn'].close()
                except Exception:
                    pass
                arduino_state['conn'] = None
                print('[ARDUINO] Disconnected')
        except ImportError:
            pass
        except Exception as e:
            print(f'[ARDUINO] autodetect error: {e}')
        time.sleep(5)

@display_app.route('/arduino/status')
def arduino_status():
    return jsonify({
        'connected': arduino_state['connected'],
        'port':      arduino_state['port'],
    })

# ── BEAMNG TELEMETRY ─────────────────────────────────────
@display_app.route('/beamng_data', methods=['POST'])
def beamng_data():
    global sim_random_enabled
    import time as _time
    from flask import request as req
    data = req.get_json(silent=True) or {}
    if not data or data.get('source') != 'beamng':
        return jsonify({'ok': False, 'error': 'invalid payload'}), 400

    # Map BeamNG fields → truck_state
    FIELD_MAP = {
        'rpm':         'rpm',
        'speed':       'speed',
        'boost':       'boost',
        'oil_temp':    'oil_temp',
        'coolant_temp':'coolant_temp',
        'throttle':    'throttle',
        'gear':        'gear',
    }
    for src, dst in FIELD_MAP.items():
        if src in data:
            truck_state[dst] = data[src]

    beamng_state['connected'] = True
    beamng_state['last_rx']   = _time.time()
    beamng_state['car']       = data.get('car', '')
    beamng_state['packets']   = beamng_state.get('packets', 0) + 1
    sim_random_enabled        = False  # freeze sim noise while BeamNG feeds data
    return jsonify({'ok': True})

@display_app.route('/beamng/status')
def beamng_status():
    import time as _time
    alive = beamng_state['connected'] and (_time.time() - beamng_state['last_rx'] < 5)
    if not alive and beamng_state['connected']:
        beamng_state['connected'] = False
    return jsonify({
        'connected': beamng_state['connected'],
        'car':       beamng_state['car'],
        'packets':   beamng_state.get('packets', 0),
    })

# ── OBD AUTH STATUS ──────────────────────────────────────
@display_app.route('/obd_auth')
def obd_auth_status():
    """Gatekeeper / OBD auth status page — shows relay state and last auth result."""
    connected   = obd2_display.get('connected', False)
    mode        = obd2_display.get('mode', 'default')
    last_update = system_health.get('last_obd_update', 0)
    age         = round(time.time() - last_update, 1) if last_update else None

    # Query gatekeeper systemd unit status if on Pi
    gk_status = 'n/a'
    if _IS_PI:
        try:
            import subprocess as _sp
            r = _sp.run(['systemctl', 'is-active', 'obd_gatekeeper'],
                        capture_output=True, text=True, timeout=3)
            gk_status = r.stdout.strip()
        except Exception:
            gk_status = 'unknown'

    return jsonify({
        'obd_connected':      connected,
        'obd_mode':           mode,
        'last_update_age_s':  age,
        'gatekeeper_service': gk_status,
        'relay_unlocked':     connected and mode == 'live',
        'auth_key_path':      '/etc/archer/obd_auth.key',
    })

# ── ARCHER OS STATUS ──────────────────────────────────────
@display_app.route('/archer_os')
def archer_os_status():
    """Archer OS / USB-OS status — shows connection state and build info."""
    usb_connected = False
    usb_info      = {}
    if _IS_PI:
        try:
            import subprocess as _sp
            # Check if the USB OS client is active (obd_auth_client connects over serial)
            r = _sp.run(['systemctl', 'is-active', 'obd_gatekeeper'],
                        capture_output=True, text=True, timeout=3)
            usb_connected = r.stdout.strip() == 'active'
        except Exception:
            pass

    return jsonify({
        'archer_os_connected': usb_connected,
        'platform':            'raspberry_pi' if _IS_PI else 'cloud',
        'obd_authenticated':   obd2_display.get('connected', False) and obd2_display.get('mode') == 'live',
        'auth_client':         '/archer-os/obd-auth/obd_auth_client.py',
        'key_gen':             '/archer-os/obd-auth/keygen.sh',
        'build_script':        '/archer-os/build.sh',
    })

# ── OBD AUTO-DETECT ──────────────────────────────────────
def _obd_cmd(ser, cmd, timeout=2.0):
    """Send an ELM327 AT or OBD command; return the response string."""
    ser.reset_input_buffer()
    ser.write((cmd + '\r').encode())
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk
            if b'>' in buf:
                break
        else:
            time.sleep(0.02)
    return buf.decode(errors='ignore').strip()

def _obd_bytes(raw):
    """Extract data bytes from an ELM327 response line (strips '41 XX' header)."""
    for line in raw.splitlines():
        parts = line.strip().split()
        try:
            idx = next(i for i, p in enumerate(parts) if p.upper() == '41')
            return [int(x, 16) for x in parts[idx + 2:]]
        except (StopIteration, IndexError, ValueError):
            continue
    return []

def obd_autodetect():
    """Detect an ELM327/OBDLink adapter, initialize it, and poll live PIDs.
    Updates truck_state and sensor_data directly; falls back to sim on disconnect."""
    global sim_random_enabled
    OBD_KEYWORDS = ('obdlink', 'obd', 'elm327', 'stm32', 'stn', 'scantool')

    while True:
        # ── Scan for adapter ──────────────────────────────────
        port_device = None
        try:
            import serial
            import serial.tools.list_ports
            for p in serial.tools.list_ports.comports():
                desc = (p.description or '').lower()
                mfr  = (p.manufacturer or '').lower()
                if any(kw in desc or kw in mfr for kw in OBD_KEYWORDS):
                    port_device = p.device
                    break
        except ImportError:
            time.sleep(10)
            continue
        except Exception as e:
            print(f'[OBD] scan error: {e}')
            time.sleep(5)
            continue

        if not port_device:
            time.sleep(5)
            continue

        # ── Connect and initialize ─────────────────────────────
        ser = None
        try:
            import serial
            ser = serial.Serial(port_device, 38400, timeout=2)
            _obd_cmd(ser, 'ATZ');   time.sleep(1.0)   # reset adapter
            _obd_cmd(ser, 'ATE0')                      # echo off
            _obd_cmd(ser, 'ATH0')                      # headers off
            _obd_cmd(ser, 'ATSP0')                     # auto-select protocol
            _obd_cmd(ser, 'ATAT1')                     # adaptive timing mode 1

            obd2_display['connected'] = True
            obd2_display['mode']      = 'live'
            sim_random_enabled        = False
            print(f'[OBD] Connected on {port_device} — live data active')

            # ── OBD sensor bounds validation ───────────────────
            def _validate_obd(value, lo, hi, name):
                """Return value if within bounds, else log and return None."""
                if value is None:
                    return None
                if value < lo or value > hi:
                    print(f'[OBD] Sensor error: {name}={value} out of bounds [{lo},{hi}] — rejected')
                    return None
                return value

            # ── Adaptive PID registry ──────────────────────────
            # Each entry: (cmd, name, parser_fn, validator args)
            # parser_fn(raw_bytes) -> float|None
            def _parse_rpm(b):
                return ((b[0] * 256) + b[1]) / 4 if len(b) >= 2 else None
            def _parse_speed(b):
                return round(b[0] * 0.621371) if b else None
            def _parse_coolant(b):
                return round((b[0] - 40) * 9 / 5 + 32) if b else None
            def _parse_throttle(b):
                return round(b[0] * 100 / 255) if b else None
            def _parse_map(b):
                return round((b[0] - 101.325) * 0.145038, 1) if b else None
            def _parse_oil_gm(b):
                return round((b[0] - 40) * 9 / 5 + 32) if b else None

            # PID table: (cmd, name, lo, hi, truck_state_key, sensor_data_key, parser, extra_bytes)
            PID_TABLE = [
                ('010C', 'RPM',       0,    8000, 'rpm',          None,           _parse_rpm,      None),
                ('010D', 'speed',     0,    200,  'speed',        None,           _parse_speed,    None),
                ('0105', 'coolant',   -40,  300,  'coolant_temp', 'coolant_temp', _parse_coolant,  None),
                ('0111', 'throttle',  0,    100,  'throttle',     None,           _parse_throttle, None),
                ('010B', 'boost',     -15,  30,   'boost',        None,           _parse_map,      None),
                ('2201318','oil_gm',  -40,  350,  'oil_temp',     'oil_temp',     _parse_oil_gm,   None),
            ]

            # Adaptive timing state per PID
            _pid_stats = {
                cmd: {'avg_ms': 300.0, 'failures': 0, 'blacklisted': False, 'blacklisted_at': 0.0}
                for cmd, *_ in PID_TABLE
            }
            _BLACKLIST_FAILURES = 3        # failures before disabling
            _BLACKLIST_RETEST_S = 300.0    # 5 minutes before retesting

            def _pid_order():
                """Return PIDs sorted fastest-first, excluding currently-blacklisted ones."""
                now = time.time()
                active = []
                for row in PID_TABLE:
                    cmd = row[0]
                    st  = _pid_stats[cmd]
                    if st['blacklisted']:
                        # Re-enable after retest period
                        if now - st['blacklisted_at'] >= _BLACKLIST_RETEST_S:
                            st['blacklisted'] = False
                            st['failures']    = 0
                            print(f'[OBD] Re-testing previously blacklisted PID {cmd}')
                        else:
                            continue  # still blacklisted
                    active.append(row)
                # Sort by average response time (fastest first)
                return sorted(active, key=lambda r: _pid_stats[r[0]]['avg_ms'])

            def _obd_cmd_timed(ser, cmd):
                """Run OBD command, record response time, update PID stats."""
                t0  = time.time()
                raw = _obd_cmd(ser, cmd, timeout=1.5)
                ms  = (time.time() - t0) * 1000
                st  = _pid_stats[cmd]
                has_data = bool(_obd_bytes(raw)) or (cmd == 'ATRV' and 'V' in raw)
                if has_data:
                    # Exponential moving average of response time
                    st['avg_ms']  = st['avg_ms'] * 0.85 + ms * 0.15
                    st['failures'] = 0
                else:
                    st['failures'] += 1
                    if st['failures'] >= _BLACKLIST_FAILURES:
                        st['blacklisted']    = True
                        st['blacklisted_at'] = time.time()
                        print(f'[OBD] PID {cmd} blacklisted after {_BLACKLIST_FAILURES} failures (avg {st["avg_ms"]:.0f}ms)')
                return raw

            # ── Poll loop (adaptive) ───────────────────────────
            while True:
                try:
                    for row in _pid_order():
                        cmd, name, lo, hi, ts_key, sd_key, parser, _ = row
                        raw = _obd_cmd_timed(ser, cmd)
                        b   = _obd_bytes(raw)
                        val = parser(b)
                        val = _validate_obd(val, lo, hi, name)
                        if val is not None:
                            truck_state[ts_key] = val
                            if sd_key:
                                sensor_data[sd_key] = val

                    # Battery voltage — AT command outside PID table (no bytes to parse)
                    raw_v = _obd_cmd(ser, 'ATRV')
                    try:
                        raw_bat = round(float(raw_v.replace('V', '').strip()), 1)
                        val = _validate_obd(raw_bat, 0, 20, 'battery_v')
                        if val is not None:
                            truck_state['battery_main'] = val
                            sensor_data['battery_v']    = val
                    except ValueError:
                        pass

                    system_health['last_obd_update'] = time.time()

                except Exception as e:
                    print(f'[OBD] read error: {e}')
                    break

                time.sleep(0.15)

        except Exception as e:
            print(f'[OBD] connection error on {port_device}: {e}')
        finally:
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            obd2_display['connected'] = False
            obd2_display['mode']      = 'default'
            sim_random_enabled        = True
            print('[OBD] Disconnected — simulation resumed')

        time.sleep(5)

def run_display_server():
    import logging as _log
    _log.getLogger('werkzeug').setLevel(_log.ERROR)
    port = int(os.environ.get('PORT', 7860))
    display_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)

def run_tier_server(tier, port):
    pass  # Tier servers disabled on HuggingFace

def _guarded(fn, name, restart_delay=5):
    """Wrap a thread target with crash recovery — restarts on any exception."""
    def wrapper():
        while True:
            try:
                fn()
            except Exception as e:
                print(f'[THREAD] {name} crashed: {e} — restarting in {restart_delay}s')
                time.sleep(restart_delay)
    return wrapper

# ── MAIN ─────────────────────────────────────────────────
def main():
    # Critical safety/monitoring threads get crash recovery
    _guarded_threads = [
        (safety_monitor,          'safety_monitor'),
        (update_awareness,        'update_awareness'),
        (weather_monitor,         'weather_monitor'),
        (casual_monitor,          'casual_monitor'),
        (record_spikes,           'record_spikes'),
        (valet_monitor,           'valet_monitor'),
        (curfew_monitor,          'curfew_monitor'),
        (weather_alert_monitor,   'weather_alert_monitor'),
        (live_data_loop,          'live_data_loop'),
        (client_timeout_monitor,  'client_timeout_monitor'),
    ]
    for fn, name in _guarded_threads:
        threading.Thread(target=_guarded(fn, name), daemon=True).start()

    threading.Thread(target=tts_worker,          daemon=True).start()
    threading.Thread(target=voice_monitor,       daemon=True).start()
    threading.Thread(target=run_display_server,  daemon=True).start()
    threading.Thread(target=discord_monitor,     daemon=True).start()
    threading.Thread(target=openclaw_monitor,    daemon=True).start()
    if _IS_PI:
        threading.Thread(target=fetch_ngrok_url, daemon=True).start()
    threading.Thread(target=obd_autodetect,      daemon=True).start()
    threading.Thread(target=arduino_autodetect,  daemon=True).start()

    archer_memory['total_sessions'] += 1
    if not archer_memory['first_drive']:
        archer_memory['first_drive'] = datetime.now().strftime('%B %d %Y')

    load_state()
    time.sleep(0.5)
    greeting = get_daily_greeting()
    print(f"[ARCHER] {greeting}")
    speak(greeting)

    # On HuggingFace / non-interactive environments stdin is not a TTY.
    # Skip the local text input loop entirely — all interaction is via the web UI.
    if not sys.stdin.isatty():
        print("[ARCHER] No TTY detected — web-only mode. Text input disabled.")
        while True:
            time.sleep(60)

    while True:
        try:
            try:
                user_input = input("[YOU] ").strip()
            except EOFError:
                time.sleep(1)
                continue
            if not user_input:
                continue
            response = handle_command(user_input)
            if response is None:
                response = ask_archer(user_input)
            if response:
                print(f"[ARCHER] {response}")
                speak(response)
                last_archer_msg['text'] = response
        except KeyboardInterrupt:
            print("\n[ARCHER] See you tomorrow.")
            save_state()
            break

if __name__ == "__main__":
    main()
