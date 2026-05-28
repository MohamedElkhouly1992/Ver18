# HVAC v3.1 DesignBuilder Building Extractor + Operational Schedule Bundle

This deployment adds a DesignBuilder building-data extractor to HVAC v3/v3.1.
It reads the DesignBuilder building-data export you uploaded, extracts physical building inputs, then generates `operational_schedule_enhanced.csv` that can be merged into the daily weather/driver CSV before running the HVAC v3 core solver.

## Important scientific limitation

The uploaded DesignBuilder building report contains building parameters and zone data, but it does **not** contain the exact hourly DesignBuilder operational schedules. Therefore, this bundle extracts what is present and generates a transparent activity-based operational schedule proxy. For exact matching, export these schedules directly from DesignBuilder and replace the proxy columns:

- occupancy schedule
- lighting schedule
- equipment schedule
- HVAC operation schedule
- heating/cooling thermostat schedules
- holiday/vacation calendar

## Main outputs

After running, the app/scripts create:

- `building_extracted_params.json`
- `building_solver_inputs.csv`
- `operational_schedule_enhanced.csv`
- `external_summary.csv`
- `glazing_orientation.csv`
- `wall_orientation.csv`
- `zones.csv`
- `elements.csv`
- `building_extraction_report.md`

## Streamlit Cloud deployment

Set Streamlit main file to:

```text
streamlit_app.py
```

Required repository root files:

```text
streamlit_app.py
requirements.txt
db_building_extractor.py
hvac_v3_schedule_adapter.py
hvac_v31_engine.py
calibrate_hvac_v31_designbuilder.py
```

## Local run

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## CLI extraction

```bash
python run_db_building_schedule_pipeline.py \
  --building_file "I am sharing 'building data' with you.csv" \
  --output_dir db_building_outputs \
  --start_year 2020 \
  --end_year 2024
```

## Merge the generated schedule into your HVAC v3 daily driver CSV

```bash
python hvac_v3_schedule_adapter.py \
  --driver_csv baseline_daily_weather.csv \
  --schedule_csv db_building_outputs/operational_schedule_enhanced.csv \
  --output_csv outputs/hvac_v3_driver_with_db_schedule.csv
```

Then use `outputs/hvac_v3_driver_with_db_schedule.csv` as the driver input to the HVAC v3 core solver.

## Solver variables to use

Inside `hvac_v3.py`, merge or read the following columns from the schedule CSV:

- `occupancy_factor`
- `lighting_schedule_factor`
- `equipment_schedule_factor`
- `fan_schedule_factor`
- `pump_schedule_factor`
- `operating_hours`
- `heating_mode_weight`
- `cooling_mode_weight`
- `shoulder_mode_weight`
- `seasonal_energy_proxy_factor`
- `heating_setpoint_c`
- `cooling_setpoint_c`

Suggested use in core solver:

```python
Q_internal = area_m2 * (people_w_m2 * occupancy_factor + lighting_w_m2 * lighting_schedule_factor + equipment_w_m2 * equipment_schedule_factor)
E_fan = E_fan_design * fan_schedule_factor * airflow_factor**3 * (1 + gamma_filter * DI_filter)
E_pump = E_pump_design * pump_schedule_factor * PLR
```

Do not use the schedule only for comfort. It must influence internal gains, fan runtime, pump runtime, and HVAC operation.
