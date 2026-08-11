#!/usr/bin/env python3
"""
baseline_learner.py — Learns the truck's individual CAN bus baseline.

Runs during the calibration period (first 100 engine starts after install).
Records normal OBD2 PID patterns, timing, and value distributions.
Builds truck_baseline.json after calibration completes.

After calibration: deviations from baseline trigger early warnings
before fault codes set — catch problems before the ECU does.

Usage:
  python baseline_learner.py start    # begin/resume calibration
  python baseline_learner.py status   # show calibration progress
  python baseline_learner.py report   # show learned baseline
"""

import json
import math
import os
import sys
import time
from pathlib import Path

BASELINE_FILE      = Path("truck_baseline.json")
# OPEN QUESTION (latent, becomes live the moment this is deployed): a relative path
# resolves against the CURRENT WORKING DIRECTORY. Run by hand from the repo root
# that is the repo root, which is what every use so far has been. Run from a systemd
# unit with no WorkingDirectory= — which is what both pi/obd_gatekeeper.service and
# pi/oled_display.service look like — CWD is "/", so the learner would attempt
# /truck_baseline.json, fail on permissions, and silently lose a calibration run.
# status() already reports the resolved path (str(self._file.resolve())); check it
# before starting any long calibration. See docs/HARDWARE_BRINGUP.md §3.7.

CALIBRATION_TARGET = 1000   # samples needed before is_calibrated == True
# OPEN HARDWARE QUESTION: this constant and the module docstring above describe two
# completely different calibration periods, and they cannot be reconciled at the
# rate this file actually samples. The docstring says "first 100 engine starts after
# install"; _run_calibration() sleeps 0.1s between samples, so 1000 samples is 100
# SECONDS — one uninterrupted minute and forty seconds. For "1000 samples ≈ 100
# starts" to hold, a single start would have to contribute ~10 samples, i.e. one
# second of engine run time each.
#
# This is not pedantry about a comment. 100 starts would span weeks and capture cold
# mornings, hot restarts, highway warm-up and seasonal swing — a representative
# baseline, which is the whole premise of the module. 100 seconds of one idle
# captures a single thermal state, and everything the truck subsequently does reads
# as an anomaly against it.
#
# Real hardware is needed to close this because the target has to be derived from
# the achievable sample rate on the actual bus, which is NOT the 10Hz assumed here —
# see the Z_SCORE_THRESHOLD note below. A per-start counter would serve the
# docstring's intent far better than a raw sample count, but that is a design change
# rather than a constant tweak. See docs/HARDWARE_BRINGUP.md §3.4.

# A deviation this many standard deviations from baseline is flagged
Z_SCORE_THRESHOLD  = 3.0
# OPEN HARDWARE QUESTION: 3.0 sigma is only meaningful if sigma itself is
# meaningful, and every baseline this project has ever built came from
# sierra_ecu_config.py — never from a vehicle. Three compounding reasons the
# emulator-derived sigma will not transfer:
#
#   1. SAMPLE RATE. _run_calibration() polls an in-process dict at 10Hz — zero
#      latency. The real path (archer.py obd_autodetect) sweeps 13 PIDs
#      sequentially, each with up to a 1.5s timeout, plus a 0.15s inter-sweep sleep,
#      over a 2006 GMT800's GM Class 2 / J1850 VPW bus at 10.4 kbit/s. A full sweep
#      is plausibly ~1s — roughly 1Hz, an order of magnitude slower than calibration
#      assumes. (README.md claims "every 200 ms"; that has never been measured.)
#
#   2. AUTOCORRELATION. The emulator's own step thread also runs at 10Hz, so
#      consecutive samples here are near-duplicates. Welford treats them as
#      independent observations, so the mean is estimated from ~1000 samples but the
#      VARIANCE from far fewer effective ones. The learned stddev comes out biased
#      low, sometimes drastically — and a 3-sigma threshold built on an artificially
#      small sigma is a hair trigger that real readings would trip constantly.
#
#   3. ZERO-VARIANCE FIELDS. Several emulator outputs are literal constants (boost
#      and ethanol are hardcoded 0). analyze() SKIPS any PID whose stddev is 0.0, so
#      those channels are not merely mis-scaled — they are excluded from anomaly
#      detection entirely, while the same channels on a real bus carry ordinary
#      sensor noise and would be checked.
#
# What real hardware settles: collect one genuine calibration run off the truck and
# compare per-PID stddev against the emulator-derived figures. If real sigma is
# consistently larger (it should be), any existing truck_baseline.json must be
# discarded and rebuilt on the vehicle, and the real-world false-positive rate at
# 3.0 measured before a single anomaly from this module is trusted.
# See docs/HARDWARE_BRINGUP.md §3.5.


