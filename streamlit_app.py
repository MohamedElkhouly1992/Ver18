from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from db_building_extractor import (
    extract_all,
    export_extraction_outputs,
    learn_monthly_factors_from_reference,
)
from hvac_v3_schedule_adapter import merge_schedule_to_driver

st.set_page_config(page_title="HVAC v3.1 DB Building Extractor", layout="wide")

st.title("HVAC v3.1 — DesignBuilder Building Extractor & Operational Schedule Generator")
st.caption("Extracts DesignBuilder building inputs and creates a daily operational schedule CSV for HVAC v3/v3.1 baseline calibration.")

with st.expander("What this app can and cannot extract", expanded=True):
    st.markdown(
        """
        **Can extract:** zones, floor area, volume, envelope UA, infiltration UA, setpoints, activity type, glazing SHGC/VT, orientation-based glazing and wall areas.

        **Cannot extract from this report alone:** exact hourly occupancy, lighting, equipment, HVAC, thermostat, holiday, or vacation schedules. If the file does not contain them, the app generates a transparent **activity-based schedule proxy**. For Q1-level validation, export the exact schedules from DesignBuilder when available.
        """
    )

building_upload = st.file_uploader(
    "Upload DesignBuilder building-data export (.csv or .xlsx; DB often exports Excel format with .csv extension)",
    type=["csv", "xlsx", "xls"],
)

col_a, col_b, col_c = st.columns(3)
with col_a:
    start_year = st.number_input("Start year", min_value=1990, max_value=2100, value=2020, step=1)
with col_b:
    end_year = st.number_input("End year", min_value=1990, max_value=2100, value=2024, step=1)
with col_c:
    weekend_rule = st.selectbox("Weekend rule", ["egypt_fri_sat", "sat_sun"], index=0)

st.subheader("Optional calibration inputs")
db_daily_upload = st.file_uploader("Optional: DesignBuilder daily energy output for monthly seasonal profile", type=["csv", "xlsx", "xls"], key="dbdaily")
solver_daily_upload = st.file_uploader("Optional: solver daily output for DB/solver monthly correction factors", type=["csv"], key="solverdaily")

st.subheader("Optional HVAC v3 driver enhancement")
driver_upload = st.file_uploader("Optional: daily weather/driver CSV to merge with generated operational schedule", type=["csv"], key="driver")


def save_upload(upload, suffix):
    if upload is None:
        return None
    suffix = suffix or Path(upload.name).suffix or ".tmp"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getvalue())
    tmp.flush()
    tmp.close()
    return tmp.name


def make_zip_bytes(folder: Path) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in folder.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(folder))
    bio.seek(0)
    return bio.getvalue()

if st.button("Extract building inputs and create schedule", type="primary"):
    if building_upload is None:
        st.error("Please upload the DesignBuilder building-data file first.")
        st.stop()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            building_path = save_upload(building_upload, Path(building_upload.name).suffix)
            extracted = extract_all(building_path)
            output_dir = td / "db_building_outputs"
            paths = export_extraction_outputs(extracted, output_dir, int(start_year), int(end_year))

            # If selected weekend is not default, regenerate schedule with selected weekend.
            if weekend_rule != "egypt_fri_sat":
                from db_building_extractor import create_operational_schedule
                sched = create_operational_schedule(extracted["building_params"], int(start_year), int(end_year), weekend=weekend_rule)
                sched.to_csv(output_dir / "operational_schedule_enhanced.csv", index=False)

            if db_daily_upload is not None:
                db_path = save_upload(db_daily_upload, Path(db_daily_upload.name).suffix)
                db_daily = pd.read_excel(db_path) if db_daily_upload.name.lower().endswith((".xlsx", ".xls")) else pd.read_csv(db_path)
                solver_daily = None
                if solver_daily_upload is not None:
                    solver_path = save_upload(solver_daily_upload, ".csv")
                    solver_daily = pd.read_csv(solver_path)
                monthly = learn_monthly_factors_from_reference(db_daily, solver_daily)
                monthly.to_csv(output_dir / "monthly_seasonal_factors_from_reference.csv", index=False)

            if driver_upload is not None:
                driver_path = save_upload(driver_upload, ".csv")
                try:
                    merge_schedule_to_driver(
                        driver_path,
                        output_dir / "operational_schedule_enhanced.csv",
                        output_dir / "hvac_v3_driver_with_db_schedule.csv",
                    )
                except Exception as e:
                    st.warning(f"Could not merge schedule into driver CSV: {e}")

            params = extracted["building_params"]
            st.success("Extraction completed.")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Zones", params.get("zone_count_extracted"))
            k2.metric("Area (m²)", f"{params.get('floor_area_m2', 0):,.1f}")
            k3.metric("Volume (m³)", f"{params.get('volume_m3', 0):,.1f}")
            k4.metric("Total UA incl. infiltration (W/K)", f"{params.get('ua_total_with_infiltration_w_k', 0):,.1f}")

            st.markdown("### Extracted building parameters")
            st.json(params)

            st.markdown("### Operational schedule preview")
            sched = pd.read_csv(output_dir / "operational_schedule_enhanced.csv")
            st.dataframe(sched.head(30), use_container_width=True)

            st.markdown("### Surface summary")
            st.dataframe(pd.read_csv(output_dir / "external_summary.csv"), use_container_width=True)

            st.download_button(
                "Download operational_schedule_enhanced.csv",
                data=(output_dir / "operational_schedule_enhanced.csv").read_bytes(),
                file_name="operational_schedule_enhanced.csv",
                mime="text/csv",
            )
            st.download_button(
                "Download all extraction outputs as ZIP",
                data=make_zip_bytes(output_dir),
                file_name="db_building_extraction_outputs.zip",
                mime="application/zip",
            )
    except Exception as e:
        st.error("Extraction failed.")
        st.exception(e)
