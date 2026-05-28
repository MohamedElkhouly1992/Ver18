"""
HVAC v3.1 schedule + seasonal patch.

How to use:
1) Generate designbuilder_extracted_schedule_seasonal.json with extract_db_schedule_seasonal.py.
2) Import these functions into hvac_v31_engine.py or your core solver.
3) Apply schedule_factor inside internal gains and operation hours.
4) Apply seasonal factor after the physics energy is calculated.

This patch is intentionally small and transparent. It should not replace physical
loads; it supplies DesignBuilder-informed schedule and seasonality controls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd


def load_db_schedule_seasonal_config(path: str | Path) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def schedule_factor_from_config(dates, config: Dict) -> pd.Series:
    """Return daily occupancy/operation factor from the extracted DB activity proxy.

    Exact DesignBuilder hourly schedules are not in the building CSV, so this is a
    transparent proxy. If you later export actual DB schedules, replace this function.
    """
    s = config.get("schedule_proxy", {})
    dates = pd.to_datetime(pd.Series(dates), errors="coerce")
    n = len(dates)
    doy = dates.dt.dayofyear.fillna(pd.Series((np.arange(n) % 365) + 1)).astype(int)
    weekday = dates.dt.weekday.fillna(pd.Series(np.arange(n) % 7)).astype(int)

    weekday_factor = float(s.get("weekday_occupied_factor", 1.0))
    weekend_factor = float(s.get("weekend_factor", 0.22))
    summer_factor = float(s.get("summer_reduced_factor", 0.65))
    winter_factor = float(s.get("winter_break_factor", 0.75))

    out = np.where(weekday >= 5, weekend_factor, weekday_factor).astype(float)

    summer_range = s.get("summer_reduced_doy_range", [170, 250])
    summer = (doy >= int(summer_range[0])) & (doy <= int(summer_range[1]))
    out = np.where(summer, out * summer_factor, out)

    winter = np.zeros(n, dtype=bool)
    for r in s.get("winter_break_doy_ranges", [[1, 20], [350, 365]]):
        winter |= ((doy >= int(r[0])) & (doy <= int(r[1]))).to_numpy()
    out = np.where(winter, out * winter_factor, out)

    return pd.Series(np.clip(out, 0.05, 1.0), index=dates.index, name="schedule_factor")


def monthly_target_factor(dates_or_month, config: Dict) -> pd.Series:
    """Reference seasonal target profile: monthly mean daily DB energy / annual mean daily DB energy."""
    seasonal = config.get("seasonal", {})
    factors = seasonal.get("monthly_target_profile_factor", {})
    if len(factors) == 0:
        return pd.Series(1.0, index=pd.RangeIndex(len(dates_or_month)))
    s = pd.Series(dates_or_month)
    if np.issubdtype(s.dropna().dtype, np.number):
        month = s.astype(int)
    else:
        month = pd.to_datetime(s, errors="coerce").dt.month.fillna(1).astype(int)
    return month.astype(str).map({str(k): float(v) for k, v in factors.items()}).fillna(1.0).astype(float)


def monthly_correction_factor(dates_or_month, config: Dict, clip_low: float = 0.25, clip_high: float = 3.0) -> pd.Series:
    """Direct monthly correction factor for the current physics solver.

    This is calibration, not pure physics. Use only after reporting physics-only results.
    Refit if the core physics model changes.
    """
    seasonal = config.get("seasonal", {})
    factors = seasonal.get("monthly_correction_factor_for_current_physics", {})
    if len(factors) == 0:
        return pd.Series(1.0, index=pd.RangeIndex(len(dates_or_month)))
    s = pd.Series(dates_or_month)
    if np.issubdtype(s.dropna().dtype, np.number):
        month = s.astype(int)
    else:
        month = pd.to_datetime(s, errors="coerce").dt.month.fillna(1).astype(int)
    return month.astype(str).map({str(k): float(v) for k, v in factors.items()}).fillna(1.0).clip(clip_low, clip_high).astype(float)


def apply_designbuilder_schedule_to_driver(driver_df: pd.DataFrame, config: Dict, date_col: str = "date") -> pd.DataFrame:
    """Add or overwrite occ/schedule factor before running hvac_v31_engine.compute_v31_baseline."""
    out = driver_df.copy()
    if date_col not in out.columns:
        out[date_col] = pd.date_range("2020-01-01", periods=len(out), freq="D")
    out["occ"] = schedule_factor_from_config(out[date_col], config)
    out["schedule_factor_db_proxy"] = out["occ"]
    return out


def apply_designbuilder_seasonal_correction(result_df: pd.DataFrame, config: Dict, energy_col: str = "energy_kwh_v31_physics") -> pd.DataFrame:
    """Apply direct monthly correction to a physics result dataframe."""
    out = result_df.copy()
    if "month" in out.columns:
        key = out["month"]
    elif "date" in out.columns:
        key = out["date"]
    else:
        key = pd.Series(((np.arange(len(out)) % 365) // 30 + 1)).clip(1, 12)
    f = monthly_correction_factor(key, config)
    out["db_monthly_correction_factor"] = f.to_numpy()
    out["energy_kwh_v31_db_seasonal"] = out[energy_col].astype(float) * out["db_monthly_correction_factor"]
    return out
