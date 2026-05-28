"""
Calibrate and validate HVAC v3.1 against DesignBuilder daily outputs.

Example:
python calibrate_hvac_v31_designbuilder.py \
  --designbuilder_xlsx "examples/ALL DATA - Design builder Data.xlsx" \
  --weather_or_solver_csv "examples/baseline_no_degradation_daily.csv" \
  --output_dir outputs \
  --train_years 2020,2021,2022,2023 \
  --validate_years 2024
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from hvac_v31_engine import (
    BuildingSpec,
    HVACSpec,
    CalibrationConfig,
    compute_v31_baseline,
    build_residual_features,
    metrics_dataframe,
    validation_metrics,
    load_config,
)


def read_designbuilder_daily(path: str | Path) -> pd.DataFrame:
    """Read DesignBuilder workbook and return daily total + components in kWh.

    Expected workbook variants:
    - Sheet2 with columns Date/Time, Total, Total.1
    - Sheet1 with DesignBuilder component columns
    - ALL DATA sheet with component columns
    """
    path = Path(path)
    xls = pd.ExcelFile(path)
    chosen = None
    for sheet in ["Sheet2", "Sheet1", "ALL DATA"] + xls.sheet_names:
        if sheet not in xls.sheet_names:
            continue
        df = pd.read_excel(path, sheet_name=sheet)
        if "Date/Time" in df.columns:
            chosen = df.copy()
            break
    if chosen is None:
        raise ValueError("Could not find a DesignBuilder sheet with a Date/Time column.")

    df = chosen.copy()
    df["date"] = pd.to_datetime(df["Date/Time"], errors="coerce")
    df = df[df["date"].notna()].reset_index(drop=True)

    # Convert possible object/unit rows to numeric.
    for c in df.columns:
        if c not in ["Date/Time", "date"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "Total" in df.columns and df["Total"].notna().sum() > 0:
        total = df["Total"]
    else:
        comp_cols = ["System Fans", "System Pumps", "Auxiliary Energy", "Heating (Gas)", "Cooling (Electricity)"]
        existing = [c for c in comp_cols if c in df.columns]
        if not existing:
            raise ValueError("Could not find total or component DesignBuilder energy columns.")
        total = df[existing].sum(axis=1, min_count=1)

    out = pd.DataFrame({"date": df["date"], "db_total_kwh": total})
    component_map = {
        "System Fans": "db_fan_kwh",
        "System Pumps": "db_pump_kwh",
        "Auxiliary Energy": "db_auxiliary_kwh",
        "Heating (Gas)": "db_heating_kwh",
        "Cooling (Electricity)": "db_cooling_kwh",
        "Outside Dry-Bulb Temperature": "db_outdoor_temp_c",
    }
    for src, dst in component_map.items():
        if src in df.columns:
            out[dst] = df[src]
    out["year"] = out["date"].dt.year
    out["month"] = out["date"].dt.month
    out["day_of_year"] = out["date"].dt.dayofyear
    out = out.dropna(subset=["db_total_kwh"]).reset_index(drop=True)
    return out


def parse_years_arg(value: str | None) -> List[int] | None:
    if not value:
        return None
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def fit_monthly_factors(train_df: pd.DataFrame, y_col: str, p_col: str) -> Dict[str, float]:
    factors = {}
    for m, g in train_df.groupby("month"):
        denom = g[p_col].sum()
        if abs(denom) < 1e-9:
            factors[str(int(m))] = 1.0
        else:
            factors[str(int(m))] = float(np.clip(g[y_col].sum() / denom, 0.25, 3.0))
    return factors


def fit_ridge_residual(train_df: pd.DataFrame, target_col: str, alpha: float = 100.0) -> Tuple[float, Dict[str, float], List[str]]:
    Xdf = build_residual_features(train_df)
    feature_names = list(Xdf.columns)
    X = Xdf.to_numpy(float)
    y = train_df[target_col].to_numpy(float)
    # Standardize features for stable ridge, but return coefficients in original scale.
    mu = X.mean(axis=0)
    sigma = X.std(axis=0)
    sigma[sigma == 0] = 1.0
    Xs = (X - mu) / sigma
    X_design = np.column_stack([np.ones(len(Xs)), Xs])
    penalty = np.eye(X_design.shape[1]) * alpha
    penalty[0, 0] = 0.0
    beta_s = np.linalg.solve(X_design.T @ X_design + penalty, X_design.T @ y)
    intercept_s = beta_s[0]
    coef_original = beta_s[1:] / sigma
    intercept_original = intercept_s - np.sum((beta_s[1:] * mu) / sigma)
    return float(intercept_original), {n: float(c) for n, c in zip(feature_names, coef_original)}, feature_names


def detect_best_lag(db: pd.Series, model: pd.Series, max_lag_days: int = 7) -> Tuple[int, pd.DataFrame]:
    rows = []
    y = pd.Series(db).reset_index(drop=True)
    p0 = pd.Series(model).reset_index(drop=True)
    for lag in range(-max_lag_days, max_lag_days + 1):
        p = p0.shift(lag)
        mask = y.notna() & p.notna()
        if mask.sum() < 10:
            continue
        m = validation_metrics(y[mask], p[mask])
        rows.append({"lag_days": lag, **m})
    table = pd.DataFrame(rows).sort_values("CVRMSE_pct")
    best = int(table.iloc[0]["lag_days"])
    return best, table


def align_designbuilder_and_driver(db: pd.DataFrame, driver: pd.DataFrame) -> pd.DataFrame:
    # Use DesignBuilder dates as authoritative; align by row number if driver has no matching calendar dates.
    n = min(len(db), len(driver))
    db2 = db.iloc[:n].reset_index(drop=True)
    drv = driver.iloc[:n].reset_index(drop=True)
    # The v3.1 engine will normalize driver columns; keep DB date for merge/calibration.
    drv = drv.copy()
    drv["date"] = db2["date"]
    return pd.concat([db2, drv.add_prefix("driver__")], axis=1)


def build_driver_from_merged(merged: pd.DataFrame) -> pd.DataFrame:
    driver = pd.DataFrame()
    # Remove prefix from selected columns.
    for c in merged.columns:
        if c.startswith("driver__"):
            driver[c.replace("driver__", "")] = merged[c]
    driver["date"] = merged["date"]
    return driver


def plot_outputs(out_df: pd.DataFrame, output_dir: Path) -> None:
    plt.figure(figsize=(12, 5))
    plt.plot(out_df["date"], out_df["db_total_kwh"], label="DesignBuilder", linewidth=1.2)
    plt.plot(out_df["date"], out_df["energy_kwh_v31_physics"], label="v3.1 physics", linewidth=0.9, alpha=0.75)
    plt.plot(out_df["date"], out_df["energy_kwh_v31_final"], label="v3.1 calibrated", linewidth=1.1)
    plt.ylabel("Daily HVAC energy (kWh/day)")
    plt.xlabel("Date")
    plt.title("HVAC v3.1 daily validation")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "v31_daily_energy_before_after.png", dpi=180)
    plt.close()

    monthly = out_df.groupby("month", as_index=False).agg(
        db=("db_total_kwh", "sum"),
        physics=("energy_kwh_v31_physics", "sum"),
        final=("energy_kwh_v31_final", "sum"),
    )
    monthly["physics_bias_pct"] = (monthly["physics"] - monthly["db"]) / monthly["db"] * 100
    monthly["final_bias_pct"] = (monthly["final"] - monthly["db"]) / monthly["db"] * 100
    monthly.to_csv(output_dir / "v31_monthly_bias.csv", index=False)
    plt.figure(figsize=(10, 5))
    plt.bar(monthly["month"] - 0.18, monthly["physics_bias_pct"], width=0.36, label="Physics")
    plt.bar(monthly["month"] + 0.18, monthly["final_bias_pct"], width=0.36, label="Calibrated")
    plt.axhline(0, linewidth=1)
    plt.ylabel("Monthly bias (%)")
    plt.xlabel("Month")
    plt.title("Monthly bias before/after HVAC v3.1 calibration")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "v31_monthly_bias_before_after.png", dpi=180)
    plt.close()

    plt.figure(figsize=(6, 6))
    plt.scatter(out_df["db_total_kwh"], out_df["energy_kwh_v31_physics"], s=8, alpha=0.45, label="Physics")
    plt.scatter(out_df["db_total_kwh"], out_df["energy_kwh_v31_final"], s=8, alpha=0.45, label="Calibrated")
    low = min(out_df["db_total_kwh"].min(), out_df["energy_kwh_v31_final"].min())
    high = max(out_df["db_total_kwh"].max(), out_df["energy_kwh_v31_physics"].max())
    plt.plot([low, high], [low, high], linestyle="--", linewidth=1)
    plt.xlabel("DesignBuilder daily energy (kWh/day)")
    plt.ylabel("Model daily energy (kWh/day)")
    plt.title("Daily scatter validation")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "v31_scatter_before_after.png", dpi=180)
    plt.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--designbuilder_xlsx", required=True)
    ap.add_argument("--weather_or_solver_csv", required=True, help="CSV containing weather/site data or an old solver daily output.")
    ap.add_argument("--building_config_json", default=None)
    ap.add_argument("--output_dir", default="hvac_v31_outputs")
    ap.add_argument("--train_years", default=None, help="Comma-separated calendar years, e.g. 2020,2021,2022,2023")
    ap.add_argument("--validate_years", default=None, help="Comma-separated calendar years, e.g. 2024")
    ap.add_argument("--residual_alpha", type=float, default=100.0)
    ap.add_argument("--max_lag_days", type=int, default=7)
    args = ap.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.building_config_json:
        building, hvac = load_config(args.building_config_json)
    else:
        building, hvac = BuildingSpec(), HVACSpec()

    db = read_designbuilder_daily(args.designbuilder_xlsx)
    driver = pd.read_csv(args.weather_or_solver_csv)
    merged = align_designbuilder_and_driver(db, driver)
    engine_input = build_driver_from_merged(merged)

    # Run physics without correction.
    physics = compute_v31_baseline(engine_input, building, hvac, CalibrationConfig.empty())
    work = pd.concat([db.iloc[: len(physics)].reset_index(drop=True), physics.drop(columns=["date", "year", "month"], errors="ignore")], axis=1)
    work = work.loc[:, ~work.columns.duplicated()]
    if "date" not in work.columns:
        work["date"] = db.iloc[: len(work)]["date"].values
    work["year"] = pd.to_datetime(work["date"]).dt.year
    work["month"] = pd.to_datetime(work["date"]).dt.month

    # Detect whether a one-day/multi-day lag explains the error.
    best_lag, lag_table = detect_best_lag(work["db_total_kwh"], work["energy_kwh_v31_physics"], args.max_lag_days)
    lag_table.to_csv(output_dir / "v31_lag_scan.csv", index=False)

    train_years = parse_years_arg(args.train_years)
    validate_years = parse_years_arg(args.validate_years)
    if train_years is None:
        # Default: all but last calendar year if possible.
        years = sorted(work["year"].dropna().unique().tolist())
        train_years = years[:-1] if len(years) > 1 else years
    if validate_years is None:
        years = sorted(work["year"].dropna().unique().tolist())
        validate_years = [years[-1]] if len(years) > 1 else years

    train_mask = work["year"].isin(train_years)
    validate_mask = work["year"].isin(validate_years)
    if train_mask.sum() < 30:
        train_mask[:] = True
    train = work.loc[train_mask].copy()

    # Component scales are diagnostic only; v3.1 physics already has separate components.
    # Use safe caps to avoid implausible calibration.
    fan_scale = 1.0
    pump_scale = 1.0
    aux_scale = 0.0
    if "db_fan_kwh" in train.columns and train["fan_kwh"].sum() > 1e-9:
        fan_scale = float(np.clip(train["db_fan_kwh"].sum() / train["fan_kwh"].sum(), 0.2, 10.0))
    if "db_pump_kwh" in train.columns and train["pump_kwh"].sum() > 1e-9:
        pump_scale = float(np.clip(train["db_pump_kwh"].sum() / train["pump_kwh"].sum(), 0.02, 10.0))
    if "db_auxiliary_kwh" in train.columns and train["auxiliary_kwh"].sum() > 1e-9:
        aux_scale = float(np.clip(train["db_auxiliary_kwh"].sum() / train["auxiliary_kwh"].sum(), 0.0, 2.0))

    # Recompute physics with component scales before seasonal/residual fitting.
    component_cal = CalibrationConfig.empty()
    component_cal.fan_scale = fan_scale
    component_cal.pump_scale = pump_scale
    component_cal.auxiliary_scale = aux_scale
    physics2 = compute_v31_baseline(engine_input, building, hvac, component_cal)
    work2 = pd.concat([db.iloc[: len(physics2)].reset_index(drop=True), physics2.drop(columns=["date", "year", "month"], errors="ignore")], axis=1)
    work2 = work2.loc[:, ~work2.columns.duplicated()]
    work2["date"] = db.iloc[: len(work2)]["date"].values
    work2["year"] = pd.to_datetime(work2["date"]).dt.year
    work2["month"] = pd.to_datetime(work2["date"]).dt.month
    train2 = work2.loc[work2["year"].isin(train_years)].copy()

    monthly_factors = fit_monthly_factors(train2, "db_total_kwh", "energy_kwh_v31_physics")
    seasonal_cal = CalibrationConfig(
        monthly_factors=monthly_factors,
        residual_coefficients={},
        feature_names=tuple(),
        fan_scale=fan_scale,
        pump_scale=pump_scale,
        auxiliary_scale=aux_scale,
    )
    seasonal_df = compute_v31_baseline(engine_input, building, hvac, seasonal_cal)
    work3 = pd.concat([db.iloc[: len(seasonal_df)].reset_index(drop=True), seasonal_df.drop(columns=["date", "year", "month"], errors="ignore")], axis=1)
    work3 = work3.loc[:, ~work3.columns.duplicated()]
    work3["date"] = db.iloc[: len(work3)]["date"].values
    work3["year"] = pd.to_datetime(work3["date"]).dt.year
    work3["month"] = pd.to_datetime(work3["date"]).dt.month
    train3 = work3.loc[work3["year"].isin(train_years)].copy()
    train3["residual_target"] = train3["db_total_kwh"] - train3["energy_kwh_v31_seasonal"]
    intercept, coefs, feature_names = fit_ridge_residual(train3, "residual_target", alpha=args.residual_alpha)

    final_cal = CalibrationConfig(
        monthly_factors=monthly_factors,
        residual_coefficients=coefs,
        feature_names=tuple(feature_names),
        residual_intercept=intercept,
        fan_scale=fan_scale,
        pump_scale=pump_scale,
        auxiliary_scale=aux_scale,
    )
    final_df = compute_v31_baseline(engine_input, building, hvac, final_cal)
    out_df = pd.concat([db.iloc[: len(final_df)].reset_index(drop=True), final_df.drop(columns=["date", "year", "month"], errors="ignore")], axis=1)
    out_df = out_df.loc[:, ~out_df.columns.duplicated()]
    out_df["date"] = db.iloc[: len(out_df)]["date"].values
    out_df["year"] = pd.to_datetime(out_df["date"]).dt.year
    out_df["month"] = pd.to_datetime(out_df["date"]).dt.month
    out_df.to_csv(output_dir / "hvac_v31_daily_outputs.csv", index=False)

    metrics = metrics_dataframe({
        "v31_physics_uncalibrated": (out_df["db_total_kwh"], out_df["energy_kwh_v31_physics"]),
        "v31_component_seasonal": (out_df["db_total_kwh"], out_df["energy_kwh_v31_seasonal"]),
        "v31_final_calibrated": (out_df["db_total_kwh"], out_df["energy_kwh_v31_final"]),
    })
    metrics.to_csv(output_dir / "hvac_v31_metrics_before_after.csv", index=False)

    if validate_mask.sum() > 0:
        hold = out_df.loc[out_df["year"].isin(validate_years)].copy()
        hold_metrics = metrics_dataframe({
            "holdout_v31_physics_uncalibrated": (hold["db_total_kwh"], hold["energy_kwh_v31_physics"]),
            "holdout_v31_component_seasonal": (hold["db_total_kwh"], hold["energy_kwh_v31_seasonal"]),
            "holdout_v31_final_calibrated": (hold["db_total_kwh"], hold["energy_kwh_v31_final"]),
        })
        hold_metrics.to_csv(output_dir / "hvac_v31_metrics_holdout.csv", index=False)

    final_cal.save(output_dir / "hvac_v31_calibration_coefficients.json")
    with open(output_dir / "hvac_v31_run_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "train_years": train_years,
            "validate_years": validate_years,
            "best_lag_days_for_physics": best_lag,
            "fan_scale": fan_scale,
            "pump_scale": pump_scale,
            "auxiliary_scale": aux_scale,
            "monthly_factors": monthly_factors,
            "metrics": metrics.to_dict(orient="records"),
        }, f, indent=2)

    plot_outputs(out_df, output_dir)

    report = []
    report.append("# HVAC v3.1 DesignBuilder-Calibrated Baseline Solver Report\n")
    report.append(f"Training years: {train_years}\n")
    report.append(f"Validation years: {validate_years}\n")
    report.append(f"Best lag scan result: {best_lag} days. A small lag should not be treated as the only correction unless it strongly reduces CVRMSE.\n")
    report.append("\n## Metrics\n")
    report.append(metrics.to_markdown(index=False))
    if (output_dir / "hvac_v31_metrics_holdout.csv").exists():
        report.append("\n\n## Holdout Metrics\n")
        report.append(pd.read_csv(output_dir / "hvac_v31_metrics_holdout.csv").to_markdown(index=False))
    report.append("\n\n## Interpretation\n")
    report.append("HVAC v3.1 adds the physical layers missing from the earlier baseline: envelope UA, infiltration, solar gains, internal gains, thermal mass lag, heating/cooling deadband, PLR-COP, and separate fan/pump/auxiliary terms. The final calibration layer is intended to correct remaining DesignBuilder-specific schedules and residual daily timing effects without replacing the physical solver.\n")
    (output_dir / "hvac_v31_validation_report.md").write_text("\n".join(report), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "metrics_csv": str(output_dir / "hvac_v31_metrics_before_after.csv"),
        "coefficients_json": str(output_dir / "hvac_v31_calibration_coefficients.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
