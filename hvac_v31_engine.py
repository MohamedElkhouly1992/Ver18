"""
HVAC v3.1 — DesignBuilder-Calibrated Baseline Solver
=====================================================

Purpose
-------
A deployable reduced-order baseline solver that improves daily agreement with
DesignBuilder by adding the physical layers missing from the earlier HVAC v3 core:

1. Envelope UA heat transfer
2. Infiltration / ventilation sensible load
3. Orientation-aware solar-gain proxy
4. Internal gains with schedule factor
5. Thermal-mass lag / smoothing
6. Heating-cooling deadband logic
7. PLR- and temperature-dependent COP
8. Separate fan, pump and auxiliary energy terms
9. Optional DesignBuilder calibration layer

This file is intentionally self-contained so it can be pasted/imported into the
existing HVAC v3 project.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
import json
import math

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass
class BuildingSpec:
    """Reduced building parameters extracted from DesignBuilder inputs."""

    floor_area_m2: float = 17948.18
    volume_m3: float = 97074.29
    n_zones: int = 312

    # Envelope values from the DesignBuilder building input report / prior extraction.
    # Units: W/K for heat-transfer coefficients.
    ua_walls_w_per_k: float = 22789.19
    ua_glazing_w_per_k: float = 2748.12
    ua_roof_w_per_k: float = 1484.36
    ua_external_floor_w_per_k: float = 82.13
    h_infiltration_w_per_k: float = 24495.56

    # Glazing / solar proxy.
    glazing_area_m2: float = 4000.18
    shgc: float = 0.248
    shade_factor: float = 0.72
    solar_cooling_fraction: float = 0.78

    # Orientation fractions. Used when only global GHI is available.
    glazing_orientation_fraction_n: float = 0.257
    glazing_orientation_fraction_e: float = 0.248
    glazing_orientation_fraction_s: float = 0.272
    glazing_orientation_fraction_w: float = 0.223

    # Internal gains. Adjust schedules rather than keeping these fully active all year.
    lighting_w_per_m2: float = 10.0
    equipment_w_per_m2: float = 8.0
    people_density_m2_per_person: float = 36.3
    sensible_w_per_person: float = 75.0

    # Setpoints and balance temperatures.
    heat_setpoint_c: float = 22.0
    cool_setpoint_c: float = 24.0
    heat_balance_c: float = 18.0
    cool_balance_c: float = 24.0

    # Thermal mass / lag coefficient: 0 = no lag, 1 = extremely slow response.
    thermal_mass_alpha: float = 0.55

    @property
    def ua_envelope_w_per_k(self) -> float:
        return self.ua_walls_w_per_k + self.ua_glazing_w_per_k + self.ua_roof_w_per_k + self.ua_external_floor_w_per_k

    @property
    def ua_total_w_per_k(self) -> float:
        return self.ua_envelope_w_per_k + self.h_infiltration_w_per_k


@dataclass
class HVACSpec:
    """HVAC equipment parameters for the baseline solver."""

    cooling_capacity_kw: float = 2697.66
    heating_capacity_kw: float = 1348.83
    nominal_cooling_cop: float = 4.50
    nominal_heating_efficiency_or_cop: float = 0.92  # DesignBuilder file reports Heating (Gas); use site-energy efficiency.

    # PLR curve multiplier for COP: f_plr = a + b*PLR + c*PLR^2. Clipped later.
    plr_a: float = 0.72
    plr_b: float = 0.55
    plr_c: float = -0.22
    min_plr_for_operation: float = 0.05

    # Outdoor-temperature COP modifier. Cooling COP degrades at high ambient temperature.
    cooling_temp_ref_c: float = 35.0
    cooling_temp_slope_per_c: float = 0.012

    # Fan/pump/auxiliary terms. These are calibrated later, but start physically.
    fan_base_kw: float = 32.8
    pump_base_kw: float = 1.20
    auxiliary_base_kw: float = 0.0
    operation_hours_per_day: float = 12.0
    fan_exponent: float = 3.0

    # Emissions/cost defaults.
    grid_emission_factor_kg_per_kwh: float = 0.536
    electricity_tariff_usd_per_kwh: float = 0.12


@dataclass
class CalibrationConfig:
    """Optional correction layer fitted against DesignBuilder."""

    monthly_factors: Dict[str, float]
    residual_coefficients: Dict[str, float]
    feature_names: Tuple[str, ...]
    residual_intercept: float = 0.0
    fan_scale: float = 1.0
    pump_scale: float = 1.0
    auxiliary_scale: float = 0.0
    clip_negative_energy: bool = True

    @staticmethod
    def empty() -> "CalibrationConfig":
        return CalibrationConfig(monthly_factors={}, residual_coefficients={}, feature_names=tuple())

    @staticmethod
    def load(path: str | Path) -> "CalibrationConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return CalibrationConfig(
            monthly_factors={str(k): float(v) for k, v in data.get("monthly_factors", {}).items()},
            residual_coefficients={str(k): float(v) for k, v in data.get("residual_coefficients", {}).items()},
            feature_names=tuple(data.get("feature_names", [])),
            residual_intercept=float(data.get("residual_intercept", 0.0)),
            fan_scale=float(data.get("fan_scale", 1.0)),
            pump_scale=float(data.get("pump_scale", 1.0)),
            auxiliary_scale=float(data.get("auxiliary_scale", 0.0)),
            clip_negative_energy=bool(data.get("clip_negative_energy", True)),
        )

    def save(self, path: str | Path) -> None:
        payload = {
            "monthly_factors": self.monthly_factors,
            "residual_coefficients": self.residual_coefficients,
            "feature_names": list(self.feature_names),
            "residual_intercept": self.residual_intercept,
            "fan_scale": self.fan_scale,
            "pump_scale": self.pump_scale,
            "auxiliary_scale": self.auxiliary_scale,
            "clip_negative_energy": self.clip_negative_energy,
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Input normalization helpers
# -----------------------------------------------------------------------------

def _first_existing_column(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        key = name.strip().lower()
        if key in lower_map:
            return lower_map[key]
    return None


def normalize_weather_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Return a standard daily weather/driver dataframe.

    Accepted input column alternatives:
    - date: Date/Time, date, timestamp
    - outdoor temperature: T_amb_C, T_mean_C, Outside Dry-Bulb Temperature
    - RH: RH_mean_pct
    - solar: GHI_mean_Wm2
    - occupancy: occ, occupancy_factor
    """
    out = pd.DataFrame(index=df.index.copy())

    date_col = _first_existing_column(df, ["Date/Time", "date", "timestamp", "datetime"])
    if date_col:
        out["date"] = pd.to_datetime(df[date_col], errors="coerce")

    t_col = _first_existing_column(df, ["T_amb_C", "T_mean_C", "Outside Dry-Bulb Temperature", "outdoor_temp_c", "T_out"])
    if t_col is None:
        raise ValueError("No outdoor-temperature column found. Expected T_amb_C, T_mean_C, or Outside Dry-Bulb Temperature.")
    out["T_out_C"] = pd.to_numeric(df[t_col], errors="coerce")

    tmax_col = _first_existing_column(df, ["T_max_C", "Tmax_C", "max_temp_c"])
    out["T_max_C"] = pd.to_numeric(df[tmax_col], errors="coerce") if tmax_col else out["T_out_C"] + 5.0

    rh_col = _first_existing_column(df, ["RH_mean_pct", "RH", "relative_humidity", "Relative Humidity"])
    out["RH_pct"] = pd.to_numeric(df[rh_col], errors="coerce") if rh_col else 60.0

    ghi_col = _first_existing_column(df, ["GHI_mean_Wm2", "GHI", "solar", "global_horizontal_irradiance"])
    out["GHI_Wm2"] = pd.to_numeric(df[ghi_col], errors="coerce") if ghi_col else 0.0

    occ_col = _first_existing_column(df, ["occ", "occupancy_factor", "Occupancy", "occupancy"])
    out["occ"] = pd.to_numeric(df[occ_col], errors="coerce") if occ_col else np.nan

    doy_col = _first_existing_column(df, ["day_of_year", "doy"])
    if doy_col:
        out["day_of_year"] = pd.to_numeric(df[doy_col], errors="coerce").astype("Int64")
    elif "date" in out:
        out["day_of_year"] = out["date"].dt.dayofyear.astype("Int64")
    else:
        out["day_of_year"] = (np.arange(len(out)) % 365 + 1).astype(int)

    year_col = _first_existing_column(df, ["year"])
    if year_col:
        # Some solver files use year = 1..5. Keep it as simulation_year if not calendar year.
        out["year"] = pd.to_numeric(df[year_col], errors="coerce").astype("Int64")
    elif "date" in out:
        out["year"] = out["date"].dt.year.astype("Int64")
    else:
        out["year"] = (np.arange(len(out)) // 365 + 1).astype(int)

    if out["occ"].isna().all():
        out["occ"] = default_schedule_factor(out.get("date"), out["day_of_year"])
    else:
        out["occ"] = out["occ"].fillna(default_schedule_factor(out.get("date"), out["day_of_year"]))

    # Fill gaps conservatively.
    for c in ["T_out_C", "T_max_C", "RH_pct", "GHI_Wm2", "occ"]:
        out[c] = out[c].interpolate(limit_direction="both").fillna(out[c].median())

    return out.reset_index(drop=True)


def default_schedule_factor(date_series: Optional[pd.Series], doy_series: pd.Series) -> pd.Series:
    """Simple educational-building schedule factor.

    Higher during working/teaching periods, reduced on weekends and summer/vacation.
    """
    n = len(doy_series)
    if date_series is not None:
        weekday = pd.to_datetime(date_series, errors="coerce").dt.weekday.fillna(0).astype(int)
        weekend = weekday >= 5
    else:
        idx = np.arange(n)
        weekend = (idx % 7 >= 5)

    doy_default = pd.Series((np.arange(n) % 365) + 1, index=doy_series.index)
    doy = pd.to_numeric(doy_series, errors="coerce").fillna(doy_default).astype(int)
    summer_reduced = ((doy >= 170) & (doy <= 250))
    winter_break = ((doy <= 20) | ((doy >= 350) & (doy <= 365)))

    occ = np.where(weekend, 0.22, 1.00)
    occ = np.where(summer_reduced, occ * 0.65, occ)
    occ = np.where(winter_break, occ * 0.75, occ)
    return pd.Series(np.clip(occ, 0.05, 1.0), index=doy_series.index)


# -----------------------------------------------------------------------------
# Physics baseline calculation
# -----------------------------------------------------------------------------

def plr_cop_modifier(plr: np.ndarray, hvac: HVACSpec) -> np.ndarray:
    f = hvac.plr_a + hvac.plr_b * plr + hvac.plr_c * plr**2
    return np.clip(f, 0.45, 1.15)


def cooling_temperature_modifier(t_out_c: np.ndarray, hvac: HVACSpec) -> np.ndarray:
    # COP decreases above reference and is capped to prevent unphysical values.
    f = 1.0 - hvac.cooling_temp_slope_per_c * (t_out_c - hvac.cooling_temp_ref_c)
    return np.clip(f, 0.65, 1.15)


def compute_v31_baseline(
    weather_or_driver_df: pd.DataFrame,
    building: BuildingSpec | None = None,
    hvac: HVACSpec | None = None,
    calibration: CalibrationConfig | None = None,
) -> pd.DataFrame:
    """Compute daily HVAC baseline energy using the v3.1 physical layers.

    Parameters
    ----------
    weather_or_driver_df:
        Daily dataframe containing at least outdoor temperature. GHI, RH, occ and
        date/day_of_year improve accuracy.
    building, hvac:
        Dataclass instances. Defaults use the user's DesignBuilder report values.
    calibration:
        Optional fitted calibration config generated by ``calibrate_hvac_v31.py``.

    Returns
    -------
    pandas.DataFrame with daily component and total energy columns.
    """
    building = building or BuildingSpec()
    hvac = hvac or HVACSpec()
    calibration = calibration or CalibrationConfig.empty()

    w = normalize_weather_dataframe(weather_or_driver_df)
    n = len(w)
    t = w["T_out_C"].to_numpy(float)
    ghi = w["GHI_Wm2"].to_numpy(float)
    occ = np.clip(w["occ"].to_numpy(float), 0.0, 1.2)
    doy = w["day_of_year"].to_numpy(float)

    # Degree-day / mode features.
    hdd = np.maximum(building.heat_balance_c - t, 0.0)
    cdd = np.maximum(t - building.cool_balance_c, 0.0)
    deadband = ((t >= building.heat_balance_c) & (t <= building.cool_balance_c)).astype(float)

    ua_kw_per_k = building.ua_total_w_per_k / 1000.0
    ua_env_kw_per_k = building.ua_envelope_w_per_k / 1000.0
    inf_kw_per_k = building.h_infiltration_w_per_k / 1000.0

    # Envelope + infiltration sensible loads (kWh/day).
    heat_env_kwh = ua_env_kw_per_k * np.maximum(building.heat_setpoint_c - t, 0.0) * 24.0
    cool_env_kwh = ua_env_kw_per_k * np.maximum(t - building.cool_setpoint_c, 0.0) * 24.0
    heat_inf_kwh = inf_kw_per_k * np.maximum(building.heat_setpoint_c - t, 0.0) * 24.0
    cool_inf_kwh = inf_kw_per_k * np.maximum(t - building.cool_setpoint_c, 0.0) * 24.0

    # Solar gain. If GHI is a daily-average W/m2 proxy, convert using daylight factor.
    daylight_hours = 10.8 + 2.2 * np.sin(2 * np.pi * (doy - 80) / 365.0)
    solar_raw_kwh = building.glazing_area_m2 * building.shgc * building.shade_factor * ghi * daylight_hours / 1000.0
    # orientation proxy increases summer/east-west daily variability.
    orientation_factor = 0.88 + 0.12 * np.sin(2 * np.pi * (doy - 172) / 365.0)
    solar_gain_kwh = solar_raw_kwh * orientation_factor

    # Internal gains weighted by educational schedule.
    people_n = building.floor_area_m2 / building.people_density_m2_per_person
    people_gain_kwh = people_n * building.sensible_w_per_person / 1000.0 * hvac.operation_hours_per_day * occ
    lighting_gain_kwh = building.floor_area_m2 * building.lighting_w_per_m2 / 1000.0 * hvac.operation_hours_per_day * occ
    equipment_gain_kwh = building.floor_area_m2 * building.equipment_w_per_m2 / 1000.0 * hvac.operation_hours_per_day * occ
    internal_gain_kwh = people_gain_kwh + lighting_gain_kwh + equipment_gain_kwh

    # Thermal mass: smooth the net cooling driver so peaks are shifted/damped.
    cooling_driver = cool_env_kwh + cool_inf_kwh + building.solar_cooling_fraction * solar_gain_kwh + 0.85 * internal_gain_kwh
    heating_driver = heat_env_kwh + heat_inf_kwh - 0.55 * solar_gain_kwh - 0.55 * internal_gain_kwh
    heating_driver = np.maximum(heating_driver, 0.0)

    cooling_lag = np.zeros(n)
    heating_lag = np.zeros(n)
    for i in range(n):
        if i == 0:
            cooling_lag[i] = cooling_driver[i]
            heating_lag[i] = heating_driver[i]
        else:
            a = building.thermal_mass_alpha
            cooling_lag[i] = a * cooling_lag[i - 1] + (1 - a) * cooling_driver[i]
            heating_lag[i] = a * heating_lag[i - 1] + (1 - a) * heating_driver[i]

    # Use mode weights rather than hard exclusive mode to handle shoulder days.
    cool_weight = np.clip((t - building.heat_balance_c) / max(building.cool_balance_c - building.heat_balance_c, 0.1), 0, 1)
    heat_weight = 1.0 - cool_weight
    shoulder_weight = deadband * 0.35

    q_cool_kwh = np.maximum(cooling_lag * (0.55 + 0.45 * cool_weight + shoulder_weight), 0.0)
    q_heat_kwh = np.maximum(heating_lag * (0.65 + 0.35 * heat_weight), 0.0)

    # Limit by capacity and operating hours but keep daily energy finite.
    q_cool_kwh = np.minimum(q_cool_kwh, hvac.cooling_capacity_kw * 24.0)
    q_heat_kwh = np.minimum(q_heat_kwh, hvac.heating_capacity_kw * 24.0)

    plr_cool = np.clip((q_cool_kwh / max(hvac.operation_hours_per_day, 1.0)) / hvac.cooling_capacity_kw, 0.0, 1.2)
    plr_heat = np.clip((q_heat_kwh / max(hvac.operation_hours_per_day, 1.0)) / hvac.heating_capacity_kw, 0.0, 1.2)

    cop_cool = hvac.nominal_cooling_cop * plr_cop_modifier(plr_cool, hvac) * cooling_temperature_modifier(t, hvac)
    cop_cool = np.clip(cop_cool, 1.5, 7.0)

    cooling_electric_kwh = q_cool_kwh / cop_cool
    heating_energy_kwh = q_heat_kwh / max(hvac.nominal_heating_efficiency_or_cop, 0.1)

    # Operation and air-flow proxy.
    operation_factor = np.clip(0.18 + 0.82 * occ, 0.05, 1.0)
    alpha_flow = np.clip(0.45 + 0.55 * np.maximum(plr_cool, plr_heat), 0.45, 1.0)
    fan_kwh = hvac.fan_base_kw * hvac.operation_hours_per_day * operation_factor * np.power(alpha_flow, hvac.fan_exponent)
    pump_kwh = hvac.pump_base_kw * hvac.operation_hours_per_day * operation_factor * np.maximum(plr_cool, plr_heat)
    aux_kwh = hvac.auxiliary_base_kw * hvac.operation_hours_per_day * operation_factor

    # Apply component scales from calibration if provided.
    fan_kwh = fan_kwh * calibration.fan_scale
    pump_kwh = pump_kwh * calibration.pump_scale
    aux_kwh = aux_kwh * calibration.auxiliary_scale

    total_raw = cooling_electric_kwh + heating_energy_kwh + fan_kwh + pump_kwh + aux_kwh

    out = w.copy()
    if "date" not in out.columns:
        out["date"] = pd.date_range("2020-01-01", periods=n, freq="D")
    month_default = pd.Series(((np.arange(n) % 365) // 30 + 1), index=out.index)
    out["month"] = pd.to_datetime(out["date"], errors="coerce").dt.month.fillna(month_default).astype(int)

    out["HDD"] = hdd
    out["CDD"] = cdd
    out["Q_env_heat_kwh"] = heat_env_kwh
    out["Q_env_cool_kwh"] = cool_env_kwh
    out["Q_inf_heat_kwh"] = heat_inf_kwh
    out["Q_inf_cool_kwh"] = cool_inf_kwh
    out["Q_solar_gain_kwh"] = solar_gain_kwh
    out["Q_internal_gain_kwh"] = internal_gain_kwh
    out["Q_cool_load_kwh"] = q_cool_kwh
    out["Q_heat_load_kwh"] = q_heat_kwh
    out["PLR_cool"] = plr_cool
    out["PLR_heat"] = plr_heat
    out["COP_cool_eff"] = cop_cool
    out["cooling_electricity_kwh"] = cooling_electric_kwh
    out["heating_energy_kwh"] = heating_energy_kwh
    out["fan_kwh"] = fan_kwh
    out["pump_kwh"] = pump_kwh
    out["auxiliary_kwh"] = aux_kwh
    out["energy_kwh_v31_physics"] = total_raw

    out = apply_calibration_layer(out, calibration)
    out["co2_kg_v31"] = out["energy_kwh_v31_final"] * hvac.grid_emission_factor_kg_per_kwh
    out["cost_usd_v31"] = out["energy_kwh_v31_final"] * hvac.electricity_tariff_usd_per_kwh
    return out


def build_residual_features(df: pd.DataFrame) -> pd.DataFrame:
    """Feature matrix for residual calibration.

    The model is deliberately simple and transparent: weather, degree-days,
    schedule, seasonality and the physics prediction.
    """
    out = pd.DataFrame(index=df.index)
    out["physics"] = pd.to_numeric(df.get("energy_kwh_v31_seasonal", df.get("energy_kwh_v31_physics")), errors="coerce")
    out["T_out_C"] = pd.to_numeric(df.get("T_out_C"), errors="coerce")
    out["RH_pct"] = pd.to_numeric(df.get("RH_pct"), errors="coerce")
    out["GHI_Wm2"] = pd.to_numeric(df.get("GHI_Wm2"), errors="coerce")
    out["occ"] = pd.to_numeric(df.get("occ"), errors="coerce")
    out["HDD"] = pd.to_numeric(df.get("HDD"), errors="coerce")
    out["CDD"] = pd.to_numeric(df.get("CDD"), errors="coerce")
    doy_default = pd.Series((np.arange(len(df)) % 365) + 1, index=df.index)
    doy = pd.to_numeric(df.get("day_of_year"), errors="coerce").fillna(doy_default)
    out["sin_doy"] = np.sin(2 * np.pi * doy / 365.0)
    out["cos_doy"] = np.cos(2 * np.pi * doy / 365.0)
    month = pd.to_numeric(df.get("month"), errors="coerce").fillna(1).astype(int)
    for m in range(1, 13):
        out[f"month_{m:02d}"] = (month == m).astype(float)
    return out.fillna(0.0)


def apply_calibration_layer(df: pd.DataFrame, calibration: CalibrationConfig) -> pd.DataFrame:
    """Apply monthly and residual calibration to v3.1 physics outputs."""
    out = df.copy()
    if calibration.monthly_factors:
        factors = out["month"].astype(int).astype(str).map(calibration.monthly_factors).fillna(1.0).astype(float)
    else:
        factors = 1.0
    out["monthly_factor"] = factors
    out["energy_kwh_v31_seasonal"] = out["energy_kwh_v31_physics"] * factors

    if calibration.residual_coefficients:
        X = build_residual_features(out)
        residual = np.full(len(out), calibration.residual_intercept, dtype=float)
        for name, coef in calibration.residual_coefficients.items():
            if name in X.columns:
                residual += X[name].to_numpy(float) * float(coef)
        out["residual_correction_kwh"] = residual
    else:
        out["residual_correction_kwh"] = 0.0

    out["energy_kwh_v31_final"] = out["energy_kwh_v31_seasonal"] + out["residual_correction_kwh"]
    if calibration.clip_negative_energy:
        out["energy_kwh_v31_final"] = out["energy_kwh_v31_final"].clip(lower=0.0)
    return out


# -----------------------------------------------------------------------------
# Validation metrics
# -----------------------------------------------------------------------------

def validation_metrics(y_true: Iterable[float], y_pred: Iterable[float]) -> Dict[str, float]:
    y = np.asarray(list(y_true), dtype=float)
    p = np.asarray(list(y_pred), dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    y = y[mask]
    p = p[mask]
    n = len(y)
    if n == 0:
        raise ValueError("No valid records for metrics.")
    err = p - y
    mean_y = np.mean(y)
    total_y = np.sum(y)
    return {
        "n_records": float(n),
        "total_designbuilder_kwh": float(total_y),
        "total_model_kwh": float(np.sum(p)),
        "total_difference_kwh": float(np.sum(p) - total_y),
        "overall_percentage_error_pct": float((np.sum(p) - total_y) / total_y * 100.0) if total_y else np.nan,
        "NMBE_pct": float(np.sum(err) / ((n - 1) * mean_y) * 100.0) if n > 1 and mean_y else np.nan,
        "MAPE_pct": float(np.mean(np.abs(err / np.where(np.abs(y) < 1e-9, np.nan, y))) * 100.0),
        "WMAPE_pct": float(np.sum(np.abs(err)) / np.sum(np.abs(y)) * 100.0),
        "CVRMSE_pct": float(np.sqrt(np.mean(err**2)) / mean_y * 100.0) if mean_y else np.nan,
        "MAE_kwh": float(np.mean(np.abs(err))),
        "RMSE_kwh": float(np.sqrt(np.mean(err**2))),
        "mean_daily_percentage_error_pct": float(np.nanmean(err / np.where(np.abs(y) < 1e-9, np.nan, y)) * 100.0),
    }


def metrics_dataframe(cases: Dict[str, Tuple[Iterable[float], Iterable[float]]]) -> pd.DataFrame:
    rows = []
    for name, (y, p) in cases.items():
        row = {"case": name}
        row.update(validation_metrics(y, p))
        rows.append(row)
    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# Config helpers
# -----------------------------------------------------------------------------

def save_default_config(path: str | Path) -> None:
    payload = {"building": asdict(BuildingSpec()), "hvac": asdict(HVACSpec())}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_config(path: str | Path) -> Tuple[BuildingSpec, HVACSpec]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    building = BuildingSpec(**payload.get("building", {}))
    hvac = HVACSpec(**payload.get("hvac", {}))
    return building, hvac


if __name__ == "__main__":
    save_default_config("sample_hvac_v31_config.json")
    print("Wrote sample_hvac_v31_config.json")
