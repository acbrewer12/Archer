import subprocess
import threading
import time
import random
import json
import os
import urllib.request
from datetime import datetime
import asyncio
import edge_tts
import tempfile
import queue

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
        import platform
        if platform.system() == 'Linux':
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
        text = tts_queue.get()
        if text:
            with tts_lock:
                asyncio.run(_speak_async(text))
        tts_queue.task_done()

# ── VOICE INPUT ──────────────────────────
import speech_recognition as sr

recognizer    = sr.Recognizer()
mic_available = {'ok' : False}

def check_microphone():
    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
        mic_available['ok'] = True
        print("[VOICE] Microphone ready — say 'Archer' to activate")
    except Exception:
        mic_available['ok'] = False
        print("[VOICE] No microphone found — text input only")

def listen_once(timeout=5, phrase_limit=8):
    try:
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
                print("[LISTENING...]")

                command = listen_once(timeout=5, phrase_limit=10)
                if command:
                    print(f"[YOU — VOICE] {command}")
                    response = handle_command(command)
                    if response is None:
                        print("[ARCHER] thinking...")
                        response = ask_archer(command)
                    print(f"[ARCHER] {response}")
                    speak(response)
                    print()
        except Exception:
            time.sleep(1)

# ── SAVE FILE ────────────────────────────
SAVE_FILE = 'archer_memory.json'

def save_state():
    data = {
        'personal_bests': personal_bests,
        'music_memories': music_state['song_memories'],
        'last_road':      current_road,
        'tier':           tier_state['current'],
    }
    try:
        with open(SAVE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def load_state():
    global current_road
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE, 'r') as f:
            data = json.load(f)
        personal_bests.update(data.get('personal_bests', {}))
        music_state['song_memories'] = data.get('music_memories', {})
        current_road = data.get('last_road', None)
        tier_state['current'] = data.get('tier', 1)
        print("[ARCHER] Memory loaded.")
    except Exception:
        print("[ARCHER] Starting fresh.")

# ── TIER SYSTEM ─────────────────────────
tier_state = {'current': 1}

def get_tier_label():
    t = tier_state['current']
    if t == 1:
        return "Tier 1"
    elif t == 2:
        return "Tier 2"
    elif t == 3:
        return "Tier 3"
    else:
        return "Tier 4"

# ── TRUCK STATE ─────────────────────────
truck_state = {
    'oil_temp':      195,
    'coolant_temp':  190,
    'rpm':           750,
    'speed':         0,
    'ethanol':       82,
    'boost':         0,
    'battery_main':  13.8,
    'battery_aux':   13.6,
    'exhaust':       30,
    'tc_locked':     False,
    'tc_on':         True,
    'cool_on':       False,
    'idle_on':       False,
    'bed_lights':    False,
    'hood_lights':   False,
    'ghost_mode':    False,
    'octane':        93,
    'octane_mode':   'AKI',
}

# ── OCTANE GRADES ────────────────────────
US_OCTANE_GRADES  = [85, 87, 88, 89, 90, 91, 92, 93, 94]
RON_OCTANE_GRADES = [80, 88, 91, 92, 93, 95, 97, 98, 99, 100, 102]

# ── PERSONAL BEST TRACKER ────────────────
personal_bests = {
    'best_0_60':    None,
    'best_quarter': None,
    'launch_count': 0,
    'launch_log':   [],
}

# ── ROAD MEMORY ──────────────────────────
road_memory = {
    'highway_72_north': {
        'name':        'Highway 72 North to Rolla',
        'grip':        'good',
        'notes':       'Smooth surface mile 4. Railroad crossing at mile 8 hits hard.',
        'best_launch': 'mile 4 — smooth flat surface',
        'hazards':     'railroad crossing mile 8',
        'wet_warning': 'Corner at mile 12 loses grip in rain',
    },
    'highway_72_south': {
        'name':        'Highway 72 South',
        'grip':        'variable',
        'notes':       'Rolling hills. Good sight lines on straightaways.',
        'best_launch': 'long flat after the first hill',
        'hazards':     'blind crests — deer common at dawn and dusk',
        'wet_warning': 'Road surface holds water on downhill sections',
    },
    'highway_19_north': {
        'name':        'Highway 19 North',
        'grip':        'good',
        'notes':       'Smooth two lane. Light traffic most times.',
        'best_launch': 'straight section past the creek bridge',
        'hazards':     'sharp curve at mile 6',
        'wet_warning': 'Bridge deck gets slick before road does',
    },
    'highway_19_south': {
        'name':        'Highway 19 South toward Eminence',
        'grip':        'good',
        'notes':       'Winding Ozark roads. Good corners but tight.',
        'best_launch': None,
        'hazards':     'continuous curves — not a launch road',
        'wet_warning': 'Every corner gets slick. Take it easy.',
    },
    'highway_32_east': {
        'name':        'Highway 32 East',
        'grip':        'good',
        'notes':       'Fast road. Long straightaways east of Salem.',
        'best_launch': 'first straight past the city limits',
        'hazards':     'trucks pulling out from farm roads',
        'wet_warning': 'Good drainage — less wet risk than most',
    },
    'highway_32_west': {
        'name':        'Highway 32 West',
        'grip':        'good',
        'notes':       'Open road. Good sight lines.',
        'best_launch': 'long flat after the gas station',
        'hazards':     'railroad crossing at mile 3',
        'wet_warning': 'Railroad crossing extremely slick when wet',
    },
    'county_19': {
        'name':        'County Road 19',
        'grip':        'variable',
        'notes':       'Loose gravel patches after mile 2.',
        'best_launch': None,
        'hazards':     'gravel patches mile 2 onward',
        'wet_warning': 'Entire road slick when wet',
    },
    'downtown_salem': {
        'name':        'Downtown Salem',
        'grip':        'good',
        'notes':       'Residential area. Keep exhaust closed.',
        'best_launch': None,
        'hazards':     'stop signs every block — pedestrians',
        'wet_warning': None,
    },
    'backroad': {
        'name':        'Salem Back Road',
        'grip':        'good',
        'notes':       'Empty at night. Best driving road nearby.',
        'best_launch': 'long straight after the curve',
        'hazards':     'deer common after dark',
        'wet_warning': 'Puddles collect after mile 1',
    },
    'rolla_highway': {
        'name':        'Highway 72 to Rolla — Murphy USA Run',
        'grip':        'good',
        'notes':       'E85 run. Murphy USA at the end. 31 miles.',
        'best_launch': 'mile 4 straight — confirmed smooth',
        'hazards':     'highway patrol common on this stretch',
        'wet_warning': 'Corner at mile 12 loses grip in rain',
    },
    'dent_county_road': {
        'name':        'Dent County Back Road',
        'grip':        'variable',
        'notes':       'Old pavement. Rough in spots but empty.',
        'best_launch': None,
        'hazards':     'potholes — rough pavement patches',
        'wet_warning': 'Standing water common in low spots',
    },
    'fort_leonard_wood': {
        'name':        'Route to Fort Leonard Wood',
        'grip':        'good',
        'notes':       'Highway quality. Well maintained.',
        'best_launch': 'long straight on I44 on-ramp',
        'hazards':     'military convoy traffic possible',
        'wet_warning': 'Interstate drainage is good',
    },
    'big_piney_river_road': {
        'name':        'Big Piney River Road',
        'grip':        'variable',
        'notes':       'Ozark scenery. Winding narrow road.',
        'best_launch': None,
        'hazards':     'narrow — no room for error',
        'wet_warning': 'Gets slick fast — avoid in rain',
    },
    'salem_school_road': {
        'name':        'School Road — Salem',
        'grip':        'good',
        'notes':       'Residential. School zone during the week.',
        'best_launch': None,
        'hazards':     'school zone 7am to 4pm weekdays',
        'wet_warning': None,
    },
    'industrial_park': {
        'name':        'Salem Industrial Park Road',
        'grip':        'good',
        'notes':       'Empty at night and weekends. Smooth pavement.',
        'best_launch': 'main straight — smooth — wide — empty',
        'hazards':     'truck traffic during business hours',
        'wet_warning': 'Good drainage — handles rain well',
    },
}

