"""
DesignBuilder building-data extractor and schedule generator for HVAC v3/v3.1.

Purpose
-------
1. Read a DesignBuilder building-data export. The file may be an .xlsx workbook
   even if it has a .csv extension.
2. Extract building geometry, zones, envelope UA, infiltration, glazing SHGC,
   orientation areas, setpoints, and activity type.
3. Generate an operational schedule CSV that can be merged into the HVAC v3
   daily driver file before the core solver is run.

Important scientific note
-------------------------
DesignBuilder's building input report normally does NOT include the complete
hourly operational schedules. This module therefore extracts all schedule-related
information that exists in the report (activity type, setpoints, heated/cooled
status, floor areas) and creates a transparent activity-based schedule proxy.
If a DesignBuilder daily reference output is supplied, monthly seasonal factors
can also be learned from the reference data.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

KNOWN_ELEMENTS = {
    "Infiltration",
    "Ceiling",
    "Wall",
    "Partition",
    "Ground floor",
    "Glazing",
    "Roof",
    "Floor",
}


def _isna(x) -> bool:
    try:
        return bool(pd.isna(x))
    except Exception:
        return False


def _num(x, default=np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def _clean_str(x) -> str:
    if pd.isna(x):
        return ""
    return str(x).strip()


def read_designbuilder_building_file(path: str | Path) -> pd.DataFrame:
    """Read DesignBuilder export as raw table.

    Many DesignBuilder/Excel exports are really .xlsx files even when the file
    extension is .csv. This function tries Excel first, then CSV fallbacks.
    """
    path = Path(path)
    # Try Excel first because the uploaded DB file often starts with PK ZIP bytes.
    try:
        xl = pd.ExcelFile(path, engine="openpyxl")
        sheet = xl.sheet_names[0]
        return pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl")
    except Exception:
        pass
    # CSV fallbacks.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        for sep in (None, ",", ";", "\t"):
            try:
                return pd.read_csv(path, header=None, encoding=enc, sep=sep, engine="python")
            except Exception:
                continue
    raise ValueError(f"Could not read DesignBuilder building file: {path}")


def extract_library_tables(raw: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    constructions = []
    glazings = []
    for _, row in raw.iterrows():
        key = _clean_str(row.get(0))
        if key.startswith("CONSTRUCTION"):
            constructions.append({
                "key": _clean_str(row.get(1)),
                "u_value_w_m2k": _num(row.get(2)),
                "km_kj_k": _num(row.get(3)),
                "cost_egp_m2": _num(row.get(4)),
            })
        elif key.startswith("GLAZING"):
            glazings.append({
                "name": _clean_str(row.get(1)),
                "layers": _num(row.get(2)),
                "u_value_w_m2k": _num(row.get(3)),
                "visible_transmittance": _num(row.get(4)),
                "shgc": _num(row.get(5)),
                "direct_solar_transmittance": _num(row.get(6)),
                "cost_egp_m2": _num(row.get(7)),
            })
    return pd.DataFrame(constructions), pd.DataFrame(glazings)


def extract_building_globals(raw: pd.DataFrame) -> Dict[str, float | int | str]:
    out: Dict[str, float | int | str] = {}
    mapping = {
        "Building number of zones:": "number_of_zones",
        "Building heated/cooled floor area (m2)": "floor_area_m2",
        "Building volume (m3)": "volume_m3",
        "Building external area (m2)": "external_area_m2",
        "Building area-weighted average U-value (W/m2K)": "area_weighted_u_value_w_m2k",
        "Building external surface area/Volume (m-1)": "external_surface_area_per_volume_m_inv",
    }
    for _, row in raw.iterrows():
        key = _clean_str(row.get(0))
        if key in mapping:
            value = row.get(1)
            n = _num(value)
            out[mapping[key]] = int(n) if mapping[key] == "number_of_zones" and not math.isnan(n) else n
    return out


def extract_activity_summary(raw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    start = None
    for i, row in raw.iterrows():
        if _clean_str(row.get(0)) == "Activity Area Summary":
            start = i + 2
            break
    if start is None:
        return pd.DataFrame(columns=["activity", "area_m2"])
    for i in range(start, len(raw)):
        activity = _clean_str(raw.iat[i, 0])
        if activity == "" or activity == "Zone":
            break
        area = _num(raw.iat[i, 1])
        rows.append({"activity": activity, "area_m2": area})
        if activity.lower() == "total":
            break
    return pd.DataFrame(rows)


def extract_zones_and_elements(raw: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    zone_headers = raw.index[raw[0].astype(str).str.strip().eq("Zone")].tolist()
    zones: List[Dict] = []
    elements: List[Dict] = []
    for idx in zone_headers:
        if idx + 1 >= len(raw):
            continue
        zr = raw.loc[idx + 1]
        zone_name = _clean_str(zr.get(0))
        if zone_name == "" or zone_name == "Activity":
            continue
        zone = {
            "zone_name": zone_name,
            "activity": _clean_str(zr.get(1)),
            "floor_area_m2": _num(zr.get(2)),
            "volume_m3": _num(zr.get(3)),
            "heated": bool(zr.get(4)) if not pd.isna(zr.get(4)) else False,
            "heating_setpoint_c": _num(zr.get(5)),
            "cooled": bool(zr.get(6)) if not pd.isna(zr.get(6)) else False,
            "cooling_setpoint_c": _num(zr.get(7)),
        }
        zones.append(zone)

        next_zone = next((z for z in zone_headers if z > idx), len(raw))
        elem_header_rows = [r for r in range(idx + 2, next_zone) if _clean_str(raw.iat[r, 1]) == "Element"]
        if not elem_header_rows:
            continue
        eh = elem_header_rows[0]
        headers = {c: _clean_str(raw.iat[eh, c]) for c in range(1, raw.shape[1]) if not pd.isna(raw.iat[eh, c])}
        for r in range(eh + 1, next_zone):
            element_name = _clean_str(raw.iat[r, 1])
            if element_name not in KNOWN_ELEMENTS:
                continue
            rec = {"zone_name": zone_name}
            for c, h in headers.items():
                rec[h] = raw.iat[r, c]
            elements.append(rec)

    zones_df = pd.DataFrame(zones)
    elements_df = pd.DataFrame(elements)
    for col in [
        "Area-Nett (m2)",
        "U-Value (W/K-m2)",
        "U-Value*Area (W/K)",
        "Km (KJ/m2-K)",
        "Km*Area (KJ/K)",
        "Orientation (deg E of N)",
        "Slope (deg)",
        "Cost of Surface Finish (EGP)",
    ]:
        if col in elements_df.columns:
            elements_df[col] = pd.to_numeric(elements_df[col], errors="coerce")
    return zones_df, elements_df


def orientation_cardinal(angle) -> str:
    if pd.isna(angle):
        return "Unknown"
    a = float(angle) % 360.0
    if a < 45 or a >= 315:
        return "North"
    if a < 135:
        return "East"
    if a < 225:
        return "South"
    return "West"


def summarize_elements(elements: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if elements.empty:
        return {"surface_summary": pd.DataFrame(), "external_summary": pd.DataFrame(), "glazing_orientation": pd.DataFrame(), "wall_orientation": pd.DataFrame()}

    e = elements.copy()
    e["adjacent_clean"] = e["Adjacent condition"].map(_clean_str)
    surface_summary = e.groupby("Element", dropna=False).agg(
        count=("Element", "size"),
        area_m2=("Area-Nett (m2)", "sum"),
        ua_w_k=("U-Value*Area (W/K)", "sum"),
        km_area_kj_k=("Km*Area (KJ/K)", "sum"),
    ).reset_index()

    external = e[e["adjacent_clean"].isin(["Outside", "Ground"])]
    external_summary = external.groupby("Element", dropna=False).agg(
        count=("Element", "size"),
        area_m2=("Area-Nett (m2)", "sum"),
        ua_w_k=("U-Value*Area (W/K)", "sum"),
        km_area_kj_k=("Km*Area (KJ/K)", "sum"),
    ).reset_index()

    glazing = e[(e["Element"] == "Glazing") & (e["adjacent_clean"] == "Outside")].copy()
    glazing["orientation"] = glazing["Orientation (deg E of N)"].apply(orientation_cardinal)
    glazing_orientation = glazing.groupby("orientation").agg(
        glazing_area_m2=("Area-Nett (m2)", "sum"),
        glazing_ua_w_k=("U-Value*Area (W/K)", "sum"),
    ).reset_index()

    walls = e[(e["Element"] == "Wall") & (e["adjacent_clean"] == "Outside")].copy()
    walls["orientation"] = walls["Orientation (deg E of N)"].apply(orientation_cardinal)
    wall_orientation = walls.groupby("orientation").agg(
        wall_area_m2=("Area-Nett (m2)", "sum"),
        wall_ua_w_k=("U-Value*Area (W/K)", "sum"),
    ).reset_index()

    return {
        "surface_summary": surface_summary,
        "external_summary": external_summary,
        "glazing_orientation": glazing_orientation,
        "wall_orientation": wall_orientation,
    }


def extract_all(path: str | Path) -> Dict:
    raw = read_designbuilder_building_file(path)
    globals_ = extract_building_globals(raw)
    activity = extract_activity_summary(raw)
    constructions, glazings = extract_library_tables(raw)
    zones, elements = extract_zones_and_elements(raw)
    summaries = summarize_elements(elements)

    ext = summaries["external_summary"]
    def sum_for(element: str, col: str = "ua_w_k") -> float:
        if ext.empty:
            return 0.0
        return float(ext.loc[ext["Element"] == element, col].sum())
    # Infiltration is stored as U-Value*Area (W/K) in the DB report.
    infiltration_ua_w_k = sum_for("Infiltration")
    wall_ua = sum_for("Wall")
    glazing_ua = sum_for("Glazing")
    roof_ua = sum_for("Roof")
    ground_ua = sum_for("Ground floor")
    floor_out_ua = sum_for("Floor")

    # Representative glazing properties from library and element data.
    if not glazings.empty:
        primary_glazing = glazings.sort_values("u_value_w_m2k").iloc[0].to_dict()
    else:
        primary_glazing = {}

    dominant_activity = "Unknown"
    if not activity.empty:
        candidates = activity[activity["activity"].str.lower() != "total"].copy()
        if not candidates.empty:
            dominant_activity = candidates.sort_values("area_m2", ascending=False).iloc[0]["activity"]

    building_params = {
        **globals_,
        "activity_type": dominant_activity,
        "zone_count_extracted": int(len(zones)),
        "heated_zone_fraction": float(zones["heated"].mean()) if not zones.empty else np.nan,
        "cooled_zone_fraction": float(zones["cooled"].mean()) if not zones.empty else np.nan,
        "heating_setpoint_c_median": float(zones["heating_setpoint_c"].median()) if not zones.empty else np.nan,
        "cooling_setpoint_c_median": float(zones["cooling_setpoint_c"].median()) if not zones.empty else np.nan,
        "ua_wall_w_k": wall_ua,
        "ua_glazing_w_k": glazing_ua,
        "ua_roof_w_k": roof_ua,
        "ua_ground_floor_w_k": ground_ua,
        "ua_external_floor_w_k": floor_out_ua,
        "ua_infiltration_w_k": infiltration_ua_w_k,
        "ua_envelope_no_infiltration_w_k": wall_ua + glazing_ua + roof_ua + ground_ua + floor_out_ua,
        "ua_total_with_infiltration_w_k": wall_ua + glazing_ua + roof_ua + ground_ua + floor_out_ua + infiltration_ua_w_k,
        "thermal_mass_km_area_kj_k_total": float(elements["Km*Area (KJ/K)"].sum()) if "Km*Area (KJ/K)" in elements else np.nan,
        "primary_glazing": primary_glazing,
        "exact_hourly_schedule_found": False,
        "schedule_note": "The DesignBuilder building report does not contain the full hourly operational schedules. A transparent activity-based proxy schedule is generated.",
    }
    return {
        "building_params": building_params,
        "activity_summary": activity,
        "constructions": constructions,
        "glazings": glazings,
        "zones": zones,
        "elements": elements,
        **summaries,
    }


def _date_range_no_feb29(start_year: int, end_year: int) -> pd.DatetimeIndex:
    dates = pd.date_range(f"{start_year}-01-01", f"{end_year}-12-31", freq="D")
    return dates[~((dates.month == 2) & (dates.day == 29))]


def default_month_profile(activity_type: str = "Generic Office Area") -> Dict[int, float]:
    # Activity/schedule proxy, not calibrated energy profile.
    # Values are intentionally moderate; weather is handled by the solver.
    # University/office buildings often have reduced operation in winter/summer breaks.
    profile = {
        1: 0.78,
        2: 0.88,
        3: 1.00,
        4: 1.00,
        5: 1.00,
        6: 0.92,
        7: 0.72,
        8: 0.72,
        9: 0.92,
        10: 1.00,
        11: 1.00,
        12: 0.86,
    }
    return profile


def default_seasonal_energy_profile_egypt() -> Dict[int, float]:
    # Smooth seasonal energy profile multiplier to help v3 daily pattern if no
    # measured/reference DesignBuilder monthly factors are supplied. This is a
    # proxy, not a substitute for calibration.
    return {
        1: 0.98,
        2: 1.05,
        3: 0.94,
        4: 0.88,
        5: 0.98,
        6: 1.08,
        7: 1.17,
        8: 1.22,
        9: 1.10,
        10: 0.94,
        11: 0.88,
        12: 0.96,
    }


def create_operational_schedule(
    building_params: Dict,
    start_year: int = 2020,
    end_year: int = 2024,
    weekend: str = "egypt_fri_sat",
    include_no_feb29: bool = True,
) -> pd.DataFrame:
    dates = _date_range_no_feb29(start_year, end_year) if include_no_feb29 else pd.date_range(f"{start_year}-01-01", f"{end_year}-12-31", freq="D")
    area = float(building_params.get("floor_area_m2", 0.0) or 0.0)
    volume = float(building_params.get("volume_m3", 0.0) or 0.0)
    activity = str(building_params.get("activity_type", "Generic Office Area"))
    heat_sp = float(building_params.get("heating_setpoint_c_median", 22.0) or 22.0)
    cool_sp = float(building_params.get("cooling_setpoint_c_median", 24.0) or 24.0)
    month_profile = default_month_profile(activity)
    energy_profile = default_seasonal_energy_profile_egypt()

    rows = []
    # Python Monday=0 ... Sunday=6. Egypt traditional weekend Friday/Saturday.
    weekend_days = {4, 5} if weekend == "egypt_fri_sat" else {5, 6}
    for d in dates:
        dow = int(d.dayofweek)
        is_weekend = dow in weekend_days
        month_factor = month_profile[int(d.month)]
        if is_weekend:
            day_type = "weekend"
            base_occ = 0.22
            op_hours = 4.0
        else:
            day_type = "weekday"
            base_occ = 1.00
            op_hours = 12.0
        occ_factor = base_occ * month_factor
        light_factor = min(1.0, 0.15 + 0.85 * occ_factor)
        equip_factor = min(1.0, 0.25 + 0.75 * occ_factor)
        fan_factor = min(1.0, (op_hours / 12.0) * (0.35 + 0.65 * occ_factor))
        pump_factor = min(1.0, (op_hours / 12.0) * (0.30 + 0.70 * occ_factor))

        # Mode weights guide the reduced-order solver, but should not force only one mode.
        m = int(d.month)
        heating_weight = {12: 1.0, 1: 1.0, 2: 0.95, 3: 0.45, 11: 0.45}.get(m, 0.0)
        cooling_weight = {4: 0.35, 5: 0.70, 6: 1.0, 7: 1.0, 8: 1.0, 9: 0.85, 10: 0.40, 3: 0.15}.get(m, 0.0)
        shoulder_weight = max(0.0, 1.0 - max(heating_weight, cooling_weight))
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "year": int(d.year),
            "month": int(d.month),
            "day_of_year": int(d.dayofyear),
            "weekday": int(dow),
            "day_name": d.day_name(),
            "day_type": day_type,
            "activity_type": activity,
            "floor_area_m2": area,
            "volume_m3": volume,
            "operating_hours": op_hours,
            "occupancy_factor": round(float(occ_factor), 4),
            "lighting_schedule_factor": round(float(light_factor), 4),
            "equipment_schedule_factor": round(float(equip_factor), 4),
            "fan_schedule_factor": round(float(fan_factor), 4),
            "pump_schedule_factor": round(float(pump_factor), 4),
            "heating_setpoint_c": heat_sp,
            "cooling_setpoint_c": cool_sp,
            "heating_mode_weight": heating_weight,
            "cooling_mode_weight": cooling_weight,
            "shoulder_mode_weight": shoulder_weight,
            "seasonal_energy_proxy_factor": energy_profile[m],
            "source": "DesignBuilder building report + transparent activity-based proxy; not exact hourly DB schedule",
        })
    return pd.DataFrame(rows)


def learn_monthly_factors_from_reference(
    designbuilder_daily: pd.DataFrame,
    solver_daily: Optional[pd.DataFrame] = None,
    db_energy_col: Optional[str] = None,
    solver_energy_col: Optional[str] = None,
) -> pd.DataFrame:
    """Learn monthly DesignBuilder profile or correction factors from daily output.

    If only DesignBuilder daily data is supplied, returns normalized monthly target
    profile. If solver daily data is also supplied, returns direct DB/solver monthly
    correction factors.
    """
    db = designbuilder_daily.copy()
    # Detect date column. DesignBuilder often uses Date/Time.
    date_candidates = [c for c in db.columns if str(c).lower() in ["date", "date/time", "datetime", "time"]]
    if date_candidates:
        dc = date_candidates[0]
        db[dc] = pd.to_datetime(db[dc], errors="coerce")
        db = db[db[dc].notna()].copy()
        db["month"] = db[dc].dt.month
    if "month" not in db.columns:
        raise ValueError("DesignBuilder daily data must have either a date/date-time column or a month column.")
    if db_energy_col is None:
        # Prefer whole-building/HVAC total columns, not component columns like Auxiliary Energy.
        priority = ["db_total_kwh", "Design Builder", "designbuilder_total", "Total", "total", "HVAC Total", "Total Energy"]
        db_energy_col = next((c for c in priority if c in db.columns), None)
        if db_energy_col is None:
            candidates = [c for c in db.columns if any(k in str(c).lower() for k in ["total", "db_total", "design", "hvac"])]
            if not candidates:
                raise ValueError("Could not detect DesignBuilder total energy column. Provide db_energy_col.")
            db_energy_col = candidates[0]
    monthly_db = db.groupby("month")[db_energy_col].sum()
    mean_month = monthly_db.mean()
    target_profile = monthly_db / mean_month if mean_month else monthly_db * np.nan
    out = pd.DataFrame({"month": monthly_db.index, "designbuilder_monthly_energy": monthly_db.values, "designbuilder_target_profile": target_profile.values})

    if solver_daily is not None:
        sol = solver_daily.copy()
        sol_date_candidates = [c for c in sol.columns if str(c).lower() in ["date", "date/time", "datetime", "time"]]
        if sol_date_candidates:
            sc = sol_date_candidates[0]
            sol[sc] = pd.to_datetime(sol[sc], errors="coerce")
            sol = sol[sol[sc].notna()].copy()
            sol["month"] = sol[sc].dt.month
        if solver_energy_col is None:
            candidates = [c for c in sol.columns if any(k in c.lower() for k in ["energy", "kwh", "model", "solver", "physics"])]
            # Prefer known v3/v31 columns.
            priority = ["energy_kwh_v31_physics", "energy_kwh", "model_total_kwh", "total_hvac_kwh"]
            solver_energy_col = next((c for c in priority if c in sol.columns), candidates[0] if candidates else None)
        if solver_energy_col is None:
            raise ValueError("Could not detect solver energy column. Provide solver_energy_col.")
        monthly_solver = sol.groupby("month")[solver_energy_col].sum()
        corr = monthly_db / monthly_solver.replace(0, np.nan)
        out = out.merge(corr.rename("monthly_correction_factor"), on="month", how="left")
    return out


def export_extraction_outputs(extracted: Dict, output_dir: str | Path, start_year: int = 2020, end_year: int = 2024) -> Dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}

    params = extracted["building_params"]
    # Convert any numpy values to native JSON-compatible values.
    def clean_json(obj):
        if isinstance(obj, dict):
            return {k: clean_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean_json(v) for v in obj]
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, float) and math.isnan(obj):
            return None
        return obj

    params_path = output_dir / "building_extracted_params.json"
    params_path.write_text(json.dumps(clean_json(params), indent=2), encoding="utf-8")
    paths["building_extracted_params.json"] = str(params_path)

    for key in ["activity_summary", "constructions", "glazings", "zones", "elements", "surface_summary", "external_summary", "glazing_orientation", "wall_orientation"]:
        df = extracted.get(key)
        if isinstance(df, pd.DataFrame):
            p = output_dir / f"{key}.csv"
            df.to_csv(p, index=False)
            paths[p.name] = str(p)

    schedule = create_operational_schedule(params, start_year=start_year, end_year=end_year)
    sched_path = output_dir / "operational_schedule_enhanced.csv"
    schedule.to_csv(sched_path, index=False)
    paths["operational_schedule_enhanced.csv"] = str(sched_path)

    # Compact solver input with one row of building constants.
    solver_input = pd.DataFrame([{k: v for k, v in params.items() if not isinstance(v, dict)}])
    solver_input_path = output_dir / "building_solver_inputs.csv"
    solver_input.to_csv(solver_input_path, index=False)
    paths["building_solver_inputs.csv"] = str(solver_input_path)

    report = make_report_text(params, extracted, schedule)
    report_path = output_dir / "building_extraction_report.md"
    report_path.write_text(report, encoding="utf-8")
    paths["building_extraction_report.md"] = str(report_path)
    return paths


def make_report_text(params: Dict, extracted: Dict, schedule: pd.DataFrame) -> str:
    ext = extracted.get("external_summary", pd.DataFrame())
    glz = extracted.get("glazing_orientation", pd.DataFrame())
    wall = extracted.get("wall_orientation", pd.DataFrame())
    lines = []
    lines.append("# DesignBuilder Building Extraction Report")
    lines.append("")
    lines.append("## Extracted building constants")
    for k in [
        "number_of_zones", "zone_count_extracted", "activity_type", "floor_area_m2", "volume_m3",
        "external_area_m2", "area_weighted_u_value_w_m2k", "heating_setpoint_c_median", "cooling_setpoint_c_median",
        "ua_wall_w_k", "ua_glazing_w_k", "ua_roof_w_k", "ua_ground_floor_w_k", "ua_external_floor_w_k",
        "ua_infiltration_w_k", "ua_envelope_no_infiltration_w_k", "ua_total_with_infiltration_w_k",
    ]:
        if k in params:
            lines.append(f"- **{k}**: {params[k]}")
    lines.append("")
    lines.append("## Scientific limitation")
    lines.append("The uploaded DesignBuilder building report does not include the exact hourly occupancy, lighting, equipment, HVAC, holiday, or thermostat schedules. The generated `operational_schedule_enhanced.csv` is therefore a transparent activity-based proxy derived from the activity type, setpoints, and building area. For exact matching, export the actual schedules from DesignBuilder and replace the proxy factors.")
    lines.append("")
    lines.append("## How to use in HVAC v3/v3.1")
    lines.append("Merge `operational_schedule_enhanced.csv` into the daily weather/driver file by `date`, then use `occupancy_factor`, `lighting_schedule_factor`, `equipment_schedule_factor`, `fan_schedule_factor`, `pump_schedule_factor`, and mode weights inside the core solver.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract DesignBuilder building inputs and generate HVAC v3 operational schedule.")
    parser.add_argument("--building_file", required=True, help="DesignBuilder building-data export, .xlsx or .csv-like xlsx")
    parser.add_argument("--output_dir", default="db_building_outputs", help="Output directory")
    parser.add_argument("--start_year", type=int, default=2020)
    parser.add_argument("--end_year", type=int, default=2024)
    args = parser.parse_args()
    extracted = extract_all(args.building_file)
    paths = export_extraction_outputs(extracted, args.output_dir, args.start_year, args.end_year)
    print(json.dumps(paths, indent=2))
