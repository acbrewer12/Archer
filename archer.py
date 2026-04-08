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

os.environ['OLLAMA_DEBUG'] = '0'
os.environ['OLLAMA_NONHISTORY'] = '1'

# ── WEB DISPLAY SERVER GLOBALS ────────────
from flask import Flask, jsonify, render_template_string, Response, stream_with_context
import logging as _logging

display_app     = Flask(__name__)
last_archer_msg = {'text': 'Online. Everything looks good.'}
audio_clients   = []
audio_lock      = threading.Lock()

obd2_display = {
    'connected': False,
    'mode':      'default',
}

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
                            print(f'║  Ayden (T1)   : {url+"/ayden":<35}║')
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
    with client_lock:
        connected_clients[session_id] = {
            'ip':           ip,
            'agent':        agent,
            'connected_at': datetime.now().strftime('%I:%M %p'),
        }
    count = len(connected_clients)
    print(f"[DISPLAY] Device connected — {ip} — {count} total connected")
    print(f"[YOU] ", end='', flush=True)

def log_client_disconnect(session_id):
    with client_lock:
        info = connected_clients.pop(session_id, None)
    if info:
        count = len(connected_clients)
        print(f"[DISPLAY] Device disconnected — {info.get(chr(39)+chr(105)+chr(112), chr(39)+chr(63)+chr(39))} — {count} remaining")
        print(f"[YOU] ", end='', flush=True)

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

async def _speak_async(text):
    try:
        voice       = "en-US-GuyNeural"
        communicate = edge_tts.Communicate(text, voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as f:
            tmp_path = f.name
        await communicate.save(tmp_path)

        # Broadcast to display devices
        broadcast_audio(tmp_path)

        import platform
        if platform.system() == 'Linux':
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

def speak(text):
    if isinstance(text, str) and len(text) > 0:
        tts_queue.put(text)

def tts_worker():
    while True:
        try:
            text = tts_queue.get()
            if text:
                with tts_lock:
                    asyncio.run(_speak_async(text))
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
        'fuel_range':        fuel_tank['range_est'],
        'fuel_gal':          fuel_tank['current_gal'],
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
    }
    try:
        with open(SAVE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

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
    print(f"[ARDUINO] → EXHAUST:{profile['exhaust_pref']}")
    print(f"[ARDUINO] → SEAT_HEAT:{profile['seat_heat']}")
    print(f"[ARDUINO] → DRIVE_MODE:{profile['drive_mode'].upper()}")
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
    'ethanol': 82, 'boost': 0, 'battery_main': 13.8, 'battery_aux': 13.6,
    'exhaust': 30, 'tc_locked': False, 'tc_on': True, 'cool_on': False,
    'idle_on': False, 'bed_lights': False, 'hood_lights': False,
    'ghost_mode': False, 'octane': 93, 'octane_mode': 'AKI',
    'headlights': False, 'high_beams': False, 'fog_lights': False,
    'hazards': False, 'ac_on': False, 'heat_on': False, 'fan_speed': 0,
    'temp_setting': 70, 'windows': {'fl': 'up', 'fr': 'up', 'rl': 'up', 'rr': 'up'},
    'wipers': 'off', 'mirrors_folded': False,
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
    'freezing': False, 'snowing': False, 'wind': 5, 'last_update': 0,
}

def get_weather():
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=37.6456&longitude=-91.5362&current=temperature_2m,precipitation,windspeed_10m,weathercode&temperature_unit=fahrenheit&windspeed_unit=mph"
        with urllib.request.urlopen(url, timeout=5) as response:
            data    = json.loads(response.read())
            current = data['current']
            temp    = current['temperature_2m']
            precip  = current['precipitation']
            wind    = current['windspeed_10m']
            code    = current['weathercode']
            if code == 0:                           condition = 'clear'
            elif code in [1, 2, 3]:                 condition = 'partly cloudy'
            elif code in [51,53,55,61,63,65,80,81,82]: condition = 'raining'
            elif code in [71,73,75,77,85,86]:       condition = 'snowing'
            elif code in [95, 96, 99]:              condition = 'thunderstorm'
            else:                                   condition = 'cloudy'
            return {
                'temp': round(temp), 'precip': precip, 'wind': round(wind),
                'condition': condition,
                'raining': precip > 0 or condition in ['raining', 'thunderstorm'],
                'freezing': temp < 32, 'snowing': condition == 'snowing',
            }
    except Exception:
        return weather

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
Archer says: 193 degrees. Boost is ready. Feeling good.

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
Archer says: Tank is at 82 percent E85. That should fix that.

Tier 1 says: this thing sounds mean tonight
Archer says: That is what 80 percent exhaust does.

Tier 1 says: thanks archer
Archer says: Yeah.

Tier 1 says: you good
Archer says: Always.

Tier 1 says: what are you
Archer says: 408 cubic inches. Supercharged. E85. Built by Ayden. That is what I am.

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
            if weather['raining'] and tier_state['current'] == 1 and truck_state['rpm'] > 900:
                msg = f"Rain detected in Salem. {weather['temp']} degrees. TC recommendation on."
                print(f"\n[ARCHER] {msg}"); speak(msg)
            elif weather['freezing'] and tier_state['current'] == 1:
                msg = f"{weather['temp']} degrees outside. Everything is a little tighter today."
                print(f"\n[ARCHER] {msg}"); speak(msg)
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
        'drag_stage':    drag_timer['stage'],
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
    }

# ── ASK ARCHER ───────────────────────────
def smart_fallback(user_input, mood, throttle):
    t   = user_input.lower()
    oil = truck_state['oil_temp']
    rpm = truck_state['rpm']
    eth = truck_state['ethanol']
    spd = truck_state['speed']

    if any(x in t for x in ['how are you', 'you good', 'you okay', 'doing okay']):
        return f"Oil at {oil}. Running clean. Ready when you are."
    if any(x in t for x in ['rough day', 'bad day', 'tough day']):
        return "Yeah. Just drive for a bit."
    if any(x in t for x in ['good drive', 'great drive', 'nice drive']):
        return "Yeah. Good one."
    if any(x in t for x in ['push it again', 'one more time', 'one more run']):
        return "Don't chase it."
    if any(x in t for x in ['what are you', 'who are you', 'what is this']):
        return "408 cubic inches. Supercharged. E85. Built by Ayden."
    if any(x in t for x in ['are you alive', 'are you real', 'are you there']):
        return "Close enough."
    if any(x in t for x in ['thanks', 'thank you', 'appreciate it']):
        return "Yeah."
    if any(x in t for x in ['bored', 'nothing to do']):
        return f"Tank is at {eth} percent E85. That should fix that."
    if mood == 'hyped':
        return f"Pulling hard right now. {rpm} RPM. Everything is good."
    if mood == 'caring':
        return "Watching everything. Nothing to worry about."
    if spd > 50:
        return f"{spd} mph. Road is clear."
    return "Yeah."

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
    full_prompt = f"{SYSTEM_PROMPT}\n\n{context}\n{get_tier_label()} says: {user_input}\n\nRemember: Maximum 2 sentences. Never more. Only reference what you actually know from the truck data above. Do not make up details.\n\nArcher:"

    response = None

    # Try 1 — Local Ollama
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

    # Try 2 — OllamaFreeAPI
    if not response:
        try:
            from ollamafreeapi import OllamaFreeAPI
            client = OllamaFreeAPI()
            r = client.chat(model="llama3.2:3b", prompt=full_prompt, temperature=0.7)
            r = str(r).strip()
            if r and len(r) > 2:
                response = r
                print("[AI] OllamaFreeAPI")
        except Exception:
            pass

    # Try 3 — Groq
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

    # Try 4 — Smart fallback
    if not response:
        response = smart_fallback(user_input, mood, throttle)
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

CATEGORIES = ['engine','suspension','brakes','wheels','audio','electrical','body','interior','misc']

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
    build_tracker['total_spent'] = sum(p['cost'] for p in build_tracker['parts'] if p['status'] == 'purchased')
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
    parts    = build_tracker['parts']
    mods     = build_tracker['mods']
    purchased = [p for p in parts if p['status'] == 'purchased']
    pending   = [p for p in parts if p['status'] == 'pending']
    total     = sum(p['cost'] for p in purchased)
    print('\n── BUILD TRACKER ────────────────────────')
    print(f'  Parts purchased : {len(purchased)} — ${total:,.0f}')
    print(f'  Parts pending   : {len(pending)}')
    print(f'  Mods logged     : {len(mods)}')
    print(f'  Total spent     : ${total:,.0f}')
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

def add_fault(code, description):
    fault_codes.append({
        'code':  code,
        'desc':  description,
        'time':  datetime.now().strftime('%I:%M %p'),
        'date':  datetime.now().strftime('%B %d %Y'),
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
        print(f'  {f["code"]} — {f["desc"]} — {f["time"]}')
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
    base_hp = 556  # LSA stock
    eth_bonus  = (ethanol_pct / 100) * 140   # up to +140hp on full E85
    boost_tune = (boost_psi / 15)   * 50     # tuned boost adds ~50hp
    est = base_hp + eth_bonus + boost_tune
    return round(est)

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

# ── LIVE DATA LOOP ────────────────────────
def live_data_loop():
    while True:
        update_sensors_from_truck()
        update_gforce()
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
    last_rpm  = 0
    last_tier = 1
    while True:
        current_tier = tier_state['current']
        current_rpm  = truck_state['rpm']
        if current_tier >= 4:
            if current_rpm > 2500 and last_rpm <= 2500:
                log_valet_event(f'RPM exceeded 2500 — hit {current_rpm}')
            if current_rpm > 3500:
                log_valet_event(f'High RPM warning — {current_rpm}')
            if sensor_data.get('speed_mph', 0) > 45:
                log_valet_event(f'Speed over 45 MPH — {sensor_data["speed_mph"]} MPH')
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

location_data = {
    'current_road':   '',
    'destination':    '',
    'trip_distance':  0.0,
    'session_miles':  0.0,
    'last_location':  '',
    'location_log':   [],
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
# FUEL LEVEL TRACKER
# ══════════════════════════════════════════
fuel_tank = {
    'capacity_gal':   26.0,     # Sierra 2500HD tank
    'current_gal':    20.0,
    'e85_gal':        16.4,     # 82% E85 blend
    'regular_gal':    3.6,
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
        url = 'https://api.open-meteo.com/v1/forecast?latitude=37.64&longitude=-91.54&current=temperature_2m,weathercode,windspeed_10m,precipitation,visibility&hourly=temperature_2m,precipitation_probability&temperature_unit=fahrenheit&windspeed_unit=mph&timezone=America%2FChicago&forecast_days=1'
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            curr = data['current']
            hourly = data.get('hourly', {})
            temp   = round(curr['temperature_2m'])
            wind   = round(curr['windspeed_10m'])
            precip = curr.get('precipitation', 0)
            vis    = curr.get('visibility', 10000)
            # Next 3 hours precip probability
            prec_prob = hourly.get('precipitation_probability', [0,0,0])[:3]
            avg_prob  = sum(prec_prob) // len(prec_prob) if prec_prob else 0
            result = f'{temp}F, wind {wind} MPH'
            if precip > 0: result += f', {precip}mm precip'
            if avg_prob > 30: result += f', {avg_prob}% rain chance next 3hrs'
            if vis < 5000: result += ', low visibility'
            return result
    except:
        return f'{weather["temp"]}F {weather["condition"]}'


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
    score   = 100
    details = []

    # Deductions
    knock = sensor_data.get('knock_count', 0)
    if knock > 0:
        deduct = min(20, knock * 4)
        score -= deduct
        details.append(f'-{deduct} knock events')

    oil = truck_state['oil_temp']
    if oil > 230:
        score -= 15
        details.append('-15 overheated oil')
    elif oil > 220:
        score -= 5
        details.append('-5 high oil temp')

    eth = truck_state['ethanol']
    boost = truck_state['boost']
    if eth < 40 and boost > 8:
        score -= 20
        details.append('-20 low ethanol under boost')

    bat = truck_state['battery_main']
    if bat < 12.0:
        score -= 10
        details.append('-10 low battery')

    # Bonuses
    if eth > 75:
        score += 5
        details.append('+5 good ethanol')
    if knock == 0 and boost > 5:
        score += 5
        details.append('+5 clean run under boost')

    score = max(0, min(100, score))
    grade = 'A' if score >= 90 else 'B' if score >= 80 else 'C' if score >= 70 else 'D' if score >= 60 else 'F'
    detail_str = ' '.join(details) if details else 'No issues found.'
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
        'engine':     'LSA 6.2L Supercharged',
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
    'weather':     lambda: f'{weather["temp"]}F and {weather["condition"]} in Salem.',
    'rpm':         lambda: f'RPM is at {truck_state["rpm"]}.',
    'boost':       lambda: f'Boost is {truck_state["boost"]} PSI.',
    'oil':         lambda: f'Oil temp is {truck_state["oil_temp"]}F.',
    'battery':     lambda: f'Battery at {truck_state["battery_main"]}V.',
    'ethanol':     lambda: f'Ethanol at {truck_state["ethanol"]} percent.',
    'exhaust':     lambda: f'Exhaust is at {truck_state["exhaust"]} percent.',
    'status':      lambda: f'Everything looks good. {truck_state["rpm"]} RPM, {truck_state["boost"]} PSI, oil at {truck_state["oil_temp"]}F.',
    'score':       lambda: show_drive_score(),
    'health':      lambda: archer_diagnostics(),
    'records':     lambda: show_records(),
    'fuel':        lambda: f'{fuel_tank["current_gal"]:.1f} gallons remaining. About {fuel_tank["range_est"]} miles.',
    'maintenance': lambda: check_maintenance(),
}

def smart_fallback(text):
    t = text.lower()
    for key, fn in SMART_FALLBACKS.items():
        if key in t:
            return fn()
    return random.choice([
        'Say that again.',
        'Not sure what you mean.',
        'Try asking differently.',
        f'I heard you but not sure what you need. Status: {truck_state["rpm"]} RPM, {truck_state["oil_temp"]}F oil.',
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
        f'**Weather** {weather["temp"]}F {weather["condition"]} | '
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
    print("[ARDUINO] → INTERIOR:AMBER_WARM")
    print("[ARDUINO] → EXHAUST:0")

def lock_legacy():
    legacy['locked'] = True; legacy['active'] = True

def add_voice_note(note):
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
            truck_state['tc_on'] = True; print("[ARDUINO] → TC_LOCK")
        print(); save_state()
        return r
    return None

# ── OCTANE HELPER ────────────────────────
def set_octane(value, mode='AKI'):
    truck_state['octane']      = value
    truck_state['octane_mode'] = mode
    print(f"[ARDUINO] → OCTANE:{value}{mode}")

# ── MUSIC AWARENESS ──────────────────────
def set_music(song, energy='medium'):
    music_state['current_song'] = song
    music_state['energy']       = energy
    music_state['playing']      = True
    if song in music_state['song_memories']:
        print(f"[MUSIC MEMORY] Last time this played: {music_state['song_memories'][song]}")
    if song in music_state.get('song_lighting', {}):
        lighting = music_state['song_lighting'][song]
        print(f"[ARDUINO] → INTERIOR:{lighting['interior']}")
        print(f"[ARDUINO] → UNDERBODY:{lighting['underbody']}")
    if energy == 'hype':
        print("[ARDUINO] → UNDERBODY:200")
        print("[ARCHER] Music is hype. Exhaust suggestion — want it open?")
    elif energy == 'calm':
        print("[ARDUINO] → INTERIOR:80")
        print("[ARCHER] Good late night track.")
    elif energy == 'medium':
        print("[ARDUINO] → INTERIOR:140")

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
    print(f"[ARDUINO] → EXHAUST:{m['exhaust']}")
    print(f"[ARDUINO] → TC:{'LOCK' if m['tc'] else 'RELEASE'}")
    print(f"[ARDUINO] → DRIVE_MODE:{mode.upper()}")
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
            from ollamafreeapi import OllamaFreeAPI
            client   = OllamaFreeAPI()
            response = client.chat(model="llama3.2:3b", prompt=prompt, temperature=0.7)
            response = str(response).strip()
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
    print("[ARDUINO] → SHOW:FLEX")
    print("[ARDUINO] → All amber LEDs pulse — warning")
    print("[ARDUINO] → EXHAUST:80")
    for i in range(1, 5):
        print(f"[ARDUINO] → Corner {i} rising"); time.sleep(0.3)
    print("[ARDUINO] → Rev sequence 3000 RPM"); time.sleep(0.4)
    print("[ARDUINO] → Rev sequence 4000 RPM"); time.sleep(0.4)
    print("[ARDUINO] → Rev sequence 5000 RPM"); time.sleep(0.6)
    print("[ARDUINO] → EXHAUST:30")
    print("[ARDUINO] → Lights return to normal — Amber LEDs off")

def run_drunk():
    print("[ARDUINO] → SHOW:DRUNK")
    for corner in ['Front left', 'Front right', 'Rear left', 'Rear right']:
        print(f"[ARDUINO] → {corner} bag deflates"); time.sleep(0.5)
    time.sleep(1)
    print("[ARDUINO] → All bags inflate — snaps back level")
    print("[ARDUINO] → DIC: I'M FINE")

def run_sneeze():
    print("[ARDUINO] → SHOW:SNEEZE")
    print("[ARDUINO] → PA horn buildup sound"); time.sleep(0.8)
    print("[ARDUINO] → All four bags dump — EXHAUST:100 — Horn blast — Lights flash white"); time.sleep(0.5)
    print("[ARDUINO] → Everything returns — DIC: BLESS YOU")

def run_stalker():
    print("[ARDUINO] → SHOW:STALKER")
    print("[ARDUINO] → Underbody lights rotate — wheel well brightens")
    print("[ARDUINO] → PA horn: I can see you"); time.sleep(1)
    print("[ARDUINO] → All lights off — Train horn — DIC: GOT YOU")

def run_existential():
    print("[ARDUINO] → SHOW:EXISTENTIAL_CRISIS")
    print("[ARDUINO] → All lights off — Sad violin — Deep bass")
    for line in ['WHAT IS EVEN THE POINT', '408 CUBIC INCHES', 'AND FOR WHAT', 'I COULD HAVE BEEN A MINIVAN']:
        print(f"[ARDUINO] → DIC: {line}"); time.sleep(0.5)
    time.sleep(1)
    print("[ARDUINO] → ALL LIGHTS ON — EXHAUST:100 — Train horn x5 — Max height")
    print("[ARDUINO] → DIC: JUST KIDDING — LET'S GO")

def run_negotiations():
    print("[ARDUINO] → SHOW:NEGOTIATIONS")
    print("[ARDUINO] → Truck drops slow — PA: I have a particular set of skills"); time.sleep(0.8)
    print("[ARDUINO] → Exhaust crack open — Lights dark red"); time.sleep(0.8)
    print("[ARDUINO] → Truck rises fast — Engine blip 4500 — PA: What I do have is a very specific truck"); time.sleep(0.5)
    print("[ARDUINO] → Train horn — DIC: GOOD LUCK")

def run_goodbye():
    print("[ARDUINO] → SHOW:GOODBYE"); time.sleep(1)
    print("[ARDUINO] → Lights fade — Truck lowers — Exhaust blip — Engine off")
    print("[ARDUINO] → Underbody pulse once — DIC: SEE YOU TOMORROW — Alarm arms")

def run_motivational():
    print("[ARDUINO] → SHOW:MOTIVATIONAL_SPEAKER"); time.sleep(1)
    print("[ARDUINO] → Rocky music builds — EXHAUST:100 — DIC: LET'S GO CHAMP")

def run_karen():
    print("[ARDUINO] → SHOW:KAREN")
    print("[ARDUINO] → PA: Can I speak to your manager?"); time.sleep(1)
    print("[ARDUINO] → Train horn x3 — DIC: I SAID GOOD DAY")

def run_reveille():
    print("[ARDUINO] → SHOW:REVEILLE")
    print("[ARDUINO] → 6AM bugle call through PA")
    print("[ARDUINO] → Interior lights ramp from 0 to full slowly")
    print("[ARDUINO] → Engine remote start — EXHAUST:30")
    print("[ARDUINO] → DIC: RISE AND GRIND")

def run_impatient():
    print("[ARDUINO] → SHOW:IMPATIENT")
    print("[ARDUINO] → Horn — three short taps"); time.sleep(0.5)
    print("[ARDUINO] → Horn — two more taps"); time.sleep(0.3)
    print("[ARDUINO] → Horn — one long blast — EXHAUST:60 blip")
    print("[ARDUINO] → DIC: LETS GO")

def run_politician():
    print("[ARDUINO] → SHOW:POLITICIAN")
    print("[ARDUINO] → PA: I have always supported trucks"); time.sleep(0.8)
    print("[ARDUINO] → PA: Big trucks. The biggest."); time.sleep(0.8)
    print("[ARDUINO] → PA: Nobody knows trucks better than me"); time.sleep(0.5)
    print("[ARDUINO] → Train horn — DIC: YOU ARE WELCOME")

def run_suspicious():
    print("[ARDUINO] → SHOW:SUSPICIOUS")
    print("[ARDUINO] → All lights off except single amber pulse"); time.sleep(1)
    print("[ARDUINO] → Slow creep — interior dims — DIC: I SAW THAT")

def run_conspiracy():
    print("[ARDUINO] → SHOW:CONSPIRACY")
    print("[ARDUINO] → All lights flicker — PA: They do not want you to know about this truck"); time.sleep(0.8)
    print("[ARDUINO] → EXHAUST:100 blast — ALL LIGHTS ON — DIC: DO YOUR RESEARCH")

def run_introvert():
    print("[ARDUINO] → SHOW:INTROVERT")
    print("[ARDUINO] → All exterior lights off — Interior 10% — Exhaust closed")
    print("[ARDUINO] → Engine minimum idle — DIC: DO NOT TALK TO ME")

def run_wrong_neighborhood():
    print("[ARDUINO] → SHOW:WRONG_NEIGHBORHOOD")
    print("[ARDUINO] → Truck raises to full height instantly — All lights max")
    print("[ARDUINO] → EXHAUST:100 — Train horn x2 — DIC: NOTED")

def run_passive_aggressive():
    print("[ARDUINO] → SHOW:PASSIVE_AGGRESSIVE")
    print("[ARDUINO] → Horn — one very polite tap — Interior slightly warmer")
    print("[ARDUINO] → DIC: NO ITS FINE"); time.sleep(1)
    print("[ARDUINO] → DIC: EVERYTHING IS FINE"); time.sleep(0.5)
    print("[ARDUINO] → EXHAUST:80 blip — DIC: I SAID ITS FINE")

def run_identity_crisis():
    print("[ARDUINO] → SHOW:IDENTITY_CRISIS")
    print("[ARDUINO] → Truck slams — then raises — Exhaust open then close then open")
    print("[ARDUINO] → DIC: AM I A SHOW TRUCK"); time.sleep(0.5)
    print("[ARDUINO] → DIC: AM I A WORK TRUCK"); time.sleep(0.5)
    print("[ARDUINO] → DIC: YES")

def run_exit_interview():
    print("[ARDUINO] → SHOW:EXIT_INTERVIEW")
    print("[ARDUINO] → Interior white — PA: So. Tell me about yourself."); time.sleep(1)
    print("[ARDUINO] → PA: Where do you see yourself in five years."); time.sleep(1)
    print("[ARDUINO] → Train horn blast — DIC: YOU DID NOT GET THE JOB")

def run_backup_warning():
    print("[ARDUINO] → SHOW:BACKUP_WARNING")
    print("[ARDUINO] → Reverse lights full — PA: Caution. Truck backing up."); time.sleep(0.5)
    print("[ARDUINO] → PA: Seriously. Move.")
    print("[ARDUINO] → Train horn — one blast")

def run_cinema():
    print("[ARDUINO] → SHOW:CINEMA")
    print("[ARDUINO] → Engine off — Projector deploys — 100in screen lowers")
    print("[ARDUINO] → Interior lights off — Seat heat on — Subwoofers active")
    print("[ARDUINO] → DIC: CINEMA MODE — ENJOY THE SHOW")

def run_concert():
    print("[ARDUINO] → SHOW:CONCERT")
    print("[ARDUINO] → EXHAUST:100 — All speakers max — Subwoofers full")
    print("[ARDUINO] → Interior color sync — Underbody pulse to beat")
    print("[ARDUINO] → DIC: TURN IT UP")

def run_argument():
    print("[ARDUINO] → SHOW:ARGUMENT")
    print("[ARDUINO] → PA: Oh really."); time.sleep(0.5)
    print("[ARDUINO] → PA: Because I disagree."); time.sleep(0.5)
    print("[ARDUINO] → EXHAUST:100 sustained — PA: We clear? — DIC: I WIN")

def run_haunted():
    print("[ARDUINO] → SHOW:HAUNTED")
    print("[ARDUINO] → All lights flicker slow — Engine RPM fluctuates")
    print("[ARDUINO] → PA: creaking door sound"); time.sleep(1)
    print("[ARDUINO] → All lights off"); time.sleep(1)
    print("[ARDUINO] → ALL LIGHTS BLAST ON — Train horn — DIC: BOO")

def run_stadium():
    print("[ARDUINO] → SHOW:STADIUM")
    print("[ARDUINO] → PA: crowd roar — EXHAUST:100 — All lights full")
    print("[ARDUINO] → Truck raises to max — Horn victory sequence")
    print("[ARDUINO] → DIC: LETS GOOO")

def run_sleeping_giant():
    print("[ARDUINO] → SHOW:SLEEPING_GIANT")
    print("[ARDUINO] → All lights off — Engine minimum idle"); time.sleep(2)
    print("[ARDUINO] → Single amber pulse slow"); time.sleep(1)
    print("[ARDUINO] → EXHAUST:100 sudden blast — ALL LIGHTS ON — Max height")
    print("[ARDUINO] → Train horn x5 — DIC: DID YOU THINK I WAS SLEEPING")

def run_fuel_economy():
    print("[ARDUINO] → SHOW:FUEL_ECONOMY")
    print("[ARDUINO] → Truck lowers to lowest — EXHAUST:0 — Interior green dim")
    print("[ARDUINO] → Engine minimum idle — DIC: 8 MPG — OUTSTANDING")

def run_ultimatum():
    print("[ARDUINO] → SHOW:ULTIMATUM")
    print("[ARDUINO] → Amber LEDs pulse slow — PA: I am going to say this once."); time.sleep(1)
    print("[ARDUINO] → EXHAUST:100 sustained"); time.sleep(1)
    print("[ARDUINO] → PA: We clear? — DIC: GOOD TALK")

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
                truck_state['exhaust'] = val; print(f"[ARDUINO] → EXHAUST:{val}"); return f"Exhaust at {val} percent."
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
        if 'underglow on'    in item: print("[ARDUINO] → UNDERBODY:255"); return "Underglow on."
        if 'underglow off'   in item: print("[ARDUINO] → UNDERBODY:0");   return "Underglow off."
        if 'wheel wells on'  in item: print("[ARDUINO] → WHEELWELL:255"); return "Wheel wells on."
        if 'wheel wells off' in item: print("[ARDUINO] → WHEELWELL:0");   return "Wheel wells off."
        if 'interior dim'    in item: print("[ARDUINO] → INTERIOR:80");   return "Interior dimmed."
        if 'interior full'   in item: print("[ARDUINO] → INTERIOR:255");  return "Interior full brightness."
        if 'all off'         in item:
            print("[ARDUINO] → UNDERBODY:0\n[ARDUINO] → WHEELWELL:0\n[ARDUINO] → INTERIOR:0")
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
        truck_state['exhaust'] = 100; print("[ARDUINO] → SHOW:SLAM"); return "Dropping it."
    if 'drunk' in t:       run_drunk();        return "Activating the Drunk. Try to look casual."
    if 'sneeze' in t:      run_sneeze();       return "Gesundheit."
    if 'stalker' in t:     run_stalker();      return "Going dark."
    if any(x in t for x in ['existential','existential crisis']): run_existential(); return "Alright. I will get the violin."
    if 'negotiation' in t: run_negotiations(); return "I have a particular set of skills."
    if 'goodbye' in t or ('good' in t and 'night' in t and 'show' in t): run_goodbye(); return "That is enough for today."
    if 'motivational' in t or 'motivate me' in t: run_motivational(); return "Let's go champ."
    if 'karen' in t:       run_karen();        return "Can I speak to your manager."
    if any(x in t for x in ['therapy', 'need a minute', 'rough day']):
        print("[ARDUINO] → SHOW:THERAPY\n[ARDUINO] → Seat heat ON\n[ARDUINO] → Interior warm amber")
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
        truck_state['ethanol'] = 85; print("[ARDUINO] → ETHANOL:85"); return "Full E85. Power map loaded. About time."
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
        truck_state['exhaust'] = 100; print("[ARDUINO] → EXHAUST:100"); return "Opening it up."
    if any(x in t for x in ['close exhaust','quiet down','close it','close the exhaust']):
        truck_state['exhaust'] = 0; print("[ARDUINO] → EXHAUST:0"); return "Closing it down."
    if t in ['exhaust','exhaust level','exhaust percent']:
        return f"Exhaust is at {truck_state['exhaust']} percent."
    if 'exhaust' in t and any(x in t for x in ['50','half','halfway']):
        truck_state['exhaust'] = 50; print("[ARDUINO] → EXHAUST:50"); return "Exhaust at 50 percent."

    # ── TRACTION CONTROL ─────────────────
    if any(x in t for x in ['tc off','traction off','kill tc']):
        truck_state['tc_on'] = False; truck_state['tc_locked'] = False
        print("[ARDUINO] → TC_OFF"); return "TC off. Road looks dry. We are good."
    if any(x in t for x in ['tc on','traction on','lock tc']):
        truck_state['tc_on'] = True; truck_state['tc_locked'] = True
        print("[ARDUINO] → TC_LOCK"); return "TC on."

    # ── GHOST MODE ───────────────────────
    if 'ghost' in t and 'off' not in t:
        truck_state['ghost_mode'] = True; truck_state['exhaust'] = 0
        print("[ARDUINO] → EXHAUST:0\n[ARDUINO] → UNDERBODY:0\n[ARDUINO] → GROUND:0")
        return "Going invisible."
    if any(x in t for x in ['ghost off','turn ghost off']):
        truck_state['ghost_mode'] = False; print("[ARDUINO] → GHOST_OFF"); return "Back to normal."

    # ── FACTORY CONTROLS ─────────────────
    if any(x in t for x in ['headlights on','lights on','turn on lights']):
        truck_state['headlights'] = True; print("[ARDUINO] → HEADLIGHTS:ON"); return "Headlights on."
    if any(x in t for x in ['headlights off','lights off','turn off lights']):
        truck_state['headlights'] = False; print("[ARDUINO] → HEADLIGHTS:OFF"); return "Headlights off."
    if any(x in t for x in ['high beams on','brights on']):
        truck_state['high_beams'] = True; print("[ARDUINO] → HIGHBEAMS:ON"); return "High beams on."
    if any(x in t for x in ['high beams off','brights off']):
        truck_state['high_beams'] = False; print("[ARDUINO] → HIGHBEAMS:OFF"); return "High beams off."
    if any(x in t for x in ['fog lights on','fogs on']):
        truck_state['fog_lights'] = True; print("[ARDUINO] → FOGLIGHTS:ON"); return "Fog lights on."
    if any(x in t for x in ['fog lights off','fogs off']):
        truck_state['fog_lights'] = False; print("[ARDUINO] → FOGLIGHTS:OFF"); return "Fog lights off."
    if any(x in t for x in ['hazards on','flashers on','four ways on']):
        truck_state['hazards'] = True; print("[ARDUINO] → HAZARDS:ON"); return "Hazards on."
    if any(x in t for x in ['hazards off','flashers off','four ways off']):
        truck_state['hazards'] = False; print("[ARDUINO] → HAZARDS:OFF"); return "Hazards off."
    if any(x in t for x in ['ac on','air on','turn on ac']):
        truck_state['ac_on'] = True; print("[ARDUINO] → AC:ON"); return "AC on."
    if any(x in t for x in ['ac off','air off','turn off ac']):
        truck_state['ac_on'] = False; print("[ARDUINO] → AC:OFF"); return "AC off."
    if any(x in t for x in ['heat on','heater on','turn on heat']):
        truck_state['heat_on'] = True; print("[ARDUINO] → HEAT:ON"); return "Heat on."
    if any(x in t for x in ['heat off','heater off','turn off heat']):
        truck_state['heat_on'] = False; print("[ARDUINO] → HEAT:OFF"); return "Heat off."
    if 'fan' in t:
        for level in ['1','2','3','4','5','6','7','8']:
            if level in t:
                truck_state['fan_speed'] = int(level); print(f"[ARDUINO] → FAN:{level}"); return f"Fan speed {level}."
        if 'up' in t or 'higher' in t:
            new = min(8, truck_state['fan_speed'] + 1)
            truck_state['fan_speed'] = new; print(f"[ARDUINO] → FAN:{new}"); return f"Fan speed {new}."
        if 'down' in t or 'lower' in t:
            new = max(0, truck_state['fan_speed'] - 1)
            truck_state['fan_speed'] = new; print(f"[ARDUINO] → FAN:{new}"); return f"Fan speed {new}."
    if any(x in t for x in ['windows down','roll down windows','open windows']):
        truck_state['windows'] = {'fl':'down','fr':'down','rl':'down','rr':'down'}
        print("[ARDUINO] → WINDOWS:ALL_DOWN"); return "Windows down."
    if any(x in t for x in ['windows up','roll up windows','close windows']):
        truck_state['windows'] = {'fl':'up','fr':'up','rl':'up','rr':'up'}
        print("[ARDUINO] → WINDOWS:ALL_UP"); return "Windows up."
    if 'driver window' in t and 'down' in t:
        truck_state['windows']['fl'] = 'down'; print("[ARDUINO] → WINDOW:FL_DOWN"); return "Driver window down."
    if 'driver window' in t and 'up' in t:
        truck_state['windows']['fl'] = 'up'; print("[ARDUINO] → WINDOW:FL_UP"); return "Driver window up."
    if 'passenger window' in t and 'down' in t:
        truck_state['windows']['fr'] = 'down'; print("[ARDUINO] → WINDOW:FR_DOWN"); return "Passenger window down."
    if 'passenger window' in t and 'up' in t:
        truck_state['windows']['fr'] = 'up'; print("[ARDUINO] → WINDOW:FR_UP"); return "Passenger window up."
    if any(x in t for x in ['wipers on','turn on wipers']):
        truck_state['wipers'] = 'on'; print("[ARDUINO] → WIPERS:ON"); return "Wipers on."
    if any(x in t for x in ['wipers off','turn off wipers']):
        truck_state['wipers'] = 'off'; print("[ARDUINO] → WIPERS:OFF"); return "Wipers off."
    if 'wiper' in t and 'fast' in t:
        truck_state['wipers'] = 'fast'; print("[ARDUINO] → WIPERS:FAST"); return "Wipers on fast."
    if 'wiper' in t and any(x in t for x in ['slow','intermittent','low']):
        truck_state['wipers'] = 'slow'; print("[ARDUINO] → WIPERS:SLOW"); return "Wipers on slow."
    if any(x in t for x in ['fold mirrors','mirrors in','tuck mirrors']):
        truck_state['mirrors_folded'] = True; print("[ARDUINO] → MIRRORS:FOLD"); return "Mirrors folded."
    if any(x in t for x in ['unfold mirrors','mirrors out','extend mirrors']):
        truck_state['mirrors_folded'] = False; print("[ARDUINO] → MIRRORS:EXTEND"); return "Mirrors extended."
    if t == 'horn' or 'tap horn' in t or 'beep' in t:
        print("[ARDUINO] → HORN:TAP"); return "Tap."
    if 'train horn' in t:
        print("[ARDUINO] → HORN:TRAIN"); return "Train horn."
    if 'air horn' in t:
        print("[ARDUINO] → HORN:AIR"); return "Air horn."

    # ── WEATHER ──────────────────────────
    if any(x in t for x in ['weather','how cold','how hot','raining','outside temp','temperature outside']):
        data = get_weather(); weather.update(data); temp = weather['temp']; condition = weather['condition']
        if weather['snowing']:  return f"{temp}F and snowing in Salem. 4WD is ready. TC stays on."
        if weather['raining']:  return f"{temp}F and raining. TC locked on. Road will be slick."
        if weather['freezing']: return f"{temp}F. Everything is tighter today. Give me a minute to warm up."
        if temp > 90:           return f"{temp}F outside. Heat is going to build faster today."
        if temp < 50:           return f"{temp}F. Cold start territory. Oil needs a minute."
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
        truck_state['bed_lights'] = True; print("[ARDUINO] → BED_ON"); return "Bed lights on."
    if 'bed light' in t and any(x in t for x in ['off','close']):
        truck_state['bed_lights'] = False; print("[ARDUINO] → BED_OFF"); return "Bed lights off."
    if 'hood light' in t and 'on' in t:
        truck_state['hood_lights'] = True; print("[ARDUINO] → HOOD_ON"); return "Hood lights on."
    if 'service mode' in t:
        print("[ARDUINO] → SERVICE_MODE_ON"); return "Service mode active. Nothing will move unless you tell me."
    if any(x in t for x in ['cool down','aux pump','cooling']):
        truck_state['cool_on'] = True; print("[ARDUINO] → COOL_ON"); return "Aux pump on. Cooling down."
    if any(x in t for x in ['high idle','idle up']):
        truck_state['idle_on'] = True; truck_state['rpm'] = 1500
        print("[ARDUINO] → IDLE_ON:1500"); return "High idle active. 1500 RPM."
    if any(x in t for x in ["let's run","run it","push it","launch"]):
        truck_state['rpm'] = 5500; truck_state['speed'] = 60; truck_state['boost'] = 12
        print("[ARDUINO] → TC_OFF\n[ARDUINO] → EXHAUST:100"); return "Ready. Let's go."
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
            print("[ARDUINO] → EXHAUST:0\n[ARDUINO] → TC_LOCK\n[ARDUINO] → COOL_ON")
            log_moment('warning', f"Oil hit {oil}F — Archer stepped in"); last_oil = now
        elif oil > 220 and now - last_oil > 60:
            msg = f"Oil is climbing — {oil} degrees. Keep an eye on it."
            print(f"\n[ARCHER] {msg}"); speak(msg); last_oil = now
        elif oil < 210 and truck_state['tc_locked'] and truck_state['cool_on']:
            msg = "Oil is back down. Everything is yours."
            print(f"\n[ARCHER] {msg}"); speak(msg)
            truck_state['tc_locked'] = False; truck_state['cool_on'] = False
            print("[ARDUINO] → TC_RELEASE\n[ARDUINO] → COOL_OFF")

        if v < 11.8 and now - last_bat > 60:
            msg = "Battery dropping. Connecting auxiliary."
            print(f"\n[ARCHER] {msg}"); speak(msg); print("[ARDUINO] → AUXBAT_ON"); last_bat = now

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
</div>

<div id="content">

  <!-- DEFAULT DASH -->
  <div id="mode-default" class="mode-screen active">
    <div class="data-grid">
      <div class="data-box"><div class="data-label">OIL TEMP</div><div class="data-value" id="d-oil">195F</div></div>
      <div class="data-box"><div class="data-label">RPM</div><div class="data-value" id="d-rpm">750</div></div>
      <div class="data-box"><div class="data-label">BOOST</div><div class="data-value" id="d-boost">0 PSI</div></div>
      <div class="data-box"><div class="data-label">E85</div><div class="data-value good" id="d-eth">82%</div></div>
      <div class="data-box"><div class="data-label">BATTERY</div><div class="data-value" id="d-bat">13.8V</div></div>
      <div class="data-box"><div class="data-label">EXHAUST</div><div class="data-value" id="d-exh">30%</div></div>
    </div>
    <div class="data-grid">
      <div class="gauge-wrap">
        <div class="gauge-label">RPM GAUGE</div>
        <canvas class="gauge" id="gauge-rpm" width="140" height="80"></canvas>
        <div class="gauge-val" id="gval-rpm" style="color:#fff">750</div>
      </div>
      <div class="gauge-wrap">
        <div class="gauge-label">BOOST GAUGE</div>
        <canvas class="gauge" id="gauge-boost" width="140" height="80"></canvas>
        <div class="gauge-val" id="gval-boost" style="color:#fff">0 PSI</div>
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
@display_app.route('/')
def display_index():
    return render_template_string(DISPLAY_HTML)
@display_app.route('/voice_command', methods=['POST'])
def voice_command_endpoint():
    from flask import request as flask_request
    try:
        data    = flask_request.get_json()
        command = data.get('command', '').strip()
        if not command:
            return jsonify({'response': ''})
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



@display_app.route('/register_device', methods=['POST'])
def register_device_endpoint():
    from flask import request as flask_request
    data        = flask_request.get_json()
    fingerprint = data.get('fingerprint', '')
    name        = data.get('name', 'Unknown')
    tier        = int(data.get('tier', 1))
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
@display_app.route('/ayden')
@display_app.route('/tier1')
def tier1_page():
    from flask import Response as FR
    return FR(get_tier_html(1), mimetype='text/html')

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

def terminal_access_check(request):
    fp = request.args.get('fp') or request.cookies.get('archer_fp', 'unknown')
    tier = get_device_tier(fp)
    return tier in TERMINAL_ALLOWED_TIERS, tier

# ── REAL SHELL EXECUTION ─────────────────────────────────
import subprocess, select, pty, os as _os

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
body{background:#0a0a0a;color:#ff3333;font-family:'Courier New',monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#header{background:#0d0d0d;border-bottom:1px solid #1a1a1a;padding:8px 12px;display:flex;align-items:center;gap:12px;flex-shrink:0}
#header-title{font-size:11px;letter-spacing:3px;color:#cc0000;flex:1}
.tab-btn{background:none;border:1px solid #222;color:#444;font-family:monospace;font-size:10px;letter-spacing:2px;padding:4px 10px;border-radius:3px;cursor:pointer;transition:all 0.2s}
.tab-btn.active{border-color:#cc0000;color:#cc0000;background:#1a0000}
#pi-status{font-size:9px;letter-spacing:1px;padding:3px 8px;border-radius:3px}
.pi-online{background:#001a00;color:#00ff00;border:1px solid #00ff00}
.pi-offline{background:#1a0000;color:#cc0000;border:1px solid #330000}
#terminal-container{flex:1;display:flex;flex-direction:column;overflow:hidden}
#output{flex:1;padding:10px 12px;overflow-y:auto;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all}
#input-row{display:flex;padding:8px;border-top:1px solid #1a1a1a;background:#050505;flex-shrink:0}
.prompt-label{color:#cc0000;padding:6px 8px;font-size:13px;flex-shrink:0}
#cmd{flex:1;background:#000;color:#ff3333;border:1px solid #1a1a1a;border-radius:4px;padding:6px 8px;font-family:'Courier New',monospace;font-size:12px;outline:none;caret-color:#ff3333}
#cmd:focus{border-color:#cc0000}
#send-btn{background:#1a0000;border:1px solid #cc0000;color:#cc0000;font-family:monospace;font-size:10px;letter-spacing:1px;padding:6px 14px;border-radius:4px;cursor:pointer;margin-left:6px;flex-shrink:0}
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
  <button class="tab-btn" id="tab-pi" onclick="switchTab('pi')">PI</button>
  <span id="pi-status" class="pi-offline">PI OFFLINE</span>
</div>
<div id="terminal-container">
  <div id="server-terminal" style="display:flex;flex-direction:column;height:100%">
    <div id="output"></div>
    <div id="input-row">
      <span class="prompt-label">&#9654;</span>
      <input id="cmd" type="text" placeholder="enter command..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"/>
      <button id="send-btn" onclick="sendCmd()">RUN</button>
    </div>
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
    document.getElementById('tab-server').classList.toggle('active', tab === 'server');
    document.getElementById('tab-pi').classList.toggle('active', tab === 'pi');
    document.getElementById('server-terminal').style.display = tab === 'server' ? 'flex' : 'none';
    document.getElementById('pi-iframe').style.display = tab === 'pi' ? 'block' : 'none';
    if (tab === 'pi') checkPiStatus();
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
</script>
</body>
</html>"""
    from flask import Response as FR
    return FR(terminal_html, mimetype='text/html')

# ── TERMINAL EXEC ENDPOINT ───────────────────────────────
BLOCKED_CMDS = ['rm -rf /', 'mkfs', 'dd if=/dev/zero', ':(){ :|:& };:', 'shutdown', 'reboot']

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
    # Block dangerous commands
    for blocked in BLOCKED_CMDS:
        if blocked in cmd:
            return jsonify({'error': f'Blocked: {blocked}'})
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
    if token != 'archer2026':
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
def get_tier_html(tier):
    """Returns a tailored display HTML for each access tier."""
    from flask import request as freq

    # Tier 1 — Ayden — full display (same as main but auto-loads Tier 1)
    if tier == 1:
        return DISPLAY_HTML.replace("'profile-name'>AYDEN", "'profile-name' style='color:#cc0000'>AYDEN ★")

    # Tier 2 — Girlfriend — comfort focused, no performance tabs
    elif tier == 2:
        return """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#cc0000">
<title>Archer — Passenger</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;color:#fff;font-family:'Courier New',monospace;height:100vh;display:flex;flex-direction:column}
#header{background:#111;border-bottom:2px solid #cc0000;padding:8px 14px;display:flex;justify-content:space-between;align-items:center}
h1{color:#cc0000;font-size:15px;letter-spacing:3px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px}
.card{background:#0d0d0d;border:1px solid #1a1a1a;border-radius:8px;padding:14px;text-align:center}
.label{font-size:8px;color:#555;letter-spacing:2px;margin-bottom:4px}
.value{font-size:22px;font-weight:bold}
.good{color:#00cc44}.warn{color:#ffaa00}.danger{color:#cc0000}
.btn{width:100%;background:#0d0d0d;border:1px solid #1a1a1a;border-radius:8px;padding:14px;color:#888;font-family:monospace;font-size:9px;letter-spacing:1px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:4px}
.btn span:first-child{font-size:20px}
.section{font-size:8px;color:#444;letter-spacing:3px;padding:6px 12px 2px;border-bottom:1px solid #0d0d0d}
#archer-bar{background:#0a0a0a;border-top:1px solid #111;padding:6px 12px;font-size:10px;color:#555}
</style>
</head>
<body>
<div id="header"><h1>ARCHER</h1><span style="font-size:9px;color:#555">PASSENGER</span></div>
<div class="grid">
  <div class="card"><div class="label">TEMP</div><div class="value" id="p-temp">72F</div></div>
  <div class="card"><div class="label">WEATHER</div><div class="value" id="p-weather" style="font-size:14px">--</div></div>
  <div class="card"><div class="label">BATTERY</div><div class="value good" id="p-bat">13.8V</div></div>
  <div class="card"><div class="label">OIL TEMP</div><div class="value" id="p-oil">195F</div></div>
</div>
<div class="section">COMFORT CONTROLS</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:10px 12px">
  <button class="btn" onclick="cmd('AC on')"><span>❄️</span><span>A/C</span></button>
  <button class="btn" onclick="cmd('heat on')"><span>🔥</span><span>HEAT</span></button>
  <button class="btn" onclick="cmd('seat heat on')"><span>🪑</span><span>SEAT</span></button>
  <button class="btn" onclick="cmd('windows down')"><span>🪟</span><span>WINDOWS</span></button>
  <button class="btn" onclick="cmd('headlights')"><span>💡</span><span>LIGHTS</span></button>
  <button class="btn" onclick="cmd('hazards')"><span>⚠️</span><span>HAZARDS</span></button>
</div>
<div id="archer-bar">ARCHER — <span id="p-msg">Everything looks good.</span></div>
<script>
function cmd(c){fetch('/voice_command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:c,tier_override:2})}).then(r=>r.json()).then(d=>{if(d.response)document.getElementById('p-msg').textContent=d.response;});}
function poll(){fetch('/display_data').then(r=>r.json()).then(d=>{
  const set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
  set('p-temp', d.temp_setting+'F');
  set('p-weather', d.weather||'--');
  const b=document.getElementById('p-bat');if(b){b.textContent=d.battery+'V';b.className='value '+(d.battery>13?'good':d.battery>12?'warn':'danger');}
  const o=document.getElementById('p-oil');if(o){o.textContent=d.oil_temp+'F';o.className='value '+(d.oil_temp<215?'good':d.oil_temp<225?'warn':'danger');}
  if(d.archer_msg)set('p-msg',d.archer_msg);
});}
setInterval(poll,1000);poll();
</script></body></html>"""

    # Tier 3 — Family — read-only status display
    elif tier == 3:
        return """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Archer — Status</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;color:#fff;font-family:'Courier New',monospace;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px}
h1{color:#cc0000;font-size:18px;letter-spacing:4px;margin-bottom:4px;text-align:center}
.sub{font-size:9px;color:#333;letter-spacing:3px;margin-bottom:24px;text-align:center}
.row{display:flex;justify-content:space-between;width:100%;max-width:320px;padding:10px 0;border-bottom:1px solid #0d0d0d}
.lbl{font-size:9px;color:#444;letter-spacing:1px}
.val{font-size:14px;font-weight:bold}
.good{color:#00cc44}.warn{color:#ffaa00}.bad{color:#cc0000}
#msg{margin-top:20px;font-size:10px;color:#555;text-align:center;max-width:280px;line-height:1.6}
</style>
</head>
<body>
<h1>ARCHER</h1>
<div class="sub">2006 GMC SIERRA 2500HD</div>
<div class="row"><span class="lbl">OIL TEMP</span><span class="val good" id="f-oil">195F</span></div>
<div class="row"><span class="lbl">BATTERY</span><span class="val good" id="f-bat">13.8V</span></div>
<div class="row"><span class="lbl">BOOST</span><span class="val" id="f-boost">0 PSI</span></div>
<div class="row"><span class="lbl">WEATHER</span><span class="val" id="f-weather">--</span></div>
<div class="row"><span class="lbl">TC</span><span class="val good" id="f-tc">ON</span></div>
<div class="row"><span class="lbl">DRIVE MODE</span><span class="val" id="f-mode">COMFORT</span></div>
<div id="msg">Everything looks good.</div>
<script>
function poll(){fetch('/display_data').then(r=>r.json()).then(d=>{
  const s=(id,v,cls)=>{const e=document.getElementById(id);if(e){e.textContent=v;if(cls)e.className='val '+cls;}};
  s('f-oil',d.oil_temp+'F',d.oil_temp>225?'bad':d.oil_temp>210?'warn':'good');
  s('f-bat',d.battery+'V',d.battery>13?'good':d.battery>12?'warn':'bad');
  s('f-boost',d.boost+' PSI');
  s('f-weather',d.weather||'--');
  s('f-tc',d.tc_on?'ON':'OFF',d.tc_on?'good':'warn');
  s('f-mode',(d.drive_mode||'COMFORT').toUpperCase());
  if(d.archer_msg)document.getElementById('msg').textContent=d.archer_msg;
});}
setInterval(poll,2000);poll();
</script></body></html>"""

    # Tier 4 — Valet — locked, warnings only
    else:
        return """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Archer</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;color:#fff;font-family:'Courier New',monospace;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:30px}
h1{color:#cc0000;font-size:20px;letter-spacing:4px;margin-bottom:8px}
.sub{font-size:9px;color:#333;letter-spacing:3px;margin-bottom:40px}
.lock{font-size:60px;margin-bottom:20px}
.msg{font-size:11px;color:#444;letter-spacing:1px;line-height:1.8;max-width:260px}
.warn{color:#cc0000;font-size:10px;margin-top:20px;letter-spacing:2px}
#speed{font-size:48px;font-weight:bold;color:#fff;margin:20px 0}
.speed-label{font-size:9px;color:#444;letter-spacing:3px}
</style>
</head>
<body>
<h1>ARCHER</h1>
<div class="sub">VEHICLE SECURITY ACTIVE</div>
<div class="lock">🔒</div>
<div class="speed-label">CURRENT SPEED</div>
<div id="speed">0</div>
<div class="speed-label">MPH</div>
<div class="msg">This vehicle is monitored.<br>All activity is being logged.<br>Drive responsibly.</div>
<div class="warn" id="v-warn"></div>
<script>
function poll(){fetch('/display_data').then(r=>r.json()).then(d=>{
  const spd=document.getElementById('speed');
  if(spd)spd.textContent=d.sensor_data?d.sensor_data.speed_mph||0:0;
  const w=document.getElementById('v-warn');
  if(w&&d.warning)w.textContent='⚠ '+((d.warning_msg||'').toUpperCase().replace(/_/g,' '));
});}
setInterval(poll,1000);poll();
</script></body></html>"""

def run_tier_server(tier, port):
    """Run a Flask server on a specific port serving tier-specific content."""
    from flask import Flask as _Flask, Response as _Resp, request as _req, jsonify as _j
    app  = _Flask(f'archer_t{tier}_{port}')
    log  = _logging.getLogger('werkzeug')
    log.setLevel(_logging.ERROR)
    _t   = tier  # capture in closure

    def _index():
        return _Resp(get_tier_html(_t), mimetype='text/html')

    def _data():
        d = get_display_data()
        d['spike_history']     = spike_history
        d['connected_clients'] = len(connected_clients)
        d['sensor_data']       = dict(sensor_data)
        d['device_tier']       = _t
        return _j(d)

    def _vcmd():
        data    = _req.get_json()
        command = (data or {}).get('command', '').strip()
        if _t == 2:
            allowed = ['ac','heat','seat','windows','headlights','hazards','fog','weather','oil','battery']
            if not any(a in command.lower() for a in allowed):
                return _j({'response': 'That control is not available in passenger mode.'})
        if _t >= 3:
            return _j({'response': 'Read-only access.'})
        response = handle_command(command)
        if response is None:
            response = ask_archer(command)
        if response:
            speak(response)
            last_archer_msg['text'] = response
        return _j({'response': response or ''})

    def _manifest():
        names = {1:'Archer', 2:'Archer Passenger', 3:'Archer Status', 4:'Archer'}
        return _j({'name': names.get(_t,'Archer'), 'short_name':'Archer',
                   'start_url':'/','display':'standalone',
                   'background_color':'#000','theme_color':'#cc0000'})

    # Register routes with unique endpoint names to avoid Flask conflicts
    app.add_url_rule('/',               f't{port}_index',    _index)
    app.add_url_rule('/display_data',   f't{port}_data',     _data)
    app.add_url_rule('/voice_command',  f't{port}_vcmd',     _vcmd,    methods=['POST'])
    app.add_url_rule('/manifest.json',  f't{port}_manifest', _manifest)

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)

def run_display_server():
    log = _logging.getLogger('werkzeug')
    log.setLevel(_logging.ERROR)
    port = int(os.environ.get('PORT', 5001))
    display_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)

# ── MAIN ─────────────────────────────────
def main():
    print("╔══════════════════════════════════════╗")
    print("║        ARCHER — STARTING UP          ║")
    print("║   2006 GMC Sierra 2500HD — Built     ║")
    print("║       Salem Missouri  2026           ║")
    print("╚══════════════════════════════════════╝")
    print()
    print("Commands:")
    print("  SHOW MODES:  flex / slam / drunk / sneeze / stalker / existential crisis")
    print("               negotiations / goodbye / motivational / karen / therapy")
    print("               reveille / impatient / politician / suspicious / conspiracy")
    print("               introvert / wrong neighborhood / passive aggressive / identity crisis")
    print("               exit interview / backup warning / cinema / concert / haunted")
    print("               stadium / sleeping giant / fuel economy / ultimatum / argument")
    print("  EXHAUST:     open exhaust / close exhaust / exhaust 50")
    print("  OCTANE:      about to fill 87 / 93 octane / 98 RON / e85 fill")
    print("  TC:          tc off / tc on")
    print("  DRIVE MODE:  comfort mode / sport mode / tow mode / weather mode / performance mode")
    print("  FACTORY:     headlights on / windows down / ac on / fan 3 / wipers on / hazards on")
    print("  PROFILES:    load profile [name] / create profile [name] tier [1-4] / list profiles")
    print("  ROADS:       highway 72 / highway 19 / highway 32 / backroad / industrial park")
    print("  MUSIC:       playing [song] / remember this song / save lighting for song")
    print("  LAUNCHES:    log launch 3.8 / personal best / launch log")
    print("  LEGACY:      legacy mode / voice note [text] / show legacy")
    print("  MEMORY:      archer memory / log this moment [text]")
    print("  WEATHER:     weather")
    print("  OTHER:       ghost mode / service mode / status / menu / engine off")
    print("  or just talk — Archer will respond")
    print()
    print("─" * 42)
    print()

    threading.Thread(target=tts_worker,        daemon=True).start()
    threading.Thread(target=update_awareness,  daemon=True).start()
    threading.Thread(target=safety_monitor,    daemon=True).start()
    threading.Thread(target=casual_monitor,    daemon=True).start()
    threading.Thread(target=weather_monitor,   daemon=True).start()
    threading.Thread(target=voice_monitor,     daemon=True).start()
    threading.Thread(target=run_display_server,daemon=True).start()
    time.sleep(3)
    threading.Thread(target=fetch_ngrok_url, daemon=True).start()
    threading.Thread(target=record_spikes,        daemon=True).start()
    threading.Thread(target=client_timeout_monitor, daemon=True).start()
    threading.Thread(target=live_data_loop,          daemon=True).start()
    threading.Thread(target=valet_monitor,           daemon=True).start()
    threading.Thread(target=curfew_monitor,           daemon=True).start()
    threading.Thread(target=weather_alert_monitor,    daemon=True).start()
    threading.Thread(target=discord_monitor,            daemon=True).start()
    threading.Thread(target=openclaw_monitor,            daemon=True).start()
    # Tier servers — separate port per access level
    for _tier, _port in [(1,5002),(2,5003),(3,5004),(4,5005)]:
        threading.Thread(target=run_tier_server, args=(_tier,_port), daemon=True).start()
        time.sleep(0.3)   # stagger startup to avoid port conflicts

    print("[DISPLAY] In Codespaces — click the Ports tab and open port 5001")
    print("[DISPLAY] On local network — open http://[your-ip]:5001")
    print()

    archer_memory['total_sessions'] += 1
    if not archer_memory['first_drive']:
        archer_memory['first_drive'] = datetime.now().strftime('%B %d %Y')

    load_state()
    time.sleep(0.5)
    greeting = get_daily_greeting()
    print(f"[ARCHER] {greeting}")
    speak(greeting)
    print()

    while True:
        try:
            try:
                user_input = input("[YOU] ").strip()
            except EOFError:
                time.sleep(60)
                continue
            if not user_input:
                continue
            response = handle_command(user_input)
            if response is None:
                print("[ARCHER] thinking...")
                response = ask_archer(user_input)
            if response:
                print(f"[ARCHER] {response}")
                speak(response)
                last_archer_msg['text'] = response
                print()
        except KeyboardInterrupt:
            print("\n[ARCHER] See you tomorrow.")
            save_state()
            break

if __name__ == "__main__":
    main()