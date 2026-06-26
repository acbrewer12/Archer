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
CALIBRATION_TARGET = 1000   # samples needed before is_calibrated == True

# A deviation this many standard deviations from baseline is flagged
Z_SCORE_THRESHOLD  = 3.0


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
