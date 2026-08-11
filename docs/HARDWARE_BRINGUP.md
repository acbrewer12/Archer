# Archer — Pi + OBD Hardware Bring-Up

Written ahead of the first physical connection between the Raspberry Pi, the OBD
Gatekeeper relay, and a real OBD-II adapter on the 2006 Sierra/Silverado 2500HD.

Everything in Archer's OBD path has, to date, only ever run against the in-process
software emulator (`sierra_ecu_config.py`) or against mocked serial ports in
`test_archer.py`. This document exists to make the difference between "passes the
test suite" and "works on the truck" explicit *before* someone finds out in a
driveway.

Three parts:

1. [Emulator vs. real hardware](#1-emulator-vs-real-hardware) — why the emulator
   provides zero coverage of the live-OBD switchover, and five specific reasons
   auto-detect is unlikely to fire on first contact.
2. [First-boot validation checklist](#2-first-boot-validation-checklist) — the
   order to actually do the bring-up in, with an abort step at every stage.
3. [Open hardware questions](#3-open-hardware-questions) — things in
   `pi/obd_gatekeeper.py` and `pi/baseline_learner.py` that assume real CAN/serial
   timing behaviour and cannot be settled without the hardware in hand.

Two distinct hardware interfaces get conflated constantly in this repo, so before
anything else:

| | `pi/obd_gatekeeper.py` | `obd_autodetect()` (archer.py:11639) |
|---|---|---|
| **What it is** | The HMAC-gated relay guarding physical access to the truck's OBD-II connector | The client-side poller that reads live PIDs off an OBD adapter |
| **Runs as** | `obd_gatekeeper.service` on the Pi (separate process) | A daemon thread inside `archer.py` |
| **Ports** | `/dev/ttyAMA0` (auth) ↔ `/dev/ttyAMA1` (locally-wired ELM327/CAN transceiver) | Whatever `serial.tools.list_ports.comports()` turns up |
| **Peer** | `archer-os/obd-auth/obd_auth_client.py` on the Archer OS box | The OBDLink MX+ (or any ELM327) |
| **Failure mode** | Port stays silent; scan tools see a dead connector | Falls back to simulated data, silently |

They are *not* two views of the same link. The gatekeeper decides whether the OBD-II
connector is electrically alive at all; `obd_autodetect()` is a consumer that runs
once past it (or in a topology where the gatekeeper isn't in the path). Nothing in
the codebase makes one aware of the other — see
[3.9](#39-gatekeeper_state-in-archerpy-is-write-only-and-always-reports-locked).

---

## 1. Emulator vs. real hardware

### 1.1 The emulator and the real path share no interface at all

`sierra_ecu_config.py` exposes a singleton `_ecu` whose `get_state()` returns a
Python dict of ~40 named fields (`rpm`, `coolant_temp`, `prndl`, `tpms_fl_psi`, …),
advanced by a 10 Hz background thread (`sierra_ecu_config.py:417-427`). It has no
serial port, no ELM327 AT-command layer, and never emits a `41 XX ..` response byte
in its life.

`obd_autodetect()` consumes something entirely different: CR-terminated ASCII
command/response over a `serial.Serial` handle, parsed by `_obd_bytes()`
(archer.py:11628) looking for a literal `41` token in the response line.

The two paths do not meet anywhere. This is not a bug — it is a *coverage
statement*, and it is the important one:

> **The emulator cannot exercise the live-OBD switchover, by construction.** Every
> line of `obd_autodetect()` from the `comports()` scan through `_obd_cmd()`
> framing to the `41 XX` parse is dead code in every environment Archer has ever
> run in. `TestOBDPIDParsers` (test_archer.py:1643) covers the Mode-01 byte math in
> isolation and `TestGatekeeperHandshake` (test_archer.py:1448) covers the
> handshake against mocked ports, but nothing has ever executed the enumeration,
> connection, init-command, or poll-loop code against anything.

So the honest answer to "would the emulator's data cause the auto-switch to trigger
correctly?" is: the emulator's data never reaches the auto-switch code, and the
auto-switch code has never run. Sections 1.2–1.6 are the specific reasons to expect
it *not* to fire on first contact with real hardware.

### 1.2 The Bluetooth/RFCOMM gap — confirmed, with a correction

README.md documents the OBDLink MX+ as a Bluetooth SPP device on `/dev/rfcomm0`
(the mermaid diagram, README.md:51, and the Troubleshooting steps at
README.md:261-263). The MX+ is in fact a Bluetooth-only adapter — there is no USB
data port on it.

The intuitive worry is that pyserial doesn't enumerate RFCOMM devices at all. That
turns out to be **wrong**, and the real mechanism is worth stating precisely,
because it is what makes the failure *silent* rather than noisy:

pyserial's Linux backend *does* glob `/dev/rfcomm*`
(`serial/tools/list_ports_linux.py`, `comports()`). An RFCOMM node bound via
`rfcomm bind` **will** appear in the list. But an RFCOMM tty has no
`/sys/class/tty/rfcomm0/device` symlink, so `SysFS.__init__` leaves `subsystem =
None`, takes none of the `usb` / `usb-serial` / `pnp` / `amba` branches, and never
calls `apply_usb_info()`. The port therefore keeps `ListPortInfo`'s constructor
defaults. Verified directly against the installed pyserial 3.5:

```
device       = /dev/rfcomm0
description  = 'n/a'
manufacturer = None
subsystem    = None
```

`obd_autodetect()` matches on exactly those two fields (archer.py:11651-11653)
against `OBD_KEYWORDS = ('obdlink', 'obd', 'elm327', 'stm32', 'stn', 'scantool')`.
None of them is a substring of `'n/a'`. **The port is enumerated and then rejected.**
`port_device` stays `None`, the loop sleeps 5 s and rescans, forever.

Nothing in the repo automates the step that would create `/dev/rfcomm0` in the
first place. Grepped for `rfcomm`, `bluetoothctl`, `bluez`, `sdptool`, `hcitool`,
`bt-agent` across `archer.py`, `pi/`, `archer-os/`, all `.service` files, and the
whole tree: **the only four hits in the entire repository are the four lines of
README.md prose cited above.** There is no udev rule, no systemd unit, no pairing
agent, no `rfcomm bind`. `bluetooth_devices` / `link_bluetooth()` (archer.py:830,
5038) is the phone audio-profile feature and has nothing to do with the adapter.

Worse, on Archer OS specifically the device *cannot* exist:
`archer-os/kernel/archer.config` compiles `CONFIG_USB_SERIAL_CP210X`, `_CH341`,
`_FTDI_SIO`, `_PL2303` and `_GENERIC` (lines 106-111) but contains **zero**
`CONFIG_BT*` / `CONFIG_BLUETOOTH*` / RFCOMM symbols — a case-insensitive grep for
`bt` or `blue` over all 257 lines returns nothing. The custom kernel has no
Bluetooth stack, so a Bluetooth-only OBDLink MX+ is unreachable from Archer OS by
construction, regardless of what the detector does.

**Net: the README's documented hardware topology and the code's detection strategy
are structurally incompatible, and the resulting failure is invisible** — no
exception, no log line, no error field. The only outward symptom is `/obd_auth`
continuing to report `obd_mode: "default"` and the dashboard continuing to show
plausible-looking simulated numbers.

### 1.3 `OBD_PORT` was dead — now wired up as a manual override

`config.py:70` defines `OBD_PORT = os.environ.get('OBD_PORT', '')` and
`archer.env.example:56` ships an empty `OBD_PORT=`. README.md:263 instructs the
user to set `OBD_PORT=/dev/rfcomm0` when auto-detect fails.

Confirmed dead as written: **`archer.py` never imports `config.py` at all.** There
is no `import config` / `from config import` anywhere in `archer.py`, so nothing in
`obd_autodetect()` could read `config.OBD_PORT`, and it never called
`os.environ.get('OBD_PORT')` directly either. The only other `OBD_PORT` in the tree
is `archer-os/obd-auth/obd_auth_client.py:21`, which is a *different* variable
(`OBD_AUTH_PORT`, defaulting to `/dev/ttyUSB0`) for the gatekeeper handshake link.

Since this is precisely the escape hatch README's own troubleshooting depends on,
and since §1.2 means it is the *only* way a Bluetooth adapter can ever be used,
`obd_autodetect()` now reads `OBD_PORT` from the environment and uses it verbatim
when set, skipping the keyword scan. Unset (the default) leaves the previous
behaviour byte-for-byte unchanged. See the comment at archer.py:11644.

### 1.4 Hardcoded 38400 baud — unconfirmed

`obd_autodetect()` opens the adapter at a hardcoded `38400`
(archer.py:11672). This is not obviously right and is **not** being changed here,
because guessing is worse than flagging:

- Baseline ELM327 clones default to 38400. That is presumably where the number came
  from.
- STN-based adapters (the OBDLink family) commonly come up at a higher rate, and
  ScanTool's own tooling autobauds rather than assuming. The MX+'s USB/UART-side
  default rate is not documented anywhere in this repo and has not been measured.
- Over Bluetooth SPP the value is *meaningless* — RFCOMM ignores the termios line
  rate entirely — so if §1.3's `OBD_PORT=/dev/rfcomm0` path is the one that ends up
  being used, this constant is inert and the question evaporates.

**Needs hardware to resolve:** connect the adapter, and if `ATZ` returns garbage or
nothing at 38400, sweep 9600 / 38400 / 115200 / 500000 and record what actually
answers. Only then decide whether this should be a constant, an env var, or an
autobaud sweep. Note also that the current code has no way to tell "wrong baud"
from "no adapter" — both produce an empty `_obd_cmd()` result and the same silent
fallback.

### 1.5 A generic USB ELM327 gets claimed by the *Arduino* detector

This one is not in the original brief and is worth stating loudly, because it will
bite on the bench before the truck is ever involved.

`arduino_autodetect()` (archer.py:11105) runs as a daemon thread alongside
`obd_autodetect()` — both are started unconditionally at archer.py:11914-11915,
neither gated on `_IS_PI`. It scans the same `comports()` list with:

```python
_ARDUINO_KEYWORDS = ('arduino', 'ch340', 'cp210', 'cp2102', 'ftdi', 'uno', 'mega', 'nano')   # archer.py:11042
```

Those are exactly the USB-serial bridge chips used by generic USB ELM327 adapters
and by the OBDLink SX. A CH340-based ELM327 enumerates with a description like
`USB2.0-Serial`; an FTDI-based one as `FT232R USB UART`. Neither string contains
any of `OBD_KEYWORDS`, so `obd_autodetect()` skips it — but both match
`_ARDUINO_KEYWORDS`, so `arduino_autodetect()` opens it at **9600 baud**, marks
`arduino_state['connected'] = True`, and starts a reader thread writing Arduino
protocol at it.

Concrete symptom to watch for on first plug-in: `[ARDUINO] Connected on
/dev/ttyUSB0` in the logs and *nothing* from `[OBD]`. There is no arbitration
between the two detectors and no exclusion list. Documented, not fixed — the right
fix (make the two scanners mutually exclusive, probe rather than string-match)
is a behaviour change that should be designed against a real adapter, not guessed.

### 1.6 `USE_EMULATOR` — the auto-switch comment is aspirational

archer.py:2754 reads:

```python
USE_EMULATOR = True   # False when real OBDLink MX+ is detected
```

Investigated: **that assignment is the only write to the name anywhere in
`archer.py`.** There is no second assignment, no `globals()['USE_EMULATOR'] = …`,
no `setattr`. The comment describes behaviour that does not exist.

There are also three unrelated variables sharing the name, which is how the illusion
survives:

| Location | Value | Read by |
|---|---|---|
| `archer.py:2754` | literal `True`, never reassigned | `archer.py:1700`, `archer.py:1811`, `blueprints/modules.py:104,232,238`, `blueprints/roku.py:63` (all via `_a.USE_EMULATOR`) |
| `config.py:78` | `os.environ.get('USE_EMULATOR', 'true')` | nothing — `config.py` has no importers in the app path |
| `sierra_ecu_config.py:17` | literal `True` | nothing |

So setting `USE_EMULATOR=false` in `archer.env` has **no effect on `archer.py`**,
and `obd_mode` will report `'EMULATED'` (archer.py:1700) even while
`obd_autodetect()` is successfully polling a real adapter. The two mechanisms are
completely disjoint: the thing that actually silences the simulator is
`sim_flags['random_enabled'] = False` at archer.py:11681, and the thing that
actually reports live status is `obd2_display['mode'] = 'live'` at archer.py:11680.

**Practical consequence for bring-up:** do not use `obd_mode` from `/display_data`
to decide whether you are on real data. It is hardcoded. Use `/obd_auth`'s
`obd_mode` field (which reads `obd2_display`, the real one) — see §2.7.

Left as-is rather than fixed: making `USE_EMULATOR` track reality means deciding
whether `config.py`'s env-driven definition or `archer.py`'s module-level one is
canonical, and whether `obd_mode: 'EMULATED'` consumers (including the Roku channel
and the modules page) expect the current always-true behaviour. That is a real
behaviour change across four files and belongs in its own review, not in a
hardware-prep pass.

---

## 2. First-boot validation checklist

Do these **in order**. Every stage has an abort step; if a stage fails, abort *that*
stage before moving on rather than pushing through.

Assumes: a Pi you can SSH into, the gatekeeper relay board wired per
`pi/obd_gatekeeper.py`'s module docstring, and an OBD adapter still in its box.

**Ground rules for the whole procedure:**

- **Engine OFF, key OUT** for stages 2.1 through 2.5. Ignition ON (engine not
  cranking) only from 2.6. Engine running only at 2.8.
- Keep a hand free for the abort action at every stage — usually "pull the adapter
  out of the OBD-II port", which is always safe and always sufficient (see
  docs/SAFETY.md:90).
- Have a second terminal open on `sudo journalctl -u obd_gatekeeper -f` from 2.2
  onward. Most of what follows is read off that stream.

### 2.0 Before touching the truck — bench pre-flight

Everything here can be done at a desk. Doing it in the driveway instead is how
people end up debugging clock skew with a door open and a dome light on.

- [ ] `ls -l /etc/archer/obd_auth.key` — exists, mode `600` or `400`, owned by the
      account the service runs as.
- [ ] **The key file is a single line.** `wc -l /etc/archer/obd_auth.key` should be
      `1`. If you have rotated and it has two lines, read
      [3.6](#36-a-two-line-rotation-key-file-breaks-the-archer-os-client) first —
      the gatekeeper handles two lines, but `obd_auth_client.py` does not, and the
      failure looks like a wrong key.
- [ ] `systemctl is-enabled obd_gatekeeper` → `enabled`.
- [ ] `python3 -c "import RPi.GPIO"` succeeds. If it does **not**, the gatekeeper
      runs in stub mode — it logs `RPi.GPIO not available — relay in stub mode` and
      every `set_relay()` call is a no-op that still logs `OBD2 relay: UNLOCKED`.
      The logs will look identical to a working system while the relay never moves.
      Do not proceed until this import works.
- [ ] `timedatectl` → `System clock synchronized: yes`. This is the single most
      likely first-boot failure (see 2.5) and takes 30 seconds to rule out now.
- [ ] Run the handshake once with both ends on the bench, over a USB-serial pair or
      a loopback, before any of it is in the vehicle. docs/SAFETY.md already asks
      for this ("Test auth handshake on bench before in-vehicle install") and it is
      the only stage where a failure costs nothing.

**Abort:** any unchecked box. Nothing below diagnoses cleanly if these aren't true.

### 2.1 Physical wiring and power-up order

Order matters: bring the Pi up *before* anything is electrically connected to the
vehicle bus, so the relay is driven to a known state before it can pass anything.

1. Truck **off**, key out. Confirm the OBD-II connector is unoccupied.
2. Wire the relay board: Pi **BCM GPIO 17** → relay `IN`, override switch on **BCM
   GPIO 27** to ground. Common ground between Pi and relay board.
   > **Note the discrepancy:** README.md:54 says `GPIO 5 / relay`. The code says
   > `RELAY_PIN = 17` (`pi/obd_gatekeeper.py:88`). **The code is authoritative.**
   > README is wrong; do not wire to GPIO 5.
3. Verify the relay board's coil polarity *before* connecting it to the bus — see
   [3.1](#31-relay-polarity-is-assumed-active-high-and-never-verified). This is the
   one wiring question with a genuine fail-open risk and it takes a multimeter and
   two minutes.
4. Power the Pi from a 5 V / 3 A supply. Let it boot fully.
5. Only now connect the relay's switched contacts into the CAN H/L path.

**Abort:** relay clicks or the OBD-II connector goes live at any point before step
5 → power the Pi down, disconnect the relay from the bus, re-check polarity.

### 2.2 Confirm the gatekeeper starts LOCKED

This is the load-bearing check of the whole procedure. What "locked" looks like from
outside, in decreasing order of trustworthiness:

**(a) The service log — trust this one.**

```
sudo journalctl -u obd_gatekeeper -n 50 --no-pager
```

Expected within a second or two of start:

```
[HH:MM:SS] [GATEKEEPER] INFO Relay on GPIO 17: OBD2 port LOCKED
[HH:MM:SS] [GATEKEEPER] INFO Emergency override on GPIO 27 (hold 3.0s)
[HH:MM:SS] [GATEKEEPER] INFO OBD2 Gatekeeper running on /dev/ttyAMA0 — 1 key(s) loaded
[HH:MM:SS] [GATEKEEPER] INFO Port is LOCKED — timestamp window: ±30s, max session: 4h
[HH:MM:SS] [GATEKEEPER] INFO Override switch monitor thread running
```

If you see `RPi.GPIO not available — relay in stub mode` instead of the first line,
stop — see 2.0.

**(b) Expected idle noise — this is normal, not a fault.**

With no client connected, the main loop opens `/dev/ttyAMA0`, blocks in
`readline()` for `AUTH_TIMEOUT` (10 s), gets an empty string, and logs:

```
[GATEKEEPER] WARNING Unrecognised opener: '' — ignoring
```

…then reopens the port and does it again. **Roughly one warning every 10 seconds,
~8,600 a day, forever.** That is the designed idle behaviour, not a symptom. Two
things worth internalising:

- This path deliberately does **not** call `_record_failure()`
  (`pi/obd_gatekeeper.py:452-454`), so idling does not creep toward lockout. Good.
- It does mean the gatekeeper's journal is mostly this line. Filter with
  `journalctl -u obd_gatekeeper | grep -v 'Unrecognised opener'` when hunting for
  anything else.

**(c) Direct GPIO readback — the ground truth.**

```
raspi-gpio get 17     # expect: level=0  (LOW = locked, per RELAY_PIN docstring)
```

or `gpioget gpiochip0 17` on a `libgpiod` system. Cross-check that against the
relay board's own LED and, ideally, continuity across the switched contacts.

**(d) `/obd_auth` — read this one with care.**

```
curl -s http://localhost:7860/obd_auth   # Tier 1 auth required
```

```json
{
  "obd_connected": false,
  "obd_mode": "default",
  "last_update_age_s": null,
  "gatekeeper_service": "active",
  "relay_unlocked": false,
  "auth_key_path": "/etc/archer/obd_auth.key"
}
```

Three caveats, all verified:

- `relay_unlocked` **is not a relay reading.** archer.py:11272 computes it as
  `obd2_display['connected'] and obd2_display['mode'] == 'live'` — i.e. it reports
  whether `obd_autodetect()` is polling, in a *different process* that has no
  visibility into the gatekeeper's GPIO. A stuck-unlocked relay reports
  `relay_unlocked: false` here. Use (c) for relay state.
- `gatekeeper_service` is `n/a` unless `_IS_PI` is true, and `_IS_PI`
  (archer.py:58) is `_platform.machine().startswith('arm')`. **A 64-bit Raspberry
  Pi OS reports `aarch64` and fails that test.** On a 64-bit image this field reads
  `n/a` on a perfectly healthy Pi. Do not read it as "service missing" — check
  `systemctl is-active obd_gatekeeper` directly.
- docs/API.md:464-468 documents this endpoint as returning
  `{"authenticated": true, "relay_open": true}`. Neither field exists. The JSON
  above is what the code actually returns.

**Do not use `/gatekeeper_status`** (blueprints/modules.py:245) during bring-up. See
[3.9](#39-gatekeeper_state-in-archerpy-is-write-only-and-always-reports-locked) — it
is backed by a dict nothing ever writes to and always reports `LOCKED`, `0` failed
attempts, regardless of reality.

**Abort:** relay reads HIGH at rest, or the service is not `active` → `sudo
systemctl stop obd_gatekeeper`, disconnect the relay from the bus, and resolve
before continuing. A relay that idles unlocked means the port was never gated.

### 2.3 A successful handshake, from the outside

Trigger it by running the client (`archer-os/obd-auth/obd_auth_client.py`) from the
Archer OS end, or by hand over the auth link.

**Gatekeeper journal — the success sequence, in order:**

```
[GATEKEEPER] INFO Challenge issued
[GATEKEEPER] INFO Authentication PASSED
[GATEKEEPER] INFO Session permissions: ['read_all', 'write_all', 'admin']
[GATEKEEPER] INFO OBD2 relay: UNLOCKED
[GATEKEEPER] INFO Proxy mode active — session expires in 4h
```

**Client side:** `[OBD_AUTH] Authenticated — OBD2 port unlocked` on stderr, exit
code `0`.

**Timing:** the whole exchange is two round trips over a 115200 baud UART carrying
~120 bytes. Expect it to complete in well under a second. If it takes seconds,
something is retrying or buffering — see
[3.3](#33-readline-framing-over-a-real-uart). `AUTH_TIMEOUT` is 10 s, so a slow-but-
successful handshake still passes; note the duration anyway, it is the only real
latency datum this project will ever get (see [3.2](#32-auth_timeout-and-timestamp_window-were-chosen-without-latency-data)).

**Variants that are still success, but mean something:**

- `Authentication PASSED` → normal, current key.
- `Authenticated with PREVIOUS key — rotate key file soon` → the *fallback* key
  matched. Auth succeeded, but line 1 of your key file is not what the client
  holds. Finish the rotation.
- `Session permissions: ['read_all']` → a READONLY key authenticated. Expect
  `BLOCKED command outside key scope:` lines later when anything tries a Mode 04.

**GPIO check while the session is up:** `raspi-gpio get 17` → `level=1`. And when
the session ends you should see `Proxy session ended after Ns — re-locking OBD2
port` followed by `OBD2 relay: LOCKED`, and the pin back to `level=0`.

**Abort:** relay goes HIGH *without* a preceding `Authentication PASSED` in the
journal → that is either the emergency override (check GPIO 27 / the switch, and
look for `EMERGENCY OVERRIDE activated`) or a relay/wiring fault. Kill the service
and pull the adapter.

### 2.4 A failed handshake, from the outside

Worth deliberately provoking once, on the bench, so you recognise it later.

**Wrong key:**
```
[GATEKEEPER] INFO Challenge issued
[GATEKEEPER] WARNING Authentication FAILED — wrong key, staying silent
```
Relay stays LOW. Client gets no `AUTH_OK` and exits 1. Note the gatekeeper
genuinely sends *nothing* on failure — a client that hangs waiting for a verdict
and then times out is the expected shape of this failure, not a bug.

**Malformed / absent response** (client died mid-handshake, partial line, wrong
framing):
```
[GATEKEEPER] WARNING Expected RESPONSE, got: '' — ignoring
```
This **does** count as a failure (`pi/obd_gatekeeper.py:472-475`). A client that
crashes after sending `ARCHER_AUTH_REQ` burns a lockout tick every attempt.

**Expired scoped key** (HMAC matched, `key_manager.py` says expired):
```
[GATEKEEPER] WARNING Authentication REJECTED — key is registered but EXPIRED (key_manager.py)
```

### 2.5 Clock skew — the most likely first-boot failure

Genuinely expect this. A Pi with no RTC boots to whatever timestamp was last
written to disk. Before `systemd-timesyncd` gets a fix — which needs the truck
hotspot up and an upstream route — the clock can be days off.

**What it looks like on the gatekeeper:**
```
[GATEKEEPER] WARNING Timestamp out of window (86412.3s) — rejecting
```
(`pi/obd_gatekeeper.py:481-484`, `TIMESTAMP_WINDOW = 30`.)

**What it looks like on the client** (which does its own ±30 s check at
`obd_auth_client.py:72-75`, before even computing the MAC):
```
[OBD_AUTH] Timestamp skew 86412.3s — clock sync issue
```

Either message means "the clocks disagree", not "the key is wrong". Note the two
ends can fail *asymmetrically*: whichever clock is wrong, the one with the correct
time is the one that rejects.

**Fix, in order of preference:**
1. `timedatectl status`, then wait for `System clock synchronized: yes`.
2. `sudo chronyc makestep` / `sudo systemctl restart systemd-timesyncd`.
3. Manually: `sudo date -u -s "YYYY-MM-DD HH:MM:SS"` on the Pi, then retry.
4. Long term, fit a battery-backed RTC. A vehicle Pi that cold-boots without
   network has no other way to land inside a ±30 s window.

**Watch the failure count while you debug this.** Every rejected attempt calls
`_record_failure()`, and the tiers are cumulative and **never decay**:

| Failures | Result | Journal line |
|---|---|---|
| 3 | 30 s lockout | `SOFT LOCKOUT 30s after 3 failures` |
| 6 | 300 s lockout | `HARD LOCKOUT 300s after 6 failures` |
| 10 | **indefinite** | `PERMANENT LOCKOUT after 10 failures — manual reset required` |

`_fail_count` (`pi/obd_gatekeeper.py:115`) is process-global and is only ever zeroed
by a *successful* auth (`_reset_failures()`, line 511). It does not time out or
decay. Ten failed attempts across the entire lifetime of the process — even spread
over weeks — reaches permanent lockout. Retrying a clock-skew failure in a loop will
get you there in under a minute.

**Recovery from permanent lockout:** the state is in memory only, so
```
sudo systemctl restart obd_gatekeeper
```
clears it. (`Restart=always`, `RestartSec=3` in `pi/obd_gatekeeper.service`.) The
physical override switch also still works during lockout — `_override_active` is
checked *before* `_check_lockout()` in the main loop (lines 548-554), which is the
deliberate escape hatch.

**Abort:** if you hit permanent lockout in the vehicle, restart the service, fix
the clock first, and only then retry auth. Do not keep retrying.

### 2.6 Connect the OBD adapter

Key to **ON, engine not running**.

**If USB (OBDLink SX or a generic ELM327):** plug it into the Pi, then
`dmesg | tail` and `python3 -m serial.tools.list_ports -v`. Write down the exact
`description` and `manufacturer` strings — you need them for 2.7, and per §1.5 you
should specifically check whether `[ARDUINO] Connected on …` shows up in the Archer
log. If it does, the Arduino detector has claimed your OBD adapter.

**If Bluetooth (OBDLink MX+):** there is no automation for this in the repo (§1.2),
so it is entirely manual and must be redone after every reboot unless you write a
unit for it:

```
bluetoothctl
  power on
  scan on            # note the MX+'s MAC
  pair XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX
  quit
sudo rfcomm bind 0 XX:XX:XX:XX:XX:XX 1
ls -l /dev/rfcomm0
```

Then set `OBD_PORT=/dev/rfcomm0` in `archer.env` and restart Archer. As of §1.3 that
variable is now actually read. Without it, §1.2 applies and the adapter will never
be selected no matter how well it is paired.

> On Archer OS specifically this will not work at all — the custom kernel has no
> Bluetooth support (§1.2). Use a USB adapter there, or rebuild the kernel.

**Abort:** adapter warm to the touch, or the truck's OBD-II fuse pops → pull it
immediately. Per docs/SAFETY.md:90, removing the adapter is always safe and the
vehicle is unaffected.

### 2.7 Prove you are on real data, not simulating

This is the check §1.1 exists to motivate. The failure mode is *silent* — the
dashboard shows smooth, plausible numbers either way, because
`sierra_ecu_config.py` is a decent simulator.

**Primary check — `/obd_auth`:**

```
curl -s http://localhost:7860/obd_auth
```

| Field | Simulating | Live |
|---|---|---|
| `obd_mode` | `"default"` | `"live"` |
| `obd_connected` | `false` | `true` |
| `last_update_age_s` | `null`, or stops advancing | a small number that keeps changing on refresh |

`obd_mode` here reads `obd2_display` (archer.py:11680), which is the flag
`obd_autodetect()` actually sets. **This is the one to trust.**

**Do NOT use `obd_mode` from `/display_data`** (archer.py:1700, 1811). That one is
derived from the hardcoded `USE_EMULATOR = True` and reports `EMULATED` forever —
see §1.6.

**Secondary check — the log.** On success `obd_autodetect()` prints exactly one
line:
```
[OBD] Connected on /dev/ttyUSB0 — live data active
```
and on loss:
```
[OBD] Disconnected — simulation resumed
```
Absence of the first line after several minutes with the adapter plugged in means
the `comports()` scan never matched — §1.2 / §1.5.

**Tertiary check — the physical distinguisher.** This is the one that cannot be
faked. Do something to the truck that the emulator has no way to know about:

- **Pull the OBD adapter out.** Live: `[OBD] read error:` then `[OBD] Disconnected —
  simulation resumed` within a couple of seconds, and `/obd_auth` flips to
  `"default"`. Simulating: absolutely nothing changes. **This is the definitive
  test — do it every time.**
- **Turn the key off.** Live: RPM and coolant go to whatever the bus reports (or the
  read errors out). Simulating: the numbers keep evolving on their own, because
  `sierra_ecu_config.py`'s background thread (line 417-427) never stops.
- **Blip the throttle** with the engine running (2.8). Live: `truck_state['rpm']`
  tracks it within a poll cycle. Simulating: it does not correlate with your foot.

**Also expect, on a genuinely live connection:** per-PID blacklisting messages for
PIDs this truck doesn't support. `015C` (oil temp) is non-standard and `0110` (MAF)
may or may not be present on an LQ4; three failures blacklists a PID for 5 minutes:
```
[OBD] PID 015C blacklisted after 3 failures (avg 1500ms)
```
**Seeing these is good news** — it is proof you are talking to a real ECU that is
declining specific PIDs. The emulator can never produce them.

**Abort:** if `/obd_auth` says `live` but pulling the adapter changes nothing, you
are not actually live and something is wrong with the state flags. Stop and
investigate before trusting any displayed number.

### 2.8 Engine running — first live baseline

Only after 2.7 passes the pull-the-adapter test.

- [ ] Start the engine. Watch RPM in `/obd_auth` → `last_update_age_s` staying
      small, and the dashboard tracking reality.
- [ ] Let it idle to operating temperature while watching for `[OBD] read error:` /
      `Sensor error: … out of bounds` lines. Out-of-bounds rejections
      (archer.py:11683-11690) are the validator working, but a *flood* of them means
      a parser/protocol mismatch, not a bad sensor.
- [ ] Record the observed per-PID response times from the blacklist messages and the
      `avg_ms` figures. This is the latency data §1.4 and
      [3.2](#32-auth_timeout-and-timestamp_window-were-chosen-without-latency-data)
      / [3.5](#35-welford-stats-from-an-instant-response-emulator-vs-a-real-bus) all
      need and currently do not have. **Write it down.** It is the entire point of
      this bring-up.
- [ ] Do **not** start `baseline_learner.py` calibration yet. Read
      [3.4](#34-calibration_target1000-does-not-mean-100-engine-starts) and
      [3.7](#37-baseline_file-is-a-relative-path) first — as it stands, `start`
      would build a baseline from the *emulator*, not from the truck, and write it
      to whatever the current working directory happens to be.

**Abort at any point:** pull the adapter from the OBD-II port. The truck is
unaffected — Archer only ever reads.

---

## 3. Open hardware questions

Things that are unresolvable without the physical hardware. Each is flagged with an
inline comment at the exact line in the source, in the style
`archer-os/build.sh` uses. **None of these are being changed** — the two `pi/`
modules are documentation-only in this pass.

Some candidates were evaluated and found *not* to be issues; those are recorded in
[3.10](#310-evaluated-and-found-not-to-be-issues) so nobody re-litigates them.

### 3.1 Relay polarity is assumed active-high and never verified

`pi/obd_gatekeeper.py:88` — `RELAY_PIN = 17  # BCM — HIGH = OBD2 unlocked, LOW = locked`

The code drives HIGH to unlock and initialises the pin `initial=GPIO.LOW`. That is
correct **only for an active-high relay module.** A large fraction of the cheap
opto-isolated relay boards sold for Pi use are **active-LOW** — they energise when
`IN` is pulled low, and the pull-down/float state at boot energises the coil.

Note that the very next line, `OVERRIDE_PIN = 27`, is explicitly annotated
`(pull-up, active-low)` — so the author was thinking about polarity for the input
and simply did not state an assumption for the output.

If the installed board is active-low, the polarity is inverted end to end and the
OBD-II port is **UNLOCKED whenever the Pi is off, still booting, or crashed** — the
exact opposite of the fail-safe the module docstring promises ("Default state:
complete silence"). This is the single highest-consequence unknown in the file.

**What real hardware settles it:** with the relay disconnected from the bus, drive
GPIO 17 low and then high and check continuity across the switched contacts.
Continuity at LOW = active-low board = polarity must be inverted before this is
wired to a vehicle. Also check the board's de-energised (Pi unpowered) state, which
is the state that matters most.

### 3.2 `AUTH_TIMEOUT` and `TIMESTAMP_WINDOW` were chosen without latency data

`pi/obd_gatekeeper.py:91-92` — `AUTH_TIMEOUT = 10.0`, `TIMESTAMP_WINDOW = 30`

Both are round numbers with no measurement behind them. On the numbers, 10 s is
enormously generous for two round trips of ~120 bytes at 115200 baud (~10 ms of wire
time), so `AUTH_TIMEOUT` is very unlikely to be *too short*. The interesting question
is the other direction: 10 s is also how long the idle loop blocks per iteration, so
it sets the service's minimum reaction time to a client appearing, and it is the
window during which a stalled client holds `/dev/ttyAMA0` open.

`TIMESTAMP_WINDOW = 30` is the sharper one. It is not a latency budget — it is a
*clock-agreement* budget, and §2.5 shows it is the binding constraint on a Pi with
no RTC. 30 s is tight enough that ordinary NTP-less drift breaks auth, and the
failure is indistinguishable from a wrong key without reading the journal.

**What real hardware settles it:** measure actual handshake wall time over the real
UART with the real adapter in the loop (§2.3), and measure how far the Pi's clock
has drifted at the moment `obd_auth_client.py` first runs after a cold boot. If the
observed handshake is ~50 ms, `AUTH_TIMEOUT` has 200× headroom and could come down
a lot. If cold-boot skew is routinely > 30 s, the answer is an RTC, not a bigger
window — widening `TIMESTAMP_WINDOW` widens the replay window by the same amount.

### 3.3 `readline()` framing over a real UART

`pi/obd_gatekeeper.py:448` and `:468` — both reads are
`port.readline().decode(...).strip()`

`readline()` on a `serial.Serial` returns on `\n` **or** on timeout, whichever comes
first, and a timeout return is a *partial line* indistinguishable from a short one.
Every existing test (test_archer.py:1448, `TestGatekeeperHandshake`) mocks the port
and returns whole lines atomically, so partial-read behaviour has never been
exercised.

On a real UART with real electrical noise, a partial read at line 448 produces
`Unrecognised opener: 'ARCHER_AUTH_RE'` and a silent retry — harmless. A partial
read at line 468 produces a truncated `client_mac`, which fails
`hmac.compare_digest` and — importantly — **calls `_record_failure()`**. Line noise
therefore counts toward the lockout tiers described in §2.5, and ten noise events
over the process lifetime is a permanent lockout.

Note the proxy path (`_proxy`, lines 354-373) explicitly does *not* have this
problem: it buffers and splits on `\r`, holding partial commands over to the next
read. The handshake path has no equivalent.

**What real hardware settles it:** run the handshake a few hundred times over the
actual `/dev/ttyAMA0` wiring, with the engine running (alternator noise, injector
transients), and count how many produce `Expected RESPONSE, got:` with a truncated
value. If that number is non-zero, either the handshake needs the same
buffer-until-terminator treatment `_proxy` already has, or `_record_failure()`
should distinguish "malformed" from "wrong key".

### 3.4 `CALIBRATION_TARGET=1000` does not mean "100 engine starts"

`pi/baseline_learner.py:26` — `CALIBRATION_TARGET = 1000`, vs. the module docstring
at line 5: *"Runs during the calibration period (first 100 engine starts after
install)."*

These cannot be reconciled at the rate the code actually samples.
`_run_calibration()` (line 226) sleeps `0.1` s between samples, so 1000 samples is
**100 seconds** — one uninterrupted minute and forty seconds, not 100 engine starts.
For "1000 samples ≈ 100 starts" to hold, one start would have to contribute ~10
samples, i.e. one second of engine run time each. The docstring's framing and the
constant describe completely different calibration periods.

This matters beyond pedantry: 100 starts would span weeks and capture cold mornings,
hot restarts, highway warm-up, and seasonal variation — a *representative* baseline.
100 seconds of one idle captures a single thermal state, and everything else the
truck ever does will read as an anomaly against it.

**What real hardware settles it:** decide what the calibration period is meant to
be, then measure the achievable real sample rate on the truck (see 3.5 — it is not
10 Hz) and set the target from that. A per-start counter would serve the docstring's
intent far better than a raw sample count, but that is a design change, not a
constant tweak.

### 3.5 Welford stats from an instant-response emulator vs. a real bus

`pi/baseline_learner.py:29` — `Z_SCORE_THRESHOLD = 3.0`, applied in `analyze()`
(lines 145-146)

Three compounding reasons a baseline learned from `sierra_ecu_config.py` will not
transfer to the truck:

**Sampling rate.** `_run_calibration()` polls at 10 Hz against an in-process dict
read — effectively zero latency. The real path sweeps 13 PIDs sequentially
(archer.py PID_TABLE, ~11719-11733), each with up to a 1.5 s timeout, plus a 0.15 s
inter-sweep sleep. A 2006 GMT800 2500HD on GM Class 2 (J1850 VPW, 10.4 kbit/s) is
slow — a full sweep is plausibly ~1 s, i.e. **~1 Hz, an order of magnitude slower
than calibration assumed.** (README.md:71 claims "every 200 ms"; that has never been
measured and looks optimistic.)

**Autocorrelation.** The emulator's background thread also steps at 10 Hz
(`sierra_ecu_config.py:420-423`), so consecutive samples are near-duplicates.
Welford treats them as independent observations. The result is a mean estimated from
~1000 samples but a **variance estimated from far fewer effective ones** — the
learned stddev is biased low, sometimes drastically. A 3-sigma threshold built on an
artificially small sigma is a hair trigger, and every real-world reading would trip
it.

**Zero-variance PIDs.** Several emulator fields are literal constants — `boost` and
`ethanol` are hardcoded `0` (`sierra_ecu_config.py:336-337`), and
`fuel_pressure`, `ltft_b1/b2`, `line_pressure`, `sol_a/sol_b` are returned unrounded
and appear static. Any PID with `stddev == 0.0` is **silently skipped** by
`analyze()` (line 143). So an emulator-derived baseline doesn't just mis-scale those
channels — it excludes them from anomaly detection entirely, while on real hardware
the same channels carry ordinary sensor noise and would be checked.

**Field-set mismatch, on top of all that.** `_ecu.get_state()` returns ~40 keys
including strings and body-control state (`engine_state`, `prndl`, `tcc_state`,
`airbag_status`, door/seatbelt flags, TPMS). The real path populates 13 PIDs plus
battery voltage. A baseline learned from the emulator tracks a superset that the
real bus can never supply, and — because `analyze()` skips unknown keys (line 137) —
those entries just sit in `truck_baseline.json` doing nothing.

**What real hardware settles it:** collect a real calibration run off the truck and
compare per-PID stddev against the emulator-derived one. If real sigma is
consistently larger (it should be), the emulator-derived baseline is unusable and
`truck_baseline.json` must be discarded and rebuilt on the vehicle. Measure the
real false-positive rate at 3.0 sigma before trusting any anomaly the learner emits.

### 3.6 A two-line rotation key file breaks the Archer OS client

`pi/obd_gatekeeper.py:210-223` (`load_keys`) vs.
`archer-os/obd-auth/obd_auth_client.py:26-35` (`load_key`)

The gatekeeper's documented rotation procedure (module docstring lines 37-41) is to
put the new key on line 1 and keep the old on line 2. `load_keys()` handles that
correctly: it splits on newlines and returns up to two separate 32-byte keys.

The client does not. It reads the whole file, `.strip()`s it, and calls
`bytes.fromhex(raw)` on the result. **Python's `bytes.fromhex` skips all ASCII
whitespace, including the interior newline** — verified: `bytes.fromhex('aa'*32 +
'\n' + 'bb'*32)` returns a **64-byte** value rather than raising. So during a
rotation window the client silently computes its HMAC with a 64-byte concatenation
of both keys, which matches neither of the gatekeeper's 32-byte keys.

The symptom is `Authentication FAILED — wrong key, staying silent` with a key file
that looks perfectly correct, and it burns a lockout tick per attempt (§2.5).

This is a genuine defect rather than a hardware unknown, but it is left unfixed here
deliberately: it lives in `archer-os/`, not in the two `pi/` modules this pass
covers, and fixing it is a behaviour change to authentication code. Flagged, and
2.0's checklist tells the operator to verify the key file is one line before
starting.

### 3.7 `BASELINE_FILE` is a relative path

`pi/baseline_learner.py:25` — `BASELINE_FILE = Path("truck_baseline.json")`

Resolved against the current working directory. Run from the repo root by hand,
that's the repo root. Run from a systemd unit with no `WorkingDirectory=` (which is
what `pi/obd_gatekeeper.service` and `pi/oled_display.service` both look like), CWD
is `/` — so the learner would try to write `/truck_baseline.json`, fail on
permissions, and lose the calibration.

Nothing currently invokes the learner from a unit, so this is latent rather than
live, but it becomes live the moment someone does the obvious thing. `status()`
already surfaces the resolved path (`str(self._file.resolve())`, line 173) — check
it before starting a long calibration run.

### 3.8 `record_sample()` has no calibration guard — the baseline can poison itself

`pi/baseline_learner.py:107-119`

`record_sample()` updates the Welford accumulators unconditionally. `is_calibrated`
gates nothing; it is only read by `_run_calibration()`'s `while` condition (line
217) and reported in `status()`.

That is fine for the standalone CLI, which stops looping once calibrated. It is not
fine for the wiring the docstring anticipates — line 201-202 says *"In production,
record_sample() is called from the main Archer data pipeline instead."* **That
pipeline does not exist:** grepping the whole tree, `BaselineLearner` and
`record_sample` appear only inside `pi/baseline_learner.py` itself. Nothing in
`archer.py`, the blueprints, or `pi/` ever constructs or feeds it.

When someone does wire it up, an unguarded `record_sample()` folds every subsequent
reading — including the anomalies it is supposed to detect — back into the baseline.
A slowly failing sensor would be learned as normal, which is precisely the failure
the module exists to catch. Worth resolving at the same time as 3.4, since both turn
on what "the calibration period" actually means.

### 3.9 `gatekeeper_state` in archer.py is write-only and always reports LOCKED

`archer.py:2744-2751`

```python
gatekeeper_state = {
    'auth_state': 'LOCKED', 'key_type': None, 'session_start': None,
    'failed_attempts': 0, 'last_seen': None, 'lockout_until': None,
}
```

Grepped for writes: there are **none**. Every reference is a read —
`archer.py:1791-1795` (into `/display_data`) and `blueprints/modules.py:106, 250`
(`/gatekeeper_status`). No code path anywhere sets `auth_state`, increments
`failed_attempts`, or populates `key_type`.

This is structural, not an oversight anyone can fix cheaply: the gatekeeper is a
**separate process** (`obd_gatekeeper.service`) with no IPC back to the Flask app.
There is no channel — no file, no socket, no shared state — over which it could
report. So `/gatekeeper_status` will report `LOCKED` / `0 failed attempts` while the
relay is unlocked and the port is in permanent lockout, with equal confidence.

**Bring-up consequence, and why it's in §2.2:** the only trustworthy views of
gatekeeper state are `journalctl -u obd_gatekeeper` and a direct GPIO read.
Closing this properly means giving the gatekeeper somewhere to publish state — a
status file that Flask reads would match the pattern `pi/oled_display.py` already
uses with `/tmp/oled_state.json` — but that is new inter-process surface area on
security-relevant code and needs its own design pass.

### 3.10 Evaluated and found *not* to be issues

Recorded so these don't get re-raised:

- **Relay switching speed racing the auth timeout.** No race. `set_relay(True)` is
  called at `pi/obd_gatekeeper.py:559` *after* `handle_connection()` has already
  returned a permission list, and `run_proxy_session()` on the next line opens the
  ELM port and blocks on its proxy threads. `AUTH_TIMEOUT` bounds the handshake
  reads only — it has already elapsed as a concern by the time the GPIO moves.
  Mechanical relay settle time (~5-10 ms) is nowhere near any timeout in the file.
  The only real-world concern is that the first byte a client sends immediately
  after `AUTH_OK` could reach the ELM port before the contacts have settled;
  worth watching for on first bring-up, but it is a lost first command, not a
  security boundary problem.
- **Idle-loop `Unrecognised opener` warnings creeping toward lockout.** They do
  not. That path returns at line 454 *without* calling `_record_failure()`. This
  looks like an oversight and is actually the correct behaviour — see §2.2(b).
- **Emergency override being blocked by lockout.** It is not.
  `_override_active.is_set()` is tested at line 548, before `_check_lockout()` at
  line 552, so the physical switch works even under permanent lockout. This is the
  intended escape hatch and it is wired correctly.
- **Population vs. sample stddev in `_stddev()`** (`baseline_learner.py:101` uses
  `M2 / count`, not `M2 / (count - 1)`). Technically biases sigma low, but by
  0.05% at n=1000 — utterly swamped by the autocorrelation effect in 3.5. Not
  worth touching.

---

## Summary — what "ready for the truck" would mean

Nothing below is currently true. In rough dependency order:

1. Relay polarity confirmed with a meter against the actual board (3.1), and
   confirmed to fail *locked* with the Pi unpowered.
2. A real handshake completed over the real UART, with its wall time recorded
   (2.3, 3.2), and the key file confirmed single-line (3.6).
3. A cold-boot clock-skew measurement, and a decision on an RTC (2.5, 3.2).
4. A few hundred handshakes with the engine running, counting malformed-response
   events (3.3).
5. An adapter that `obd_autodetect()` actually selects — which today means either
   `OBD_PORT=` set explicitly (§1.3) or a USB adapter whose description happens to
   contain one of the keywords and *doesn't* get grabbed by the Arduino detector
   first (§1.5).
6. Real per-PID latency numbers off the truck (2.8), which is the input everything
   in 3.4 and 3.5 is blocked on.
7. `truck_baseline.json` discarded and rebuilt from real bus data, after 3.4/3.7/3.8
   are resolved.