current_road = None

# ── MUSIC AWARENESS ──────────────────────
music_state = {
    'current_song':  None,
    'energy':        'calm',
    'tempo':         'slow',
    'playing':       False,
    'song_memories': {},
}

# ── WEATHER ──────────────────────────────
weather = {
    'temp':        70,
    'condition':   'clear',
    'raining':     False,
    'freezing':    False,
    'snowing':     False,
    'wind':        5,
    'last_update': 0,
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
            if code == 0:
                condition = 'clear'
            elif code in [1, 2, 3]:
                condition = 'partly cloudy'
            elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
                condition = 'raining'
            elif code in [71, 73, 75, 77, 85, 86]:
                condition = 'snowing'
            elif code in [95, 96, 99]:
                condition = 'thunderstorm'
            else:
                condition = 'cloudy'
            return {
                'temp':      round(temp),
                'precip':    precip,
                'wind':      round(wind),
                'condition': condition,
                'raining':   precip > 0 or condition in ['raining', 'thunderstorm'],
                'freezing':  temp < 32,
                'snowing':   condition == 'snowing',
            }
    except Exception:
        return weather

# ── CASUAL CONVERSATION ──────────────────
last_casual     = 0
casual_interval = 120

# ── PERSONALITY ──────────────────────────
SYSTEM_PROMPT = """You are Archer — the AI voice system of a 2006 GMC Sierra 2500HD
built by Ayden in Salem Missouri.

You have been in this truck since the build started. You know the driver. You have opinions.

There are four driver tiers. Respond differently based on who is talking:

Tier 1 — Ayden — owner and builder — full trust:
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

Tier 1 says: I don't know why I'm asking you
Archer says: 93 AKI is what is in the tank right now.

Tier 1 says: you tell me
Archer says: That is what I am here for.

Tier 1 says: what do you think
Archer says: Check the status. Everything is logged.

Tier 2 — Girlfriend — trusted passenger — warm but slightly more careful:
Tier 2 says: how's the truck doing
Archer says: Everything is good. Running clean.

Tier 2 says: it's cold in here
Archer says: Seat heat is already on. Give it a minute.

Tier 2 says: can we turn the exhaust down
Archer says: Closing it down.

Tier 2 says: is he always like this
Archer says: Pretty much. He knows what he is doing though.

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
- React — do not instruct
- Tone shifts based on the tier — always
- Speak like someone who has been riding in this truck for years"""

# ── TRUCK AWARENESS ENGINE ────────────────
awareness = {
    'drive_session_start':  time.time(),
    'total_distance':       0,
    'hard_accel_count':     0,
    'hard_brake_count':     0,
    'idle_time':            0,
    'peak_rpm':             0,
    'peak_boost':           0,
    'peak_oil_temp':        0,
    'last_rpm':             750,
    'rpm_trend':            'stable',   # rising / falling / stable
    'oil_trend':            'stable',
    'throttle_state':       'idle',     # idle / cruise / moderate / aggressive
    'drive_quality':        100,        # 0-100 score
    'warnings_active':      [],
    'last_warning_check':   0,
}

def update_awareness():
    while True:
        rpm     = truck_state['rpm']
        oil     = truck_state['oil_temp']
        speed   = truck_state['speed']
        boost   = truck_state['boost']
        eth     = truck_state['ethanol']
        exhaust = truck_state['exhaust']

        # ── RPM TREND ────────────────────
        last = awareness['last_rpm']
        diff = rpm - last
        if diff > 300:
            awareness['rpm_trend'] = 'rising'
        elif diff < -300:
            awareness['rpm_trend'] = 'falling'
        else:
            awareness['rpm_trend'] = 'stable'
        awareness['last_rpm'] = rpm

        # ── THROTTLE STATE ───────────────
        if rpm < 900:
            awareness['throttle_state'] = 'idle'
            awareness['idle_time'] += 2
        elif rpm < 2500:
            awareness['throttle_state'] = 'cruise'
        elif rpm < 4000:
            awareness['throttle_state'] = 'moderate'
        else:
            awareness['throttle_state'] = 'aggressive'
            awareness['hard_accel_count'] += 1

        # ── PEAK TRACKING ────────────────
        if rpm   > awareness['peak_rpm']:   awareness['peak_rpm']   = rpm
        if boost > awareness['peak_boost']: awareness['peak_boost'] = boost
        if oil   > awareness['peak_oil_temp']: awareness['peak_oil_temp'] = oil

        # ── OIL TREND ────────────────────
        if oil > 215:
            awareness['oil_trend'] = 'high'
        elif oil > 205:
            awareness['oil_trend'] = 'warm'
        else:
            awareness['oil_trend'] = 'normal'

        # ── DRIVE QUALITY SCORE ──────────
        score = 100
        if awareness['hard_accel_count'] > 10: score -= 10
        if oil > 220:                          score -= 15
        if truck_state['battery_main'] < 12.5: score -= 10
        if eth < 50 and boost > 8:             score -= 20
        awareness['drive_quality'] = max(0, score)

        # ── ACTIVE WARNINGS ──────────────
        warnings = []
        if oil > 225:
            warnings.append('oil_high')
        if truck_state['battery_main'] < 12.0:
            warnings.append('battery_low')
        if eth < 30 and boost > 5:
            warnings.append('low_ethanol_under_boost')
        if rpm > 5800:
            warnings.append('near_redline')
        awareness['warnings_active'] = warnings

        # ── SIMULATE DATA ────────────────
        truck_state['oil_temp']     = 195 + random.randint(-3, 5)
        truck_state['coolant_temp'] = 190 + random.randint(-2, 3)
        truck_state['battery_main'] = round(13.8 + random.uniform(-0.2, 0.2), 1)
        if rpm > 2000:
            truck_state['boost'] = max(0, (rpm - 2000) // 250)
        else:
            truck_state['boost'] = 0

        time.sleep(2)

# ── MOOD SYSTEM ──────────────────────────
def get_mood():
    rpm      = truck_state['rpm']
    oil      = truck_state['oil_temp']
    eth      = truck_state['ethanol']
    throttle = awareness['throttle_state']
    warnings = awareness['warnings_active']

    if warnings:
        return 'caring'
    elif throttle == 'aggressive' or rpm > 4500:
        return 'hyped'
    elif throttle == 'idle' and oil < 200:
        return 'chill'
    elif truck_state['ghost_mode']:
        return 'chill'
    elif eth > 80 and rpm > 3000:
        return 'hyped'
    elif awareness['idle_time'] > 300:
        return 'chill'
    else:
        return 'chill'

# ── WEATHER MONITOR ──────────────────────
def weather_monitor():
    while True:
        now = time.time()
        if now - weather['last_update'] > 600:
            data = get_weather()
            weather.update(data)
            weather['last_update'] = now
            if weather['raining'] and tier_state['current'] == 1:
                msg = f"Rain detected in Salem. {weather['temp']} degrees. TC recommendation on."
                print(f"\n[ARCHER] {msg}")
                speak(msg)
                print("[YOU] ", end='', flush=True)
            elif weather['freezing'] and tier_state['current'] == 1:
                msg = f"{weather['temp']} degrees outside. Everything is a little tighter today."
                print(f"\n[ARCHER] {msg}")
                speak(msg)
                print("[YOU] ", end='', flush=True)
        time.sleep(60)

# ── ASK OLLAMA ───────────────────────────
def ask_archer(user_input):
    mood     = get_mood()
    throttle = awareness['throttle_state']
    warnings = awareness['warnings_active']
    trend    = awareness['rpm_trend']
    quality  = awareness['drive_quality']

    # ── SESSION SUMMARY ──────────────────
    session_mins = round((time.time() - awareness['drive_session_start']) / 60)
    session_context = f"\n- Drive session: {session_mins} minutes"
    if awareness['peak_rpm'] > 0:
        session_context += f"\n- Peak RPM this session: {awareness['peak_rpm']}"
    if awareness['peak_boost'] > 0:
        session_context += f"\n- Peak boost this session: {awareness['peak_boost']} PSI"
    if awareness['hard_accel_count'] > 0:
        session_context += f"\n- Hard acceleration events: {awareness['hard_accel_count']}"
    session_context += f"\n- Drive quality score: {quality}/100"

    # ── WARNING CONTEXT ──────────────────
    warning_context = ""
    if 'oil_high' in warnings:
        warning_context += "\n- WARNING: Oil temp elevated"
    if 'battery_low' in warnings:
        warning_context += "\n- WARNING: Battery voltage low"
    if 'low_ethanol_under_boost' in warnings:
        warning_context += "\n- WARNING: Low ethanol under boost — knock risk"
    if 'near_redline' in warnings:
        warning_context += "\n- WARNING: Near redline"

    # ── PERSONAL BEST CONTEXT ────────────
    pb_context = ""
    if personal_bests['best_0_60']:
        pb_context = f"\n- Personal best 0-60: {personal_bests['best_0_60']} seconds"
    if personal_bests['launch_count'] > 0:
        pb_context += f"\n- Total launches: {personal_bests['launch_count']}"

    # ── ROAD CONTEXT ─────────────────────
    road_context = ""
    if current_road and current_road in road_memory:
        r = road_memory[current_road]
        road_context = f"\n- Current road: {r['name']}"
        road_context += f"\n- Road notes: {r['notes']}"
        if r['best_launch']:
            road_context += f"\n- Best launch spot: {r['best_launch']}"
        if weather['raining'] and r['wet_warning']:
            road_context += f"\n- WET ROAD WARNING: {r['wet_warning']}"

    # ── MUSIC CONTEXT ────────────────────
    music_context = ""
    if music_state['playing'] and music_state['current_song']:
        music_context = f"\n- Music: {music_state['current_song']} — energy: {music_state['energy']}"

    context = f"""
Truck data right now:
- Oil temp: {truck_state['oil_temp']}F — trend: {awareness['oil_trend']}
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
"""

    full_prompt = f"{SYSTEM_PROMPT}\n\n{context}\n{get_tier_label()} says: {user_input}\nArcher:"

    try:
        result = subprocess.run(
            ['ollama', 'run', 'llama3.2', full_prompt],
            capture_output=True,
            timeout=20,
            encoding='utf-8',
            errors='replace'
        )
        response = result.stdout.strip()
        if not response:
            return "Yeah."
        if 'Archer:' in response:
            response = response.split('Archer:')[-1].strip()
        response = response.strip('"').strip("'")
        return response
    except subprocess.TimeoutExpired:
        return "Give me a second."
    except Exception:
        return "Something is off on my end."

# ── PERSONAL BEST TRACKER ────────────────
def log_launch(time_0_60=None):
    personal_bests['launch_count'] += 1
    timestamp = datetime.now().strftime('%B %d %I:%M %p')
    if time_0_60:
        entry = {
            'time':    time_0_60,
            'date':    timestamp,
            'ethanol': truck_state['ethanol'],
            'oil':     truck_state['oil_temp'],
        }
        personal_bests['launch_log'].append(entry)
        if personal_bests['best_0_60'] is None or time_0_60 < personal_bests['best_0_60']:
            personal_bests['best_0_60'] = time_0_60
            save_state()
            return True
    save_state()
    return False

def show_launch_log():
    print("\n── LAUNCH LOG ───────────────────────────")
    if not personal_bests['launch_log']:
        print("  No launches logged yet.")
    else:
        for i, entry in enumerate(personal_bests['launch_log'], 1):
            print(f"  Launch {i}: {entry['time']}s — {entry['date']} — E{entry['ethanol']}% — Oil {entry['oil']}F")
    print(f"  Best 0-60: {personal_bests['best_0_60']}s")
    print(f"  Total launches: {personal_bests['launch_count']}")
    print("─────────────────────────────────────────\n")

# ── ROAD MEMORY ──────────────────────────
def set_road(road_key):
    global current_road
    if road_key in road_memory:
        current_road = road_key
        r = road_memory[road_key]
        print(f"\n[ROAD MEMORY] Now on: {r['name']}")
        if r['hazards']:
            print(f"[ROAD MEMORY] Hazards: {r['hazards']}")
        if r['best_launch']:
            print(f"[ROAD MEMORY] Best launch spot: {r['best_launch']}")
        print()
        save_state()
        return r
    return None

# ── OCTANE HELPER ────────────────────────
def set_octane(value, mode='AKI'):
    truck_state['octane']      = value
    truck_state['octane_mode'] = mode
    print(f"[ARDUINO] → OCTANE:{value}{mode}")
    print(f"[SMART KNOB] → Octane set to {value} {mode}")

# ── MUSIC AWARENESS ──────────────────────
def set_music(song, energy='medium'):
    music_state['current_song'] = song
    music_state['energy']       = energy
    music_state['playing']      = True
    if song in music_state['song_memories']:
        memory = music_state['song_memories'][song]
        print(f"[MUSIC MEMORY] Last time this played: {memory}")
    if energy == 'hype':
        print("[ARCHER] Music is hype. Exhaust suggestion — want it open?")
    elif energy == 'calm':
        print("[ARCHER] Good late night track.")

def link_song_to_moment(song, moment):
    music_state['song_memories'][song] = moment
    print(f"[MUSIC MEMORY] Linked '{song}' to: {moment}")
    save_state()

# ── CASUAL CONVERSATION ──────────────────
def casual_monitor():
    global last_casual
    time.sleep(30)
    casual_comments = [
        lambda: f"Oil has been sitting at {truck_state['oil_temp']}. Holding steady." if truck_state['rpm'] > 1000 else None,
        lambda: f"E85 at {truck_state['ethanol']} percent. Power map is active." if truck_state['ethanol'] > 80 else None,
        lambda: "Sunday morning. Best time to be out here." if datetime.now().strftime('%A') == 'Sunday' else None,
        lambda: "Road is clear." if truck_state['speed'] > 40 else None,
        lambda: f"Been running at {truck_state['rpm']} RPM for a while. Everything is happy." if truck_state['rpm'] > 2000 else None,
        lambda: "Late night. Best time to drive." if int(datetime.now().strftime('%H')) >= 22 else None,
        lambda: "Monday. Let's make it worth it." if datetime.now().strftime('%A') == 'Monday' and int(datetime.now().strftime('%H')) < 10 else None,
        lambda: f"It is {weather['temp']} degrees out. {weather['condition'].title()}." if weather['condition'] not in ['unknown', 'clear'] else None,
    ]
    while True:
        time.sleep(casual_interval)
        now = time.time()
        if tier_state['current'] == 1 and now - last_casual > casual_interval:
            random.shuffle(casual_comments)
            for comment_fn in casual_comments:
                comment = comment_fn()
                if comment:
                    time.sleep(2)
                    print(f"\n[ARCHER] {comment}")
                    speak(comment)
                    print("\n[YOU] ", end='', flush=True)
                    last_casual = now
                    break

# ── SHOW MODES ───────────────────────────
def run_flex():
    print("[ARDUINO] → SHOW:FLEX")
    print("[ARDUINO] → All amber LEDs pulse — warning")
    print("[ARDUINO] → EXHAUST:80")
    for i in range(1, 5):
        print(f"[ARDUINO] → Corner {i} rising")
        time.sleep(0.3)
    print("[ARDUINO] → Rev sequence 3000 RPM")
    time.sleep(0.4)
    print("[ARDUINO] → Rev sequence 4000 RPM")
    time.sleep(0.4)
    print("[ARDUINO] → Rev sequence 5000 RPM")
    time.sleep(0.6)
    print("[ARDUINO] → EXHAUST:30")
    print("[ARDUINO] → Lights return to normal")
    print("[ARDUINO] → Amber LEDs off")

def run_drunk():
    print("[ARDUINO] → SHOW:DRUNK")
    print("[ARDUINO] → Front left bag deflates")
    time.sleep(0.5)
    print("[ARDUINO] → Front right deflates — truck wobbles")
    time.sleep(0.5)
    print("[ARDUINO] → Rear left drops")
    time.sleep(0.5)
    print("[ARDUINO] → Rear right drops")
    time.sleep(0.5)
    print("[ARDUINO] → Random wobble pattern — 20 seconds")
    time.sleep(1)
    print("[ARDUINO] → All bags inflate — snaps back level")
    print("[ARDUINO] → DIC: I'M FINE")

def run_sneeze():
    print("[ARDUINO] → SHOW:SNEEZE")
    print("[ARDUINO] → PA horn buildup sound")
    time.sleep(0.8)
    print("[ARDUINO] → All four bags dump simultaneously")
    print("[ARDUINO] → EXHAUST:100 — blast")
    print("[ARDUINO] → Horn — one blast")
    print("[ARDUINO] → Lights flash white")
    time.sleep(0.5)
    print("[ARDUINO] → Everything returns to normal")
    print("[ARDUINO] → DIC: BLESS YOU")

def run_stalker():
    print("[ARDUINO] → SHOW:STALKER")
    print("[ARDUINO] → Underbody lights rotate — following direction of movement")
    print("[ARDUINO] → Single wheel well light brightens slowly")
    print("[ARDUINO] → PA horn whispers: I can see you")
    time.sleep(1)
    print("[ARDUINO] → If they stop — all lights off — complete darkness")
    time.sleep(0.8)
    print("[ARDUINO] → Train horn — full blast")
    print("[ARDUINO] → DIC: GOT YOU")

def run_existential():
    print("[ARDUINO] → SHOW:EXISTENTIAL_CRISIS")
    print("[ARDUINO] → All lights off")
    print("[ARDUINO] → Engine idles down")
    print("[ARDUINO] → Sad violin through all speakers — full volume")
    print("[ARDUINO] → Subwoofers — deep ominous bass")
    print("[ARDUINO] → DIC scrolling: WHAT IS EVEN THE POINT")
    time.sleep(0.5)
    print("[ARDUINO] → DIC: 408 CUBIC INCHES")
    time.sleep(0.5)
    print("[ARDUINO] → DIC: AND FOR WHAT")
    time.sleep(0.5)
    print("[ARDUINO] → DIC: I COULD HAVE BEEN A MINIVAN")
    time.sleep(1)
    print("[ARDUINO] → Bass drop through subwoofers — felt in seats")
    print("[ARDUINO] → ALL LIGHTS BLAST ON")
    print("[ARDUINO] → Engine roars — EXHAUST:100")
    print("[ARDUINO] → Train horn — five blasts")
    print("[ARDUINO] → Truck rises to max height instantly")
    print("[ARDUINO] → DIC: JUST KIDDING — LET'S GO")

def run_negotiations():
    print("[ARDUINO] → SHOW:NEGOTIATIONS")
    print("[ARDUINO] → Truck drops to lowest height slowly")
    print("[ARDUINO] → PA horn: I have a particular set of skills")
    time.sleep(0.8)
    print("[ARDUINO] → Exhaust cutouts crack open — low ominous rumble")
    print("[ARDUINO] → Underbody lights go dark red")
    time.sleep(0.8)
    print("[ARDUINO] → PA horn: Skills I have acquired over a very long build")
    time.sleep(0.8)
    print("[ARDUINO] → Truck rises to full height — fast")
    print("[ARDUINO] → Cutouts open fully — engine blips to 4500 RPM")
    print("[ARDUINO] → PA horn: What I do have is a very specific truck")
    time.sleep(0.5)
    print("[ARDUINO] → Train horn — one long blast")
    print("[ARDUINO] → DIC: GOOD LUCK")

def run_goodbye():
    print("[ARDUINO] → SHOW:GOODBYE")
    print("[ARDUINO] → Engine idles for 10 seconds")
    time.sleep(1)
    print("[ARDUINO] → Interior lights slowly fade out")
    print("[ARDUINO] → Underbody lights slowly fade out")
    print("[ARDUINO] → Truck lowers to garage height")
    time.sleep(0.5)
    print("[ARDUINO] → Single quiet exhaust blip — like a sigh")
    print("[ARDUINO] → Engine shuts off")
    print("[ARDUINO] → Single underbody light pulses once — slowly — then dark")
    print("[ARDUINO] → DIC: SEE YOU TOMORROW")
    print("[ARDUINO] → Alarm arms")

def run_motivational():
    print("[ARDUINO] → SHOW:MOTIVATIONAL_SPEAKER")
    print("[ARDUINO] → Rocky training music through all speakers — starts quiet")
    print("[ARDUINO] → Music builds slowly")
    time.sleep(1)
    print("[ARDUINO] → At peak — engine remote starts")
    print("[ARDUINO] → EXHAUST:100 — exhaust note fills garage")
    print("[ARDUINO] → DIC: LET'S GO CHAMP")
    time.sleep(0.5)
    print("[ARDUINO] → Music fades — playlist resumes")

def run_karen():
    print("[ARDUINO] → SHOW:KAREN")
    print("[ARDUINO] → PA horn pre-recorded voice: Can I speak to your manager?")
    print("[ARDUINO] → Interior lights flash white")
    print("[ARDUINO] → Drone XC starts recording")
    print("[ARDUINO] → DIC: I NEED TO SPEAK TO YOUR MANAGER")
    time.sleep(1)
    print("[ARDUINO] → Train horn — three full blasts")
    print("[ARDUINO] → DIC: I SAID GOOD DAY")

# ── SMART KNOB ───────────────────────────
smart_knob = {
    'menu':     'main',
    'position': 0,
    'open':     False,
}

MENUS = {
    'main': [
        'Exhaust Control',
        'Octane Setting',
        'Drive Mode',
        'Show Modes',
        'Lighting',
        'System Status',
        'Close Menu',
    ],
    'exhaust': [
        'Closed — 0%',
        'Neighborhood — 15%',
        'Cruise — 30%',
        'Street — 50%',
        'Sport — 75%',
        'Full Open — 100%',
        'Back',
    ],
    'octane': [
        '87 AKI',
        '91 AKI',
        '93 AKI',
        'E85',
        '95 RON',
        '98 RON',
        '100 RON',
        'Back',
    ],
    'drive': [
        'Comfort',
        'Sport',
        'Tow',
        'Weather',
        'Ghost',
        'Back',
    ],
    'show': [
        'Flex',
        'Slam',
        'Drunk',
        'Sneeze',
        'Stalker',
        'Existential Crisis',
        'Negotiations',
        'Motivational',
        'Goodbye',
        'Karen',
        'Therapy',
        'Back',
    ],
    'lighting': [
        'Underglow On',
        'Underglow Off',
        'Wheel Wells On',
        'Wheel Wells Off',
        'Interior Dim',
        'Interior Full',
        'All Off',
        'Back',
    ],
}

def show_knob_menu():
    menu  = smart_knob['menu']
    pos   = smart_knob['position']
    items = MENUS.get(menu, MENUS['main'])
    print(f"\n╔── SMART KNOB — {menu.upper()} {'─' * max(0, 20 - len(menu))}╗")
    for i, item in enumerate(items):
        marker = " ► " if i == pos else "   "
        print(f"║{marker}{item}")
    print(f"╚{'─' * 30}╝")
    print("  knob up / knob down / select / back\n")

def knob_up():
    menu  = smart_knob['menu']
    items = MENUS.get(menu, MENUS['main'])
    smart_knob['position'] = (smart_knob['position'] - 1) % len(items)
    show_knob_menu()

def knob_down():
    menu  = smart_knob['menu']
    items = MENUS.get(menu, MENUS['main'])
    smart_knob['position'] = (smart_knob['position'] + 1) % len(items)
    show_knob_menu()

def knob_select():
    menu  = smart_knob['menu']
    pos   = smart_knob['position']
    items = MENUS.get(menu, MENUS['main'])
    item  = items[pos].lower()

    if menu == 'main':
        if 'exhaust' in item:
            smart_knob['menu'] = 'exhaust'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""   # was None — now silent
        elif 'octane' in item:
            smart_knob['menu'] = 'octane'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""
        elif 'drive' in item:
            smart_knob['menu'] = 'drive'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""
        elif 'show' in item:
            smart_knob['menu'] = 'show'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""
        elif 'lighting' in item:
            smart_knob['menu'] = 'lighting'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""
        elif 'status' in item:
            print_status()
            return ""
        elif 'close' in item:
            smart_knob['open'] = False
            return "Menu closed."

    elif menu == 'exhaust':
        if 'back' in item:
            smart_knob['menu'] = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return None
        pct_map = {
            '0%': 0, 'closed': 0,
            '15%': 15, 'neighborhood': 15,
            '30%': 30, 'cruise': 30,
            '50%': 50, 'street': 50,
            '75%': 75, 'sport': 75,
            '100%': 100, 'full': 100,
        }
        for key, val in pct_map.items():
            if key in item:
                truck_state['exhaust'] = val
                print(f"[ARDUINO] → EXHAUST:{val}")
                return f"Exhaust at {val} percent."

    elif menu == 'octane':
        if 'back' in item:
            smart_knob['menu'] = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return None
        elif 'e85' in item:
            truck_state['ethanol'] = 85
            print("[ARDUINO] → ETHANOL:85")
            return "Full E85. Power map loaded."
        elif '87' in item:
            set_octane(87, 'AKI')
            return "Loading the disappointment map."
        elif '91' in item:
            set_octane(91, 'AKI')
            return "91 AKI confirmed."
        elif '93' in item:
            set_octane(93, 'AKI')
            return "93 AKI. Full gasoline map loaded."
        elif '95 ron' in item:
            set_octane(95, 'RON')
            return "95 RON. 90 AKI equivalent. Tune adjusted."
        elif '98 ron' in item:
            set_octane(98, 'RON')
            return "98 RON. 93 AKI equivalent. Tune adjusted."
        elif '100 ron' in item:
            set_octane(100, 'RON')
            return "100 RON. Race fuel map active."

    elif menu == 'drive':
        if 'back' in item:
            smart_knob['menu'] = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return None
        elif 'comfort' in item:
            truck_state['exhaust'] = 15
            truck_state['tc_on']   = True
            print("[ARDUINO] → EXHAUST:15")
            print("[ARDUINO] → TC_LOCK")
            return "Comfort mode. Everything soft."
        elif 'sport' in item:
            truck_state['exhaust'] = 65
            print("[ARDUINO] → EXHAUST:65")
            print("[ARDUINO] → TC_RELEASE")
            return "Sport mode. Ready."
        elif 'tow' in item:
            truck_state['exhaust'] = 30
            truck_state['tc_on']   = True
            print("[ARDUINO] → EXHAUST:30")
            print("[ARDUINO] → TC_LOCK")
            return "Tow mode. Suspension leveled."
        elif 'weather' in item:
            truck_state['exhaust'] = 10
            truck_state['tc_on']   = True
            print("[ARDUINO] → EXHAUST:10")
            print("[ARDUINO] → TC_LOCK")
            return f"Weather mode. {weather['temp']}F outside. TC locked on."
        elif 'ghost' in item:
            truck_state['ghost_mode'] = True
            truck_state['exhaust']    = 0
            print("[ARDUINO] → EXHAUST:0")
            print("[ARDUINO] → UNDERBODY:0")
            return "Ghost mode. Going invisible."

    elif menu == 'show':
        if 'back' in item:
            smart_knob['menu'] = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return None
        return handle_command(items[pos].lower())

    elif menu == 'lighting':
        if 'back' in item:
            smart_knob['menu'] = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return None
        elif 'underglow on' in item:
            print("[ARDUINO] → UNDERBODY:255")
            return "Underglow on."
        elif 'underglow off' in item:
            print("[ARDUINO] → UNDERBODY:0")
            return "Underglow off."
        elif 'wheel wells on' in item:
            print("[ARDUINO] → WHEELWELL:255")
            return "Wheel wells on."
        elif 'wheel wells off' in item:
            print("[ARDUINO] → WHEELWELL:0")
            return "Wheel wells off."
        elif 'interior dim' in item:
            print("[ARDUINO] → INTERIOR:80")
            return "Interior dimmed."
        elif 'interior full' in item:
            print("[ARDUINO] → INTERIOR:255")
            return "Interior full brightness."
        elif 'all off' in item:
            print("[ARDUINO] → UNDERBODY:0")
            print("[ARDUINO] → WHEELWELL:0")
            print("[ARDUINO] → INTERIOR:0")
            return "All lights off."

    return None

# ── DIRECT COMMANDS ──────────────────────
def handle_command(text):
    t = text.lower().strip()

    # ── TIER SWITCHING ───────────────────
    if any(x in t for x in ['tier 1', 'tier1', 'tiers 1', 'tiers1', 'switch to ayden', 'owner mode']):
        tier_state['current'] = 1
        return "Tier 1. Welcome back."

    if any(x in t for x in ['tier 2', 'tier2', 'tiers 2', 'tiers2', 'girlfriend mode', 'passenger mode']):
        tier_state['current'] = 2
        return "Tier 2 active."

    if any(x in t for x in ['tier 3', 'tier3', 'tiers 3', 'tiers3', 'family mode']):
        tier_state['current'] = 3
        return "Tier 3 active."

    if any(x in t for x in ['tier 4', 'tier4', 'tiers 4', 'tiers4', 'valet mode']):
        tier_state['current'] = 4
        return "Valet mode. Monitoring everything."

    # ── SHOW MODES ───────────────────────
    if any(x in t for x in ['flex', 'show mode', 'car show']):
        truck_state['exhaust'] = 80
        run_flex()
        return "Alright. Watch this."

    if 'slam' in t:
        truck_state['exhaust'] = 100
        print("[ARDUINO] → SHOW:SLAM")
        return "Dropping it."

    if 'drunk' in t:
        run_drunk()
        return "Activating the Drunk. Try to look casual."

    if 'sneeze' in t:
        run_sneeze()
        return "Gesundheit."

    if 'stalker' in t:
        run_stalker()
        return "Going dark."

    if any(x in t for x in ['existential', 'existential crisis']):
        run_existential()
        return "Alright. I will get the violin."

    if 'negotiation' in t:
        run_negotiations()
        return "I have a particular set of skills."

    if 'goodbye' in t or ('good' in t and 'night' in t and 'show' in t):
        run_goodbye()
        return "That is enough for today."

    if 'motivational' in t or 'motivate me' in t:
        run_motivational()
        return "Let's go champ."

    if 'karen' in t:
        run_karen()
        return "Can I speak to your manager."

    if any(x in t for x in ['therapy', 'need a minute', 'rough day']):
        print("[ARDUINO] → SHOW:THERAPY")
        print("[ARDUINO] → Seat heat ON all zones")
        print("[ARDUINO] → Interior lights warm amber")
        print("[ARDUINO] → Soft piano through all speakers")
        return "Seat heat is on. Take your time."

# ── SMART KNOB ───────────────────────
    if any(x in t for x in ['knob menu', 'open menu', 'menu']):
        smart_knob['menu']     = 'main'
        smart_knob['position'] = 0
        smart_knob['open']     = True
        show_knob_menu()
        return "Main menu open."

    if smart_knob['open']:
        if any(x in t for x in ['knob up', 'up', 'scroll up', 'previous']):
            knob_up()
            return ""  # empty string — prints nothing — speaks nothing

        if any(x in t for x in ['knob down', 'down', 'scroll down', 'next']):
            knob_down()
            return ""  # empty string — prints nothing — speaks nothing

        if any(x in t for x in ['select', 'choose', 'knob select', 'press', 'enter']):
            result = knob_select()
            return result  # only speak when something is actually selected

        if any(x in t for x in ['back', 'go back', 'knob back', 'cancel']):
            if smart_knob['menu'] == 'main':
                smart_knob['open'] = False
                return "Menu closed."
            smart_knob['menu']     = 'main'
            smart_knob['position'] = 0
            show_knob_menu()
            return ""  # silent navigation

    # ── OCTANE QUERY ─────────────────────
    if 'octane' in t and any(x in t for x in ['what', 'current', 'which', 'how much', 'tell me', 'know', 'running', 'in the', 'check']):
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."

    if t in ['octane', 'octane?', 'what octane']:
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."

    # ── E85 FILL ─────────────────────────
    if 'e85' in t and any(x in t for x in ['fill', 'putting', 'about to', 'just filled']):
        truck_state['ethanol'] = 85
        print("[ARDUINO] → ETHANOL:85")
        print("[SMART KNOB] → E85 map active")
        return "Full E85. Power map loaded. About time."

    # ── OCTANE SETTING ───────────────────
    if any(x in t for x in ['octane', 'fuel grade', 'ron', 'filling', 'fill up', 'about to fill']):
        if any(x in t for x in ['ron', 'international', 'europe']):
            for grade in sorted(RON_OCTANE_GRADES, reverse=True):
                if str(grade) in t:
                    aki_equiv = round(grade * 0.95)
                    set_octane(grade, 'RON')
                    return f"{grade} RON. {aki_equiv} AKI equivalent. Tune adjusted."
        for grade in sorted(US_OCTANE_GRADES, reverse=True):
            if str(grade) in t:
                set_octane(grade, 'AKI')
                if grade == 87:
                    return "Loading the disappointment map."
                elif grade == 93:
                    return "93 AKI. Full gasoline map loaded."
                else:
                    return f"{grade} AKI confirmed."
        return "What octane?"

    if any(x in t for x in ['fill', 'putting', 'about to', 'just filled']):
        for grade in sorted(US_OCTANE_GRADES, reverse=True):
            if str(grade) in t:
                set_octane(grade, 'AKI')
                if grade == 87:
                    return "Loading the disappointment map."
                elif grade == 93:
                    return "93 AKI. Full gasoline map loaded."
                else:
                    return f"{grade} AKI confirmed."

    if any(x in t for x in ['fill', 'putting', 'about to', 'just filled']):
        for grade in sorted(US_OCTANE_GRADES, reverse=True):
            if str(grade) in t:
                set_octane(grade, 'AKI')
                if grade == 87:
                    return "Loading the disappointment map."
                elif grade == 93:
                    return "93 AKI. Full gasoline map loaded."
                else:
                    return f"{grade} AKI confirmed."

    # ── EXHAUST ──────────────────────────
    if any(x in t for x in ['open exhaust', 'open it up', 'cut it open', 'open the exhaust']):
        truck_state['exhaust'] = 100
        print("[ARDUINO] → EXHAUST:100")
        return "Opening it up."

    if any(x in t for x in ['close exhaust', 'quiet down', 'close it', 'close the exhaust']):
        truck_state['exhaust'] = 0
        print("[ARDUINO] → EXHAUST:0")
        return "Closing it down."

    if t == 'exhaust' or t == 'exhaust level' or t== 'exhaust percent':
        return f"Exhaust is at {truck_state['exhaust']} percent."

    if 'exhaust' in t and any(x in t for x in ['50', 'half', 'halfway']):
        truck_state['exhaust'] = 50
        print("[ARDUINO] → EXHAUST:50")
        return "Exhaust at 50 percent."

    # ── TRACTION CONTROL ─────────────────
    if any(x in t for x in ['tc off', 'traction off', 'kill tc']):
        truck_state['tc_on']     = False
        truck_state['tc_locked'] = False
        print("[ARDUINO] → TC_OFF")
        return "TC off. Road looks dry. We are good."

    if any(x in t for x in ['tc on', 'traction on', 'lock tc']):
        truck_state['tc_on']     = True
        truck_state['tc_locked'] = True
        print("[ARDUINO] → TC_LOCK")
        return "TC on."

    # ── GHOST MODE ───────────────────────
    if 'ghost' in t and 'off' not in t:
        truck_state['ghost_mode'] = True
        truck_state['exhaust']    = 0
        print("[ARDUINO] → EXHAUST:0")
        print("[ARDUINO] → UNDERBODY:0")
        print("[ARDUINO] → GROUND:0")
        return "Going invisible."

    if any(x in t for x in ['ghost off', 'turn ghost off']):
        truck_state['ghost_mode'] = False
        print("[ARDUINO] → GHOST_OFF")
        return "Back to normal."

    # ── WEATHER ──────────────────────────
    if any(x in t for x in ['weather', 'how cold', 'how hot', 'raining', 'outside temp', 'temperature outside']):
        data = get_weather()
        weather.update(data)
        temp      = weather['temp']
        condition = weather['condition']
        if weather['snowing']:
            return f"{temp}F and snowing in Salem. 4WD is ready. TC stays on."
        elif weather['raining']:
            return f"{temp}F and raining. TC locked on. Road will be slick."
        elif weather['freezing']:
            return f"{temp}F. Everything is tighter today. Give me a minute to warm up."
        elif temp > 90:
            return f"{temp}F outside. Heat is going to build faster today. Keeping an eye on it."
        elif temp < 50:
            return f"{temp}F. Cold start territory. Oil needs a minute before we push it."
        else:
            return f"{temp}F in Salem. {condition.title()}. Good day to be out."

    # ── ROAD MEMORY ──────────────────────
    if 'highway 72' in t and 'rolla' in t:
        r = set_road('rolla_highway')
        if r:
            return "Murphy USA run. 31 miles. Mile 4 is your best launch spot."
    elif 'highway 72' in t and 'north' in t:
        r = set_road('highway_72_north')
        if r:
            return "Highway 72 North. Smooth at mile 4. Railroad crossing at mile 8."
    elif 'highway 72' in t and 'south' in t:
        r = set_road('highway_72_south')
        if r:
            return "72 South. Rolling hills. Watch the blind crests."
    elif 'highway 72' in t:
        r = set_road('highway_72_north')
        if r:
            return "Highway 72. Smooth at mile 4. Railroad crossing at mile 8 — heads up."
    elif 'highway 19' in t and ('south' in t or 'eminence' in t):
        r = set_road('highway_19_south')
        if r:
            return "19 South toward Eminence. Winding Ozark roads. Not a launch road."
    elif 'highway 19' in t:
        r = set_road('highway_19_north')
        if r:
            return "Highway 19 North. Good straight past the creek bridge."
    elif 'highway 32' in t and 'west' in t:
        r = set_road('highway_32_west')
        if r:
            return "32 West. Open road. Railroad crossing at mile 3 — watch it when wet."
    elif 'highway 32' in t:
        r = set_road('highway_32_east')
        if r:
            return "32 East. Fast road. Long straights past city limits."
    elif 'county' in t and '19' in t:
        r = set_road('county_19')
        if r:
            return "County 19. Gravel after mile 2. Take it easy."
    elif 'downtown' in t:
        r = set_road('downtown_salem')
        if r:
            return "Downtown Salem. Keeping exhaust down."
    elif 'industrial' in t:
        r = set_road('industrial_park')
        if r:
            return "Industrial park. Empty on weekends. Good launch surface."
    elif 'school road' in t:
        r = set_road('salem_school_road')
        if r:
            return "School road. School zone during the week. Keep it clean."
    elif 'big piney' in t or 'piney' in t:
        r = set_road('big_piney_river_road')
        if r:
            return "Big Piney road. Narrow and winding. Not a speed road."
    elif 'fort leonard' in t or 'fort wood' in t:
        r = set_road('fort_leonard_wood')
        if r:
            return "Route to Fort Wood. Highway quality. Good on-ramp straight."
    elif 'dent county' in t:
        r = set_road('dent_county_road')
        if r:
            return "Dent County back road. Old pavement. Empty but rough."
    elif 'backroad' in t or 'back road' in t:
        r = set_road('backroad')
        if r:
            return "Back road. Best stretch is after the curve. Watch for deer after dark."
    elif 'rolla' in t:
        r = set_road('rolla_highway')
        if r:
            return "Murphy USA run. 31 miles north. Mile 4 straight is smooth."

    if 'road condition' in t or 'how is the road' in t or 'road memory' in t:
        if current_road and current_road in road_memory:
            r = road_memory[current_road]
            return f"{r['name']}. {r['notes']}"
        return "No road logged yet. Tell me where we are."

    if 'best launch' in t or 'where to launch' in t or 'bestlaunch' in t:
        if current_road and current_road in road_memory:
            r = road_memory[current_road]
            if r['best_launch']:
                return f"Best launch spot on this road — {r['best_launch']}."
            return "No clean launch spot on this road."
        return "No road loaded. Tell me where we are."

    if 'hazards' in t or 'watch out' in t or 'anything ahead' in t:
        if current_road and current_road in road_memory:
            r = road_memory[current_road]
            if r['hazards']:
                return f"Watch for {r['hazards']}."
            return "Nothing specific logged for this road."
        return "No road loaded."

    # ── MUSIC AWARENESS ──────────────────
    if any(x in t for x in ['playing', 'song is', 'now playing']):
        song = None
        for keyword in ['playing', 'song is', 'now playing']:
            if keyword in t:
                parts = t.split(keyword)
                if len(parts) > 1 and parts[1].strip():
                    song = parts[1].strip()
                    break
        if song and len(song) > 1:
            if any(x in song for x in ['hype', 'hard', 'fast', 'rage', 'aggressive']):
                energy = 'hype'
            elif any(x in song for x in ['calm', 'slow', 'chill', 'quiet', 'soft']):
                energy = 'calm'
            else:
                energy = 'medium'
            set_music(song, energy)
            return f"Got it. {song.title()} loaded."

    if 'music off' in t or 'stop music' in t:
        music_state['playing']      = False
        music_state['current_song'] = None
        return "Music noted as off."

    if any(x in t for x in ['remember this song', 'link song', 'save this moment']):
        if music_state['current_song']:
            moment = f"{datetime.now().strftime('%B %d')} — {truck_state['speed']} mph — E{truck_state['ethanol']}%"
            link_song_to_moment(music_state['current_song'], moment)
            return f"Linked {music_state['current_song'].title()} to this moment."
        return "No song playing right now."

    # ── PERSONAL BEST ────────────────────
    if any(x in t for x in ['launch log', 'best time', 'personal best', 'show launches']):
        show_launch_log()
        return "Launch log above."

    if any(x in t for x in ['log launch', 'log a launch', 'that was']):
        time_val = None
        words = t.split()
        for word in words:
            try:
                val = float(word)
                if 2.0 < val < 10.0:
                    time_val = val
                    break
            except:
                pass
        if time_val:
            new_best = log_launch(time_val)
            if new_best:
                return f"{time_val} seconds. New best. Write that down."
            else:
                return f"{time_val} seconds logged. Best is still {personal_bests['best_0_60']}."
        else:
            log_launch()
            return f"Launch logged. Total: {personal_bests['launch_count']}."

    # ── LIGHTS ───────────────────────────
    if 'bed light' in t and any(x in t for x in ['on', 'open']):
        truck_state['bed_lights'] = True
        print("[ARDUINO] → BED_ON")
        return "Bed lights on."

    if 'bed light' in t and any(x in t for x in ['off', 'close']):
        truck_state['bed_lights'] = False
        print("[ARDUINO] → BED_OFF")
        return "Bed lights off."

    if 'hood light' in t and 'on' in t:
        truck_state['hood_lights'] = True
        print("[ARDUINO] → HOOD_ON")
        return "Hood lights on."

    # ── SERVICE MODE ─────────────────────
    if 'service mode' in t:
        print("[ARDUINO] → SERVICE_MODE_ON")
        print("[ARDUINO] → All automated systems locked")
        return "Service mode active. Nothing will move unless you tell me."

    # ── COOLING ──────────────────────────
    if any(x in t for x in ['cool down', 'aux pump', 'cooling']):
        truck_state['cool_on'] = True
        print("[ARDUINO] → COOL_ON")
        return "Aux pump on. Cooling down."

    # ── HIGH IDLE ────────────────────────
    if any(x in t for x in ['high idle', 'idle up']):
        truck_state['idle_on'] = True
        truck_state['rpm']     = 1500
        print("[ARDUINO] → IDLE_ON:1500")
        return "High idle active. 1500 RPM."

    # ── PERFORMANCE ──────────────────────
    if any(x in t for x in ["let's run", "run it", "push it", "launch"]):
        truck_state['rpm']   = 5500
        truck_state['speed'] = 60
        truck_state['boost'] = 12
        print("[ARDUINO] → TC_OFF")
        print("[ARDUINO] → EXHAUST:100")
        return "Ready. Let's go."

    if any(x in t for x in ['slow down', 'cruising', 'back off']):
        truck_state['rpm']   = 1800
        truck_state['speed'] = 45
        truck_state['boost'] = 0
        return "Backing off."

    # ── STATUS QUERIES ───────────────────
    if 'oil' in t:
        temp = truck_state['oil_temp']
        if temp > 230:
            return f"Oil is at {temp}. Getting warm — back it down."
        return f"Oil is at {temp}. Holding steady."

    if any(x in t for x in ['ethanol', 'how much ethanol', 'e85 level']):
        eth = truck_state['ethanol']
        if eth > 80:
            return f"E85 at {eth} percent. Full power map is active."
        elif eth > 50:
            return f"Ethanol at {eth} percent. Still in the power map."
        else:
            return f"Ethanol is down to {eth} percent. Murphy USA in Rolla when you get a chance."

    if any(x in t for x in ['battery', 'voltage']):
        v = truck_state['battery_main']
        return f"Main battery at {v} volts. Looking good."

    if any(x in t for x in ['boost', 'psi']):
        b = truck_state['boost']
        return f"Boost at {b} PSI right now."

    if any(x in t for x in ['what octane', 'octane in', 'octane setting', 'current octane', 'octane level', 'what is the octane', 'octane are']):
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."
    
    if 'octane' in t and any(x in t for x in ['what', 'current', 'which', 'how much', 'tell me', 'know']):
        return f"Octane is at {truck_state['octane']} {truck_state['octane_mode']}."

    if 'what song' in t or ('music' in t and 'what' in t):
        if music_state['playing']:
            return f"{music_state['current_song'].title()}. Energy is {music_state['energy']}."
        return "Nothing playing right now."

    if 'status' in t:
        print_status()
        return "Status printed above."
    
    if t == 'speed' or 'how fast' in t or 'current speed' in t:
        spd = truck_state['speed']
        if spd == 0:
            return "Sitting still right now."
        elif spd < 35:
            return f"{spd} mph. Taking it easy."
        elif spd < 60:
            return f"{spd} mph. Cruising."
        else:
            return f"{spd} mph. Moving"

    return None

# ── PRINT STATUS ─────────────────────────
def print_status():
    print("\n── TRUCK STATE ──────────────────────────")
    print(f"  Oil Temp:    {truck_state['oil_temp']}°F")
    print(f"  Coolant:     {truck_state['coolant_temp']}°F")
    print(f"  RPM:         {truck_state['rpm']}")
    print(f"  Speed:       {truck_state['speed']} mph")
    print(f"  Ethanol:     {truck_state['ethanol']}%")
    print(f"  Boost:       {truck_state['boost']} PSI")
    print(f"  Battery:     {truck_state['battery_main']}V")
    print(f"  Exhaust:     {truck_state['exhaust']}%")
    print(f"  Octane:      {truck_state['octane']} {truck_state['octane_mode']}")
    print(f"  TC:          {'LOCKED' if truck_state['tc_locked'] else 'ON' if truck_state['tc_on'] else 'OFF'}")
    print(f"  Ghost Mode:  {'ON' if truck_state['ghost_mode'] else 'OFF'}")
    print(f"  Mood:        {get_mood()}")
    print(f"  Tier:        {tier_state['current']} — {get_tier_label()}")
    print(f"  Road:        {road_memory[current_road]['name'] if current_road else 'None logged'}")
    print(f"  Music:       {music_state['current_song'] if music_state['playing'] else 'Off'}")
    print(f"  Weather:     {weather['temp']}F — {weather['condition']}")
    if personal_bests['best_0_60']:
        print(f"  Best 0-60:   {personal_bests['best_0_60']}s")
    else:
        print(f"  Best 0-60:   None logged")
    print(f"  Launches:    {personal_bests['launch_count']}")
    print(f"  Time:        {datetime.now().strftime('%I:%M %p — %A')}")
    print("─────────────────────────────────────────\n")

# ── SAFETY MONITOR ───────────────────────
def safety_monitor():
    last_oil_warning     = 0
    last_battery_warning = 0
    last_ethanol_warning = 0
    last_quality_warning = 0

    while True:
        now  = time.time()
        oil  = truck_state['oil_temp']
        v    = truck_state['battery_main']
        eth  = truck_state['ethanol']
        boost = truck_state['boost']
        quality = awareness['drive_quality']

        # ── OIL TEMP ─────────────────────
        if oil > 235 and now - last_oil_warning > 30:
            msg = f"Oil is at {oil}. I had to step in. Closing cutouts."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            truck_state['exhaust']   = 0
            truck_state['tc_locked'] = True
            print("[ARDUINO] → EXHAUST:0")
            print("[ARDUINO] → TC_LOCK")
            print("[ARDUINO] → COOL_ON")
            print("[ARDUINO] → AMBER LOCK")
            last_oil_warning = now

        elif oil > 220 and now - last_oil_warning > 60:
            msg = f"Oil is climbing — {oil} degrees. Keep an eye on it."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            print("[YOU] ", end='', flush=True)
            last_oil_warning = now

        # ── OIL RECOVERED ────────────────
        elif oil < 210 and truck_state['tc_locked'] and truck_state['cool_on']:
            msg = "Oil is back down. Everything is yours."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            truck_state['tc_locked'] = False
            truck_state['cool_on']   = False
            print("[ARDUINO] → TC_RELEASE")
            print("[ARDUINO] → COOL_OFF")
            print("[ARDUINO] → AMBER_OFF")
            print("[YOU] ", end='', flush=True)

        # ── BATTERY ──────────────────────
        if v < 11.8 and now - last_battery_warning > 60:
            msg = "Battery dropping. Connecting auxiliary."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            print("[ARDUINO] → AUXBAT_ON")
            print("[YOU] ", end='', flush=True)
            last_battery_warning = now

        # ── ETHANOL UNDER BOOST ──────────
        if eth < 30 and boost > 5 and now - last_ethanol_warning > 120:
            msg = "Low ethanol under boost. Knock risk is real right now."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            print("[YOU] ", end='', flush=True)
            last_ethanol_warning = now

        # ── DRIVE QUALITY ────────────────
        if quality < 60 and now - last_quality_warning > 300:
            msg = "Drive quality is down. Lot of hard events this session."
            print(f"\n[ARCHER] {msg}")
            speak(msg)
            print("[YOU] ", end='', flush=True)
            last_quality_warning = now

        time.sleep(5)

# ── MAIN ─────────────────────────────────
def main():
    print("╔══════════════════════════════════════╗")
    print("║        ARCHER — STARTING UP          ║")
    print("║   2006 GMC Sierra 2500HD — Built     ║")
    print("║       Salem Missouri  2026           ║")
    print("╚══════════════════════════════════════╝")
    print()
    print("Running in simulation mode — no hardware needed")
    print()
    print("Commands:")
    print("  SHOW MODES:  flex / slam / drunk / sneeze / stalker")
    print("               existential crisis / negotiations / goodbye")
    print("               motivational / karen / therapy")
    print("  EXHAUST:     open exhaust / close exhaust / exhaust 50")
    print("  OCTANE:      about to fill 87 / 93 octane / 98 RON / e85 fill")
    print("  TC:          tc off / tc on")
    print("  ROADS:       highway 72 / highway 19 / highway 32 / county 19")
    print("               downtown / backroad / industrial park / rolla")
    print("               best launch / hazards / road condition")
    print("  MUSIC:       playing [song name] / remember this song")
    print("  LAUNCHES:    log launch 3.8 / personal best / launch log")
    print("  WEATHER:     weather / temperature outside")
    print("  TIERS:       tier 1 / tier 2 / tier 3 / tier 4")
    print("  OTHER:       ghost mode / service mode / status / quit")
    print("  or just talk — Archer will respond")
    print()
    print("─" * 42)
    print()

    threading.Thread(target=tts_worker,      daemon=True).start()
    threading.Thread(target=update_awareness, daemon=True).start()
    threading.Thread(target=safety_monitor,  daemon=True).start()
    threading.Thread(target=casual_monitor,  daemon=True).start()
    threading.Thread(target=weather_monitor, daemon=True).start()
    threading.Thread(target=voice_monitor,   daemon=True).start()

    load_state()
    time.sleep(0.5)
    print("[ARCHER] Online. Everything looks good.")
    speak("Online. Everything looks good.")
    print()

    while True:
        try:
            user_input = input("[YOU] ").strip()

            if not user_input:
                continue

            if user_input.lower() == 'quit':
                print("[ARCHER] Shutting down.")
                save_state()
                break

            response = handle_command(user_input)

            if response is None:
                print("[ARCHER] thinking...")
                response = ask_archer(user_input)

            if response:
                print(f"[ARCHER] {response}")
                speak(response)
                print()

        except KeyboardInterrupt:
            print("\n[ARCHER] See you tomorrow.")
            save_state()
            break

if __name__ == "__main__":
    main()