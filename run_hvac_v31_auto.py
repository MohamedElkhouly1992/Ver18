"""Auto-runner for HVAC v3.1 bundle.

Run from inside the bundle folder:
    python run_hvac_v31_auto.py

The script searches the examples folder for:
- a DesignBuilder workbook (*.xlsx)
- a solver/weather CSV, preferably baseline_no_degradation_daily.csv
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def pick_file(folder: Path, patterns):
    for pat in patterns:
        hits = sorted(folder.glob(pat))
        if hits:
            return hits[0]
    return None


def main():
    root = Path(__file__).resolve().parent
    examples = root / "examples"
    db = pick_file(examples, ["*Design*Builder*.xlsx", "*.xlsx"])
    driver = pick_file(examples, ["baseline_no_degradation_daily.csv", "site_data.csv", "baseline_daily_weather.csv", "weather_timeseries.csv", "*.csv"])
    if db is None or driver is None:
        raise SystemExit("Could not auto-detect required files in examples/. Please use calibrate_hvac_v31_designbuilder.py manually.")
    cmd = [
        sys.executable,
        str(root / "calibrate_hvac_v31_designbuilder.py"),
        "--designbuilder_xlsx", str(db),
        "--weather_or_solver_csv", str(driver),
        "--output_dir", str(root / "outputs"),
        "--train_years", "2020,2021,2022,2023",
        "--validate_years", "2024",
        "--residual_alpha", "100",
        "--max_lag_days", "7",
    ]
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print("\nDone. Open outputs/hvac_v31_validation_report.md and outputs/hvac_v31_metrics_before_after.csv")


if __name__ == "__main__":
    main()
