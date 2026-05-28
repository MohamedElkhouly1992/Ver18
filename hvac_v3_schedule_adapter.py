"""Adapter utilities to connect DesignBuilder schedule outputs to HVAC v3/v3.1 drivers.

This file does not assume the internal signature of your hvac_v3.py solver.
It safely prepares the daily driver CSV that the solver should read.
"""
from __future__ import annotations

from pathlib import Path
import importlib.util
import pandas as pd


def merge_schedule_to_driver(driver_csv: str | Path, schedule_csv: str | Path, output_csv: str | Path, date_col: str = "date") -> str:
    driver = pd.read_csv(driver_csv)
    sched = pd.read_csv(schedule_csv)
    if date_col not in driver.columns:
        raise ValueError(f"Driver file has no '{date_col}' column. Add a date column or pass date_col correctly.")
    driver[date_col] = pd.to_datetime(driver[date_col]).dt.strftime("%Y-%m-%d")
    sched[date_col] = pd.to_datetime(sched[date_col]).dt.strftime("%Y-%m-%d")
    # Drop overlapping schedule columns from driver, except date, to avoid duplicated year/month etc.
    overlap = [c for c in sched.columns if c in driver.columns and c != date_col]
    driver = driver.drop(columns=overlap, errors="ignore")
    merged = driver.merge(sched, on=date_col, how="left")
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_csv, index=False)
    return str(output_csv)


def import_hvac_v3_module(hvac_v3_path: str | Path):
    hvac_v3_path = Path(hvac_v3_path)
    spec = importlib.util.spec_from_file_location("hvac_v3_user_core", hvac_v3_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import {hvac_v3_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_known_solver_function(hvac_v3_path: str | Path, driver_csv: str | Path, output_dir: str | Path, function_name: str = "run_scenario_model"):
    """Best-effort runner for user hvac_v3.py.

    Because different versions of hvac_v3.py may have different function signatures,
    this function tries common patterns. If it cannot run the solver, it raises a
    clear error and leaves the enriched driver CSV ready for manual use.
    """
    module = import_hvac_v3_module(hvac_v3_path)
    if not hasattr(module, function_name):
        raise AttributeError(f"{hvac_v3_path} has no function named {function_name}. Use the enriched driver CSV directly or pass the correct function name.")
    fn = getattr(module, function_name)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Try common signatures.
    attempts = [
        lambda: fn(input_csv=str(driver_csv), output_dir=str(output_dir)),
        lambda: fn(driver_csv=str(driver_csv), output_dir=str(output_dir)),
        lambda: fn(str(driver_csv), str(output_dir)),
        lambda: fn(pd.read_csv(driver_csv)),
    ]
    last = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as e:
            last = e
            continue
    raise TypeError(f"Could not call {function_name} with common signatures. Last TypeError: {last}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Merge DesignBuilder operational schedule into HVAC v3 driver CSV and optionally run hvac_v3.py.")
    parser.add_argument("--driver_csv", required=True)
    parser.add_argument("--schedule_csv", required=True)
    parser.add_argument("--output_csv", default="outputs/hvac_v3_driver_with_db_schedule.csv")
    parser.add_argument("--hvac_v3_path", default=None)
    parser.add_argument("--function_name", default="run_scenario_model")
    parser.add_argument("--output_dir", default="outputs/hvac_v3_run")
    args = parser.parse_args()
    merged = merge_schedule_to_driver(args.driver_csv, args.schedule_csv, args.output_csv)
    print(f"Enriched driver written to: {merged}")
    if args.hvac_v3_path:
        result = run_known_solver_function(args.hvac_v3_path, merged, args.output_dir, args.function_name)
        print("Solver result:", result)
