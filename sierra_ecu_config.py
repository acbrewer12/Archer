#!/usr/bin/env python3
"""
sierra_ecu_config.py — Software ECU emulator for the 2006 GMC Sierra 2500HD
LQ4 6.0L V8 with 4L80E transmission.

No external dependencies beyond stdlib.
"""

import math
import random
import threading
import time

USE_EMULATOR = True

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exp_approach(current: float, target: float, tau: float, dt: float) -> float:
    """Exponential approach: moves current toward target with time constant tau."""
    return target - (target - current) * math.exp(-dt / tau)


class SierraECU:
    """
    Simulates ECU sensor readings for the 2006 GMC Sierra 2500HD (LQ4 / 4L80E).

    Call step(dt) repeatedly to advance time, then get_state() to read values.
    Use set_mode() to jump to a named operating scenario.
    """

    VIN = "1GTHK23U060000000"

    def __init__(self):
        self._lock = threading.Lock()
        self._elapsed = 0.0          # total simulated seconds since engine start

        # Engine
        self.engine_on = True
        self.rpm = 850.0
        self.speed = 0.0             # mph
        self.throttle = 5.0          # percent
        self.engine_load = 0.0       # percent

        # Temperatures (°F)
        self.ambient = 70.0
        self.coolant_temp = 70.0
        self.oil_temp = 70.0
        self.iat = 75.0              # ambient + ~5°F

        # Fuel / air
        self.maf = 3.2               # g/s
        self.fuel_pressure = 58.0    # psi
        self.stft_b1 = 0.0
        self.stft_b2 = 0.0
        self.ltft_b1 = 0.0
        self.ltft_b2 = 0.0

        # O2 sensors (volts)
        self.o2_b1s1 = 0.45
        self.o2_b2s1 = 0.45
        self._o2_phase = 0.0         # oscillation phase

        # Ignition
        self.timing = 10.0           # degrees BTDC
        self._knock = False

        # Battery
        self.battery_main = 14.2
        self.battery_aux = 13.8

        # Alternator
        self.alt_output = 14.2

        # Transmission
        self.gear = "P"
        self.target_gear = "P"
        self.tft = 70.0              # trans fluid temp °F
        self.line_pressure = 90.0   # psi
        self.tcc_state = "UNLOCKED"
        self.sol_a = False
        self.sol_b = False
        self.prndl = "P"

        # Wheel speeds / brakes
        self.brake_pressure = 0.0
        self.abs_active = False
        self.tcs_active = False

        # TPMS — 35 PSI at 70°F, ±0.5 PSI per 10°F delta
        self._tpms_base_psi = 35.0
        self.tpms_fl_psi = 35.0
        self.tpms_fr_psi = 35.0
        self.tpms_rl_psi = 35.0
        self.tpms_rr_psi = 35.0
        self.tpms_fl_temp = 70.0
        self.tpms_fr_temp = 70.0
        self.tpms_rl_temp = 70.0
        self.tpms_rr_temp = 70.0

        # Safety / body
        self.airbag_status = "OK"
        self.seatbelt_fl = True
        self.seatbelt_fr = False
        self.door_fl = False
        self.door_fr = False
        self.door_rl = False
        self.door_rr = False
        self.interior_lights = False

        # Mode override — when set, step() uses these targets instead of defaults
        self._mode = "cold_start"
        self._mode_rpm_target = 850.0
        self._mode_speed_target = 0.0
        self._mode_throttle_target = 5.0

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def set_mode(self, mode: str):
        """Jump to a named operating scenario."""
        with self._lock:
            self._mode = mode

            if mode == "cold_start":
                self.rpm = 850.0
                self.speed = 0.0
                self.throttle = 5.0
                self.coolant_temp = 70.0
                self.oil_temp = 70.0
                self._elapsed = 0.0

            elif mode == "warm_idle":
                self.rpm = 650.0
                self.speed = 0.0
                self.throttle = 5.0
                self.coolant_temp = 195.0
                self.oil_temp = 200.0

            elif mode == "cruise_55":
                self.rpm = 2000.0
                self.speed = 55.0
                self.throttle = 18.0
                self.gear = "4"
                self.tcc_state = "LOCKED"
                self.tft = 175.0
                self.coolant_temp = 195.0
                self.oil_temp = 200.0

            elif mode == "hard_pull":
                self.rpm = 5500.0
                self.speed = 80.0
                self.throttle = 100.0
                self.gear = "3"
                self.tcc_state = "UNLOCKED"
                self.coolant_temp = 210.0
                self.oil_temp = 220.0

            elif mode == "highway_80":
                self.rpm = 2400.0
                self.speed = 80.0
                self.throttle = 22.0
                self.gear = "4"
                self.tcc_state = "LOCKED"
                self.tft = 175.0
                self.coolant_temp = 195.0
                self.oil_temp = 200.0

            elif mode == "stop":
                self._mode_rpm_target = 0.0
                self._mode_speed_target = 0.0
                self.gear = "P"
                self.engine_on = False

            else:
                raise ValueError(f"Unknown mode: {mode!r}")

    def step(self, dt_seconds: float):
        """Advance simulation by dt_seconds."""
        with self._lock:
            dt = dt_seconds
            self._elapsed += dt

            # ── Coolant temp: exponential approach to 195°F over ~300 s ──────
            # tau chosen so 63% rise in 300 s: tau ≈ 180 s
            self.coolant_temp = _exp_approach(self.coolant_temp, 195.0, 180.0, dt)

            # ── Oil temp: slower, always slightly above coolant ───────────────
            oil_target = min(self.coolant_temp + random.uniform(0.0, 10.0), 205.0)
            self.oil_temp = _exp_approach(self.oil_temp, oil_target, 280.0, dt)

            # ── Trans fluid temp ──────────────────────────────────────────────
            tft_target = 170.0
            if self._mode == "hard_pull":
                tft_target = min(225.0, self.tft + 0.3 * dt)
            self.tft = _exp_approach(self.tft, tft_target, 350.0, dt)

            # ── IAT (follows ambient + 5°F) ───────────────────────────────────
            self.iat = _exp_approach(self.iat, self.ambient + 5.0, 60.0, dt)

            # ── RPM: cold start idles higher, drops after warm ────────────────
            if self._mode == "cold_start":
                if self.coolant_temp >= 160.0:
                    rpm_target = 650.0
                else:
                    rpm_target = 850.0
            elif self._mode == "stop":
                rpm_target = 0.0
            else:
                rpm_target = self.rpm   # held by mode setter
            self.rpm = _exp_approach(self.rpm, rpm_target, 2.0, dt)
            if self.rpm < 1.0:
                self.rpm = 0.0
                self.engine_on = False

            # ── Throttle / engine load ────────────────────────────────────────
            if self._mode == "hard_pull":
                self.engine_load = 95.0 + random.uniform(-2.0, 2.0)
            else:
                self.engine_load = min(100.0, (self.rpm / 6000.0) * (self.throttle / 100.0) * 120.0)

            # ── MAF ───────────────────────────────────────────────────────────
            # 3.2 g/s at idle; scales linearly with RPM and throttle
            self.maf = 3.2 * (self.rpm / 850.0) * (1.0 + (self.throttle - 5.0) / 100.0)
            self.maf = max(0.0, self.maf)

            # ── Ignition timing ───────────────────────────────────────────────
            if self.rpm >= 4000.0:
                timing_target = 28.0
            else:
                timing_target = 10.0 + (self.rpm - 850.0) / (4000.0 - 850.0) * 18.0
            if self._knock:
                timing_target -= 6.0
            self.timing = _exp_approach(self.timing, timing_target, 0.5, dt)

            # ── STFT noise ────────────────────────────────────────────────────
            if self.coolant_temp >= 160.0:
                noise = 3.0
            else:
                noise = 8.0
            self.stft_b1 = random.uniform(-noise, noise)
            self.stft_b2 = random.uniform(-noise, noise)

            # ── O2 sensors ────────────────────────────────────────────────────
            if self.coolant_temp >= 160.0:
                self._o2_phase += 2.0 * math.pi * 1.0 * dt   # 1 Hz
                swing = (math.sin(self._o2_phase) + 1.0) / 2.0  # 0–1
                self.o2_b1s1 = 0.1 + swing * 0.8
                self.o2_b2s1 = 0.1 + ((math.sin(self._o2_phase + 0.4) + 1.0) / 2.0) * 0.8
            else:
                self.o2_b1s1 = 0.45
                self.o2_b2s1 = 0.45

            # ── Battery ───────────────────────────────────────────────────────
            if self.engine_on:
                self.battery_main = 14.2 + random.uniform(-0.05, 0.05)
                self.battery_aux = 13.8 + random.uniform(-0.05, 0.05)
                self.alt_output = 14.2 + random.uniform(-0.05, 0.05)
            else:
                self.battery_main = 12.6
                self.battery_aux = 12.4
                self.alt_output = 0.0

            # ── Gear selection ────────────────────────────────────────────────
            if self._mode not in ("cruise_55", "hard_pull", "highway_80"):
                if self.speed == 0:
                    self.gear = "P"
                    self.prndl = "P"
                elif self.speed < 15:
                    self.gear = "1"
                    self.prndl = "D"
                elif self.speed < 30:
                    self.gear = "2"
                    self.prndl = "D"
                elif self.speed < 50:
                    self.gear = "3"
                    self.prndl = "D"
                else:
                    self.gear = "4"
                    self.prndl = "D"
            else:
                self.prndl = "D"

            self.target_gear = self.gear

            # ── TCC lockup ────────────────────────────────────────────────────
            if self.speed > 45 and self.gear == "4":
                self.tcc_state = "LOCKED"
            elif self._mode == "hard_pull":
                self.tcc_state = "UNLOCKED"
            elif self.speed < 40:
                self.tcc_state = "UNLOCKED"

            # 4L80E solenoids: sol_a/sol_b pattern for gear 1-4
            _sol_map = {
                "1": (True,  False),
                "2": (True,  True),
                "3": (False, True),
                "4": (False, False),
                "P": (False, False),
                "R": (True,  False),
                "N": (False, False),
                "D": (False, False),
            }
            self.sol_a, self.sol_b = _sol_map.get(self.gear, (False, False))

            # ── TPMS ──────────────────────────────────────────────────────────
            for corner in ("fl", "fr", "rl", "rr"):
                tire_temp = getattr(self, f"tpms_{corner}_temp")
                delta_f = tire_temp - 70.0
                psi = self._tpms_base_psi + (delta_f / 10.0) * 0.5
                setattr(self, f"tpms_{corner}_psi", round(psi, 2))

    def get_state(self) -> dict:
        """Return a snapshot of all ECU / sensor values."""
        with self._lock:
            # Engine state label
            if self.coolant_temp < 120.0:
                engine_state = "COLD START"
            elif self.coolant_temp < 165.0:
                engine_state = "WARMING"
            elif self.coolant_temp <= 215.0:
                engine_state = "NORMAL"
            else:
                engine_state = "HOT"

            return {
                # Engine
                "rpm":            round(self.rpm, 1),
                "speed":          round(self.speed, 1),
                "boost":          0,
                "ethanol":        0,
                "throttle":       round(self.throttle, 1),
                "engine_load":    round(self.engine_load, 1),
                "engine_state":   engine_state,

                # Temperatures
                "coolant_temp":   round(self.coolant_temp, 1),
                "oil_temp":       round(self.oil_temp, 1),
                "iat":            round(self.iat, 1),
                "tft":            round(self.tft, 1),

                # Fuel / air
                "maf":            round(self.maf, 3),
                "fuel_pressure":  self.fuel_pressure,
                "timing":         round(self.timing, 1),

                # STFT / LTFT
                "stft_b1":        round(self.stft_b1, 2),
                "stft_b2":        round(self.stft_b2, 2),
                "ltft_b1":        self.ltft_b1,
                "ltft_b2":        self.ltft_b2,

                # O2
                "o2_b1s1":        round(self.o2_b1s1, 3),
                "o2_b2s1":        round(self.o2_b2s1, 3),

                # Battery / electrical
                "battery_main":   round(self.battery_main, 2),
                "battery_aux":    round(self.battery_aux, 2),
                "alt_output":     round(self.alt_output, 2),

                # Transmission
                "gear":           self.gear,
                "target_gear":    self.target_gear,
                "line_pressure":  self.line_pressure,
                "tcc_state":      self.tcc_state,
                "sol_a":          self.sol_a,
                "sol_b":          self.sol_b,
                "prndl":          self.prndl,

                # Wheel speeds
                "wheel_speed_fl": round(self.speed, 1),
                "wheel_speed_fr": round(self.speed, 1),
                "wheel_speed_rl": round(self.speed, 1),
                "wheel_speed_rr": round(self.speed, 1),

                # Brakes / stability
                "brake_pressure": self.brake_pressure,
                "abs_active":     self.abs_active,
                "tcs_active":     self.tcs_active,

                # TPMS
                "tpms_fl_psi":    self.tpms_fl_psi,
                "tpms_fr_psi":    self.tpms_fr_psi,
                "tpms_rl_psi":    self.tpms_rl_psi,
                "tpms_rr_psi":    self.tpms_rr_psi,
                "tpms_fl_temp":   self.tpms_fl_temp,
                "tpms_fr_temp":   self.tpms_fr_temp,
                "tpms_rl_temp":   self.tpms_rl_temp,
                "tpms_rr_temp":   self.tpms_rr_temp,

                # Safety / body
                "airbag_status":  self.airbag_status,
                "seatbelt_fl":    self.seatbelt_fl,
                "seatbelt_fr":    self.seatbelt_fr,
                "door_fl":        self.door_fl,
                "door_fr":        self.door_fr,
                "door_rl":        self.door_rl,
                "door_rr":        self.door_rr,
                "interior_lights": self.interior_lights,
            }

    def get_vin(self) -> str:
        return self.VIN


# ---------------------------------------------------------------------------
# Module-level singleton + background step thread
# ---------------------------------------------------------------------------

_ecu = SierraECU()


def _background_step():
    while True:
        _ecu.step(0.1)
        time.sleep(0.1)


_step_thread = threading.Thread(target=_background_step, daemon=True, name="ecu-step")
_step_thread.start()
