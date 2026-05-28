"""
Extract DesignBuilder building-input information into a compact schedule/seasonal
configuration for the HVAC v3.1 reduced-order solver.

Important limitation:
DesignBuilder CSV reports exported from the Model Data/Report view usually do not
contain the full hourly schedules. This extractor therefore reads what exists
(activity type, areas, setpoints, envelope, infiltration, glazing orientation)
and builds a defensible proxy schedule. If a daily DesignBuilder output file is
also provided, it extracts the monthly seasonal target profile from the reference
energy data.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _num(x):
    try:
        s = str(x).strip().replace(",", "")
        if not s or s.upper() in {"N/A", "NA", "NAN"}:
            return None
        return float(s)
    except Exception:
        return None


def _cardinal(deg: Optional[float]) -> Optional[str]:
    if deg is None or (isinstance(deg, float) and math.isnan(deg)):
        return None
    d = deg % 360.0
    # Use quadrants. North includes 315-45, East 45-135, South 135-225, West 225-315.
    if d >= 315 or d < 45:
        return "N"
    if d < 135:
        return "E"
    if d < 225:
        return "S"
    return "W"


def extract_building_csv(path: str | Path) -> Dict[str, Any]:
    df = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)

    def find_value(label: str):
        mask = df[0].astype(str).str.strip().str.lower().eq(label.lower())
        if mask.any():
            return _num(df.loc[mask.idxmax(), 1])
        return None

    n_zones = find_value("Building number of zones:")
    floor_area = find_value("Building heated/cooled floor area (m2)")
    volume = find_value("Building volume (m3)")
    external_area = find_value("Building external area (m2)")
    avg_u = find_value("Building area-weighted average U-value (W/m2K)")

    # Zone rows: zone, activity, area, volume, heated, heat setpoint, cooled, cool setpoint
    zone_rows = []
    for i, row in df.iterrows():
        activity = str(row[1]).strip()
        area = _num(row[2]); vol = _num(row[3])
        heated = str(row[4]).strip().upper()
        if activity and area is not None and vol is not None and heated in {"TRUE", "FALSE"}:
            zone_rows.append({
                "zone": str(row[0]).strip(),
                "activity": activity,
                "area_m2": area,
                "volume_m3": vol,
                "heated": heated == "TRUE",
                "heat_setpoint_c": _num(row[5]),
                "cooled": str(row[6]).strip().upper() == "TRUE",
                "cool_setpoint_c": _num(row[7]),
            })
    zdf = pd.DataFrame(zone_rows)

    if not zdf.empty:
        activity_area = zdf.groupby("activity")["area_m2"].sum().sort_values(ascending=False).to_dict()
        heat_sp = float(zdf["heat_setpoint_c"].dropna().median()) if zdf["heat_setpoint_c"].notna().any() else None
        cool_sp = float(zdf["cool_setpoint_c"].dropna().median()) if zdf["cool_setpoint_c"].notna().any() else None
        heated_fraction = float(zdf["heated"].mean())
        cooled_fraction = float(zdf["cooled"].mean())
    else:
        activity_area = {}
        heat_sp = cool_sp = None
        heated_fraction = cooled_fraction = None

    # Surface rows: blank col0, element in col1, adjacent condition in col2, area in col3, UA in col6, orientation in col11.
    surfaces = []
    for _, row in df.iterrows():
        element = str(row[1]).strip()
        adjacent = str(row[2]).strip()
        ua = _num(row[6])
        if not element or not adjacent or adjacent == "Adjacent condition" or ua is None:
            continue
        area = _num(row[3])
        orient = _num(row[11])
        surfaces.append({
            "element": element,
            "adjacent": adjacent,
            "area_m2": area or 0.0,
            "ua_w_per_k": ua,
            "orientation_deg": orient,
            "orientation_bin": _cardinal(orient),
        })
    sdf = pd.DataFrame(surfaces)
    ext = sdf[sdf["adjacent"].str.lower().eq("outside")].copy() if not sdf.empty else pd.DataFrame()

    ext_by_element = {}
    if not ext.empty:
        g = ext.groupby("element").agg(area_m2=("area_m2", "sum"), ua_w_per_k=("ua_w_per_k", "sum"), count=("element", "size"))
        ext_by_element = {k: {kk: float(vv) if kk != "count" else int(vv) for kk, vv in row.items()} for k, row in g.to_dict("index").items()}

    glazing_by_orient = {"N": 0.0, "E": 0.0, "S": 0.0, "W": 0.0}
    if not ext.empty:
        glz = ext[ext["element"].eq("Glazing")]
        if not glz.empty:
            for k, v in glz.groupby("orientation_bin")["area_m2"].sum().to_dict().items():
                if k in glazing_by_orient:
                    glazing_by_orient[k] = float(v)

    total_glazing = sum(glazing_by_orient.values()) or ext_by_element.get("Glazing", {}).get("area_m2", 0.0)
    glazing_fraction = {k: (v / total_glazing if total_glazing else 0.0) for k, v in glazing_by_orient.items()}

    # Construction/glazing properties from header tables.
    glazing_shgc = None; glazing_u = None; glazing_vt = None
    for _, row in df.iterrows():
        if str(row[1]).strip() == "SageGlass Climatop Blue No Tint":
            glazing_u = _num(row[3])
            glazing_vt = _num(row[4])
            glazing_shgc = _num(row[5])
            break

    # Since exact hourly schedules are not in this DB CSV, create a transparent proxy based on activity type.
    primary_activity = max(activity_area, key=activity_area.get) if activity_area else "Unknown"
    schedule_proxy = {
        "source": "proxy_from_activity_type_not_exact_hourly_schedule",
        "activity_type": primary_activity,
        "note": "The building input CSV contains activity labels and setpoints, not the full DesignBuilder hourly schedules. Replace these defaults with exported DB schedules if available.",
        "weekday_occupied_factor": 1.00,
        "weekend_factor": 0.22,
        "winter_break_factor": 0.75,
        "summer_reduced_factor": 0.65,
        "winter_break_doy_ranges": [[1, 20], [350, 365]],
        "summer_reduced_doy_range": [170, 250],
        "operation_hours_per_day": 12.0,
    }

    return {
        "building": {
            "n_zones": int(n_zones) if n_zones is not None else None,
            "floor_area_m2": floor_area,
            "volume_m3": volume,
            "external_area_m2": external_area,
            "area_weighted_u_value_w_m2k": avg_u,
            "heated_zone_fraction": heated_fraction,
            "cooled_zone_fraction": cooled_fraction,
            "heat_setpoint_c": heat_sp,
            "cool_setpoint_c": cool_sp,
            "activity_area_m2": activity_area,
        },
        "envelope": {
            "external_by_element": ext_by_element,
            "ua_walls_w_per_k": ext_by_element.get("Wall", {}).get("ua_w_per_k"),
            "ua_glazing_w_per_k": ext_by_element.get("Glazing", {}).get("ua_w_per_k"),
            "ua_roof_w_per_k": ext_by_element.get("Roof", {}).get("ua_w_per_k"),
            "ua_external_floor_w_per_k": ext_by_element.get("Floor", {}).get("ua_w_per_k"),
            "h_infiltration_w_per_k": ext_by_element.get("Infiltration", {}).get("ua_w_per_k"),
            "ua_total_external_plus_infiltration_w_per_k": sum(v.get("ua_w_per_k", 0.0) for v in ext_by_element.values()),
        },
        "glazing": {
            "type": "SageGlass Climatop Blue No Tint",
            "u_value_w_m2k": glazing_u,
            "shgc": glazing_shgc,
            "visible_transmittance": glazing_vt,
            "area_by_orientation_m2": glazing_by_orient,
            "orientation_fraction": glazing_fraction,
        },
        "schedule_proxy": schedule_proxy,
    }


def add_seasonal_from_daily_outputs(config: Dict[str, Any], daily_csv: Optional[str | Path]) -> Dict[str, Any]:
    if not daily_csv or not Path(daily_csv).exists():
        config["seasonal"] = {
            "source": "not_extracted_no_daily_reference_output_provided",
            "note": "Building input CSV alone does not include an annual seasonal energy profile. Provide DesignBuilder daily outputs to compute monthly factors.",
        }
        return config

    df = pd.read_csv(daily_csv)
    # Find date/month and DB energy column.
    if "month" not in df.columns:
        date_col = next((c for c in df.columns if c.lower() in {"date", "datetime", "timestamp"}), None)
        if date_col:
            df["month"] = pd.to_datetime(df[date_col], errors="coerce").dt.month
        else:
            df["month"] = ((np.arange(len(df)) % 365) // 30 + 1).clip(1, 12)

    db_col = next((c for c in ["db_total_kwh", "designbuilder_total_kwh", "Total Design Builder energy", "total_designbuilder_kwh"] if c in df.columns), None)
    if db_col is None:
        config["seasonal"] = {"source": "daily_reference_output_provided_but_no_db_energy_column_found"}
        return config

    g = df.groupby("month")[db_col].agg(["sum", "mean", "count"])
    annual_mean = df[db_col].mean()
    target = (g["mean"] / annual_mean).to_dict()
    monthly_mwh = (g["sum"] / 1000.0).to_dict()

    # If physics column exists, compute direct correction factors.
    physics_col = next((c for c in ["energy_kwh_v31_physics", "model_energy_kwh", "energy_kwh", "model_total_kwh"] if c in df.columns), None)
    correction = {}
    if physics_col:
        correction = (df.groupby("month")[db_col].sum() / df.groupby("month")[physics_col].sum()).replace([np.inf, -np.inf], np.nan).fillna(1.0).clip(0.25, 3.0).to_dict()

    config["seasonal"] = {
        "source": "computed_from_designbuilder_daily_outputs",
        "monthly_designbuilder_mwh": {str(int(k)): float(v) for k, v in monthly_mwh.items()},
        "monthly_target_profile_factor": {str(int(k)): float(v) for k, v in target.items()},
        "monthly_correction_factor_for_current_physics": {str(int(k)): float(v) for k, v in correction.items()},
        "note": "Target profile is reference monthly mean daily energy divided by annual mean daily energy. Correction factor maps current physics monthly total to DesignBuilder monthly total and should be re-fitted if the physics solver changes.",
    }
    return config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--building_csv", required=True)
    ap.add_argument("--daily_outputs_csv", default=None, help="Optional daily outputs containing db_total_kwh and optionally energy_kwh_v31_physics")
    ap.add_argument("--output_json", default="designbuilder_extracted_schedule_seasonal.json")
    args = ap.parse_args()

    config = extract_building_csv(args.building_csv)
    config = add_seasonal_from_daily_outputs(config, args.daily_outputs_csv)
    Path(args.output_json).write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote {args.output_json}")


if __name__ == "__main__":
    main()