class BaselineLearner:
    """
    Online running-statistics baseline for OBD2 PID values.

    Each PID key tracks: count, mean, M2 (Welford accumulator), min, max.
    Standard deviation is derived from M2 on demand (no full dataset stored).
    """

    def __init__(self, baseline_file: Path = BASELINE_FILE):
        self._file = Path(baseline_file)
        self._stats: dict = {}       # { pid_key: {count, mean, M2, min, max} }
        self._sample_count: int = 0
        self._load()

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _load(self):
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text())
                self._stats        = data.get("stats", {})
                self._sample_count = data.get("sample_count", 0)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[baseline] Warning: could not load {self._file}: {e}")
                self._stats        = {}
                self._sample_count = 0

    def _save(self):
        payload = {
            "sample_count":        self._sample_count,
            "calibration_target":  CALIBRATION_TARGET,
            "is_calibrated":       self.is_calibrated,
            "stats":               self._stats,
        }
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._file)

    # -----------------------------------------------------------------------
    # Core statistics (Welford online algorithm)
    # -----------------------------------------------------------------------

    def _update_stats(self, key: str, value: float):
        """Update running mean and variance for a single PID using Welford's method."""
        if key not in self._stats:
            self._stats[key] = {
                "count": 0,
                "mean":  0.0,
                "M2":    0.0,
                "min":   value,
                "max":   value,
            }

        s = self._stats[key]
        s["count"] += 1
        delta        = value - s["mean"]
        s["mean"]   += delta / s["count"]
        delta2       = value - s["mean"]
        s["M2"]     += delta * delta2
        s["min"]     = min(s["min"], value)
        s["max"]     = max(s["max"], value)

    @staticmethod
    def _stddev(entry: dict) -> float:
        """Population standard deviation from Welford accumulator."""
        if entry["count"] < 2:
            return 0.0
        return math.sqrt(entry["M2"] / entry["count"])

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def record_sample(self, pid_dict: dict):
        """
        Record one complete set of PID readings.  Non-numeric values are skipped.
        Saves to disk every 50 samples to avoid data loss.
        """
        # OPEN QUESTION, tied to CALIBRATION_TARGET above: this updates the Welford
        # accumulators UNCONDITIONALLY — is_calibrated gates nothing here. It is read
        # only by _run_calibration()'s while-condition and reported in status().
        #
        # That is fine for the standalone CLI, which stops looping once calibrated.
        # It is not fine for the wiring _run_calibration()'s own docstring
        # anticipates ("In production, record_sample() is called from the main Archer
        # data pipeline instead") — a pipeline that does not exist today: grepping
        # the tree, BaselineLearner and record_sample appear nowhere outside this
        # file. Nothing in archer.py, blueprints/, or pi/ ever constructs or feeds
        # it. Whoever wires it up will, without a guard here, fold every subsequent
        # reading — including the anomalies this module exists to catch — back into
        # the baseline. A slowly failing sensor would be learned as normal, which is
        # exactly the failure mode the module is meant to prevent. Best resolved
        # together with CALIBRATION_TARGET, since both turn on what "the calibration
        # period" is actually supposed to mean on a real truck.
        # See docs/HARDWARE_BRINGUP.md §3.8.
        for key, value in pid_dict.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._update_stats(key, float(value))

        self._sample_count += 1

        if self._sample_count % 50 == 0:
            self._save()

    def analyze(self, pid_dict: dict) -> list:
        """
        Compare a set of PID readings against the learned baseline.

        Returns a list of anomaly dicts for any PID that deviates more than
        Z_SCORE_THRESHOLD standard deviations from the baseline mean.

        Each anomaly:
          { "pid": str, "value": float, "baseline_mean": float,
            "baseline_stddev": float, "z_score": float, "direction": "HIGH"/"LOW" }
        """
        anomalies = []

        for key, value in pid_dict.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if key not in self._stats:
                continue

            entry  = self._stats[key]
            stddev = self._stddev(entry)
            if stddev == 0.0:
                continue

            z = (float(value) - entry["mean"]) / stddev
            if abs(z) >= Z_SCORE_THRESHOLD:
                anomalies.append({
                    "pid":              key,
                    "value":            value,
                    "baseline_mean":    round(entry["mean"], 4),
                    "baseline_stddev":  round(stddev, 4),
                    "z_score":          round(z, 3),
                    "direction":        "HIGH" if z > 0 else "LOW",
                })

        anomalies.sort(key=lambda a: abs(a["z_score"]), reverse=True)
        return anomalies

    @property
    def is_calibrated(self) -> bool:
        """True once at least CALIBRATION_TARGET samples have been recorded."""
        return self._sample_count >= CALIBRATION_TARGET

    def status(self) -> dict:
        """Return calibration progress and basic statistics."""
        progress = min(1.0, self._sample_count / CALIBRATION_TARGET)
        return {
            "sample_count":        self._sample_count,
            "calibration_target":  CALIBRATION_TARGET,
            "progress_pct":        round(progress * 100.0, 1),
            "is_calibrated":       self.is_calibrated,
            "pids_tracked":        len(self._stats),
            "baseline_file":       str(self._file.resolve()),
        }

    def report(self) -> dict:
        """Return full baseline statistics for every tracked PID."""
        result = {}
        for key, entry in self._stats.items():
            stddev = self._stddev(entry)
            result[key] = {
                "count":  entry["count"],
                "mean":   round(entry["mean"], 4),
                "stddev": round(stddev, 4),
                "min":    entry["min"],
                "max":    entry["max"],
            }
        return {
            "sample_count":   self._sample_count,
            "is_calibrated":  self.is_calibrated,
            "pid_baselines":  result,
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _run_calibration(learner: BaselineLearner):
    """
    Minimal standalone calibration loop.  In production, record_sample() is
    called from the main Archer data pipeline instead.
    """
    # OPEN HARDWARE QUESTION: this loop is the ONLY caller of record_sample() in the
    # repo, and it feeds the software emulator — never a vehicle. Beyond the rate and
    # autocorrelation problems noted at Z_SCORE_THRESHOLD, the two data sources do
    # not even agree on WHICH fields exist. _ecu.get_state() returns ~40 keys,
    # including strings and body-control state (engine_state, prndl, tcc_state,
    # airbag_status, door/seatbelt flags, TPMS pressures). The real OBD path
    # populates 13 Mode-01 PIDs plus battery voltage. So a baseline built here tracks
    # a large superset the real bus can never supply; analyze() skips keys it has no
    # stats for, so those entries just sit inert in truck_baseline.json.
    #
    # The practical consequence for bring-up: a truck_baseline.json produced by this
    # command is NOT a baseline of the truck and must not be carried over to the
    # vehicle. Real hardware is needed both to establish the achievable sample rate
    # and to produce a baseline over the field set the live path actually emits.
    # See docs/HARDWARE_BRINGUP.md §3.5 and §2.8.
    # Import the ECU emulator if available; otherwise abort with guidance.
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from sierra_ecu_config import _ecu
    except ImportError:
        print("[baseline] Cannot import sierra_ecu_config — run from Archer repo root.")
        sys.exit(1)

    print(f"[baseline] Starting calibration — target {CALIBRATION_TARGET} samples")
    print("[baseline] Press Ctrl+C to pause (progress is saved every 50 samples)")

    try:
        while not learner.is_calibrated:
            state = _ecu.get_state()
            learner.record_sample(state)
            s = learner.status()
            if s["sample_count"] % 100 == 0:
                print(
                    f"[baseline] {s['sample_count']}/{CALIBRATION_TARGET} samples "
                    f"({s['progress_pct']}%) — {s['pids_tracked']} PIDs tracked"
                )
            time.sleep(0.1)
    except KeyboardInterrupt:
        learner._save()
        print("\n[baseline] Paused. Resume with: python baseline_learner.py start")
        sys.exit(0)

    learner._save()
    print(f"[baseline] Calibration complete — baseline saved to {BASELINE_FILE}")


def _usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _usage()

    learner = BaselineLearner()
    cmd     = args[0]

    if cmd == "start":
        _run_calibration(learner)

    elif cmd == "status":
        s = learner.status()
        print(json.dumps(s, indent=2))

    elif cmd == "report":
        r = learner.report()
        print(json.dumps(r, indent=2))

    else:
        print(f"Unknown command: {cmd!r}")
        _usage()
