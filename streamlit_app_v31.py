from __future__ import annotations

from pathlib import Path
import tempfile
import json

import pandas as pd
import streamlit as st

from hvac_v31_engine import BuildingSpec, HVACSpec, CalibrationConfig, compute_v31_baseline, metrics_dataframe
from calibrate_hvac_v31_designbuilder import (
    read_designbuilder_daily,
    align_designbuilder_and_driver,
    build_driver_from_merged,
    fit_monthly_factors,
    fit_ridge_residual,
)
try:
    from hvac_v31_schedule_seasonal_patch import (
        load_db_schedule_seasonal_config,
        apply_designbuilder_schedule_to_driver,
    )
except Exception:
    load_db_schedule_seasonal_config = None
    apply_designbuilder_schedule_to_driver = None

st.set_page_config(page_title="HVAC v3.1 DB-Calibrated Solver", layout="wide")
st.title("HVAC v3.1 — DesignBuilder-Calibrated Baseline Solver")
st.caption("Ready-deploy version: DesignBuilder import + schedule proxy + seasonal/residual daily calibration")

ROOT = Path(__file__).resolve().parent


def _find_examples_dir() -> Path:
    """Locate the examples folder in both flat and nested Streamlit/GitHub deployments."""
    candidates = [
        ROOT / "examples",
        Path.cwd() / "examples",
        ROOT / "HVAC_v31_FINAL_DEPLOYMENT" / "examples",
        Path.cwd() / "HVAC_v31_FINAL_DEPLOYMENT" / "examples",
    ]
    for p in candidates:
        try:
            has_xlsx = any(p.glob("*.xlsx"))
            has_csv = any(p.glob("*.csv"))
        except Exception:
            has_xlsx = has_csv = False
        if p.exists() and has_xlsx and has_csv:
            return p
    # Return default location even if missing; UI will ask user to upload files instead of crashing.
    return ROOT / "examples"


EXAMPLES = _find_examples_dir()


def _deduplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.loc[:, ~df.columns.duplicated()].copy()


def _compose_work_table(db: pd.DataFrame, result: pd.DataFrame) -> pd.DataFrame:
    n = min(len(db), len(result))
    db_part = db.iloc[:n].reset_index(drop=True).copy()
    result_part = result.iloc[:n].reset_index(drop=True).copy()
    result_part = result_part.drop(columns=["date", "year", "month", "day_of_year"], errors="ignore")
    work = pd.concat([db_part, result_part], axis=1)
    work = _deduplicate_columns(work)
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date", "db_total_kwh"]).reset_index(drop=True)
    work["year"] = work["date"].dt.year.astype(int)
    work["month"] = work["date"].dt.month.astype(int)
    work["day_of_year"] = work["date"].dt.dayofyear.astype(int)
    return work


def _load_example_path(patterns: list[str]) -> Path | None:
    for pat in patterns:
        hits = sorted(EXAMPLES.glob(pat))
        if hits:
            return hits[0]
    return None


def _save_upload(uploaded, suffix: str, td: Path) -> Path:
    path = td / f"uploaded{suffix}"
    path.write_bytes(uploaded.getvalue())
    return path


def _maybe_load_building_defaults_from_json(path: Path | None) -> tuple[BuildingSpec, HVACSpec]:
    b, h = BuildingSpec(), HVACSpec()
    cfg_path = ROOT / "sample_hvac_v31_config.json"
    if path and path.exists():
        cfg_path = path
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        for k, v in data.get("building", {}).items():
            if hasattr(b, k):
                setattr(b, k, v)
        for k, v in data.get("hvac", {}).items():
            if hasattr(h, k):
                setattr(h, k, v)
    except Exception:
        pass
    return b, h


def run_calibration(db_path: Path, driver_path: Path, b: BuildingSpec, h: HVACSpec, residual_alpha: float, force_db_schedule: bool):
    db = read_designbuilder_daily(db_path)
    driver = pd.read_csv(driver_path)

    if force_db_schedule and load_db_schedule_seasonal_config and apply_designbuilder_schedule_to_driver:
        cfg_file = EXAMPLES / "designbuilder_extracted_schedule_seasonal.json"
        if cfg_file.exists():
            sched_cfg = load_db_schedule_seasonal_config(cfg_file)
            # Use DesignBuilder dates as the authoritative calendar before applying schedule.
            tmp = driver.iloc[:len(db)].reset_index(drop=True).copy()
            tmp["date"] = db.iloc[:len(tmp)]["date"].values
            tmp = apply_designbuilder_schedule_to_driver(tmp, sched_cfg, date_col="date")
            driver = tmp

    merged = align_designbuilder_and_driver(db, driver)
    engine_input = build_driver_from_merged(merged)

    physics = compute_v31_baseline(engine_input, b, h, CalibrationConfig.empty())
    work = _compose_work_table(db, physics)

    years = sorted(pd.Series(work["year"]).dropna().astype(int).unique().tolist())
    train_years = years[:-1] if len(years) > 1 else years
    validate_years = years[-1:] if len(years) > 1 else years

    train = work[work["year"].isin(train_years)].copy()
    monthly_factors = fit_monthly_factors(train, "db_total_kwh", "energy_kwh_v31_physics")

    seasonal_cal = CalibrationConfig(monthly_factors=monthly_factors, residual_coefficients={}, feature_names=tuple())
    seasonal = compute_v31_baseline(engine_input, b, h, seasonal_cal)
    work2 = _compose_work_table(db, seasonal)

    train2 = work2[work2["year"].isin(train_years)].copy()
    train2["residual_target"] = train2["db_total_kwh"] - train2["energy_kwh_v31_seasonal"]
    intercept, coefs, feature_names = fit_ridge_residual(train2, "residual_target", alpha=residual_alpha)

    final_cal = CalibrationConfig(
        monthly_factors=monthly_factors,
        residual_coefficients=coefs,
        feature_names=tuple(feature_names),
        residual_intercept=intercept,
    )
    final = compute_v31_baseline(engine_input, b, h, final_cal)
    out = _compose_work_table(db, final)

    metrics_all = metrics_dataframe({
        "v31_physics": (out["db_total_kwh"], out["energy_kwh_v31_physics"]),
        "v31_seasonal": (out["db_total_kwh"], out["energy_kwh_v31_seasonal"]),
        "v31_final": (out["db_total_kwh"], out["energy_kwh_v31_final"]),
    })
    hold = out[out["year"].isin(validate_years)].copy()
    metrics_hold = metrics_dataframe({
        "holdout_v31_physics": (hold["db_total_kwh"], hold["energy_kwh_v31_physics"]),
        "holdout_v31_seasonal": (hold["db_total_kwh"], hold["energy_kwh_v31_seasonal"]),
        "holdout_v31_final": (hold["db_total_kwh"], hold["energy_kwh_v31_final"]),
    })
    return out, metrics_all, metrics_hold, monthly_factors, final_cal, train_years, validate_years


def _examples_available() -> bool:
    return (_load_example_path(["*Design*Builder*.xlsx", "*.xlsx"]) is not None) and \
           (_load_example_path(["baseline_no_degradation_daily.csv", "baseline_daily_weather.csv", "site_data.csv", "*.csv"]) is not None)


examples_available = _examples_available()

with st.sidebar:
    st.header("Run mode")
    if examples_available:
        st.success(f"Example files found: {EXAMPLES}")
    else:
        st.warning("The examples/ folder is missing from this Streamlit deployment. Upload the two input files below, or redeploy using the fixed bundle with the examples folder included at repository root.")

    use_examples = st.checkbox("Use included example files", value=examples_available, disabled=not examples_available)
    force_db_schedule = st.checkbox("Apply DesignBuilder schedule proxy", value=True)
    residual_alpha = st.number_input("Residual ridge alpha", min_value=0.0, value=100.0, step=25.0)
    st.caption("For deployment tests, keep included examples enabled. For thesis validation, upload your latest DesignBuilder workbook and solver/weather CSV.")

    db_file = None
    driver_file = None
    if not use_examples:
        db_file = st.file_uploader("DesignBuilder daily workbook (.xlsx)", type=["xlsx"])
        driver_file = st.file_uploader("Weather or old solver daily CSV", type=["csv"])

b0, h0 = _maybe_load_building_defaults_from_json(None)

with st.expander("Building and HVAC parameters", expanded=False):
    c1, c2, c3 = st.columns(3)
    with c1:
        b0.floor_area_m2 = st.number_input("Floor area (m²)", value=float(b0.floor_area_m2))
        b0.volume_m3 = st.number_input("Volume (m³)", value=float(b0.volume_m3))
        b0.h_infiltration_w_per_k = st.number_input("Infiltration H (W/K)", value=float(b0.h_infiltration_w_per_k))
        b0.glazing_area_m2 = st.number_input("Glazing area (m²)", value=float(b0.glazing_area_m2))
    with c2:
        b0.shgc = st.number_input("SHGC", value=float(b0.shgc), min_value=0.0, max_value=1.0)
        b0.heat_setpoint_c = st.number_input("Heating setpoint (°C)", value=float(b0.heat_setpoint_c))
        b0.cool_setpoint_c = st.number_input("Cooling setpoint (°C)", value=float(b0.cool_setpoint_c))
        b0.thermal_mass_alpha = st.number_input("Thermal mass alpha", value=float(b0.thermal_mass_alpha), min_value=0.0, max_value=0.95)
    with c3:
        h0.cooling_capacity_kw = st.number_input("Cooling capacity (kW)", value=float(h0.cooling_capacity_kw))
        h0.nominal_cooling_cop = st.number_input("Nominal cooling COP", value=float(h0.nominal_cooling_cop))
        h0.operation_hours_per_day = st.number_input("Operation hours/day", value=float(h0.operation_hours_per_day))
        h0.fan_base_kw = st.number_input("Fan base kW", value=float(h0.fan_base_kw))
        h0.pump_base_kw = st.number_input("Pump base kW", value=float(h0.pump_base_kw))

st.markdown("""
This app produces the clean calibrated baseline required before running S0–S3 and the Severity × Strategy matrix. It reports three stages: pure physics, seasonal calibration, and final residual-calibrated output.
""")

can_run = (use_examples and examples_available) or (db_file is not None and driver_file is not None)
if not can_run:
    st.info("To run the app, either deploy the full bundle including the examples/ folder, or upload both required files: DesignBuilder .xlsx and solver/weather .csv.")
if st.button("Run HVAC v3.1 calibration", type="primary", disabled=not can_run):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            if use_examples:
                db_path = _load_example_path(["*Design*Builder*.xlsx", "*.xlsx"])
                driver_path = _load_example_path(["baseline_no_degradation_daily.csv", "baseline_daily_weather.csv", "site_data.csv", "*.csv"])
                if db_path is None or driver_path is None:
                    st.error("Example files were not found in the deployed repository. Please upload the DesignBuilder workbook and solver/weather CSV from the sidebar, or redeploy the fixed bundle with examples/ at repository root.")
                    st.stop()
            else:
                db_path = _save_upload(db_file, ".xlsx", td)
                driver_path = _save_upload(driver_file, ".csv", td)

            out, metrics_all, metrics_hold, factors, cal, train_years, validate_years = run_calibration(
                db_path, driver_path, b0, h0, residual_alpha, force_db_schedule
            )

            st.success("Calibration completed successfully.")
            st.info(f"Training years: {train_years}; holdout year(s): {validate_years}")

            m1, m2, m3 = st.columns(3)
            final_row = metrics_all[metrics_all["case"] == "v31_final"].iloc[0]
            m1.metric("Final Daily MAPE", f"{final_row['MAPE_pct']:.2f}%")
            m2.metric("Final Daily CVRMSE", f"{final_row['CVRMSE_pct']:.2f}%")
            m3.metric("Final NMBE", f"{final_row['NMBE_pct']:.2f}%")

            st.subheader("Full-period validation metrics")
            st.dataframe(metrics_all, use_container_width=True)
            st.subheader("Holdout-year validation metrics")
            st.dataframe(metrics_hold, use_container_width=True)

            st.subheader("Monthly calibration factors")
            st.dataframe(pd.DataFrame({"month": sorted(factors, key=lambda x: int(x)), "factor": [factors[m] for m in sorted(factors, key=lambda x: int(x))]}), use_container_width=True)

            st.subheader("Daily DesignBuilder vs HVAC v3.1")
            chart_cols = ["date", "db_total_kwh", "energy_kwh_v31_physics", "energy_kwh_v31_seasonal", "energy_kwh_v31_final"]
            st.line_chart(out[chart_cols].set_index("date"))

            coeff_payload = {
                "monthly_factors": cal.monthly_factors,
                "residual_coefficients": cal.residual_coefficients,
                "feature_names": list(cal.feature_names),
                "residual_intercept": cal.residual_intercept,
                "fan_scale": cal.fan_scale,
                "pump_scale": cal.pump_scale,
                "auxiliary_scale": cal.auxiliary_scale,
                "clip_negative_energy": cal.clip_negative_energy,
            }

            st.download_button("Download daily outputs CSV", out.to_csv(index=False).encode("utf-8"), "hvac_v31_daily_outputs.csv", "text/csv")
            st.download_button("Download metrics CSV", metrics_all.to_csv(index=False).encode("utf-8"), "hvac_v31_metrics.csv", "text/csv")
            st.download_button("Download holdout metrics CSV", metrics_hold.to_csv(index=False).encode("utf-8"), "hvac_v31_holdout_metrics.csv", "text/csv")
            st.download_button("Download calibration coefficients JSON", json.dumps(coeff_payload, indent=2).encode("utf-8"), "hvac_v31_calibration_coefficients.json", "application/json")
    except Exception as exc:
        st.error("HVAC v3.1 calibration failed.")
        st.exception(exc)

st.divider()
st.caption("Recommended thesis use: report physics-only → seasonal → final as an ablation sequence, then use energy_kwh_v31_final as the clean baseline for S0–S3.")
