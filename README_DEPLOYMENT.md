# HVAC v3.1 — Final Ready-Deployed Software Bundle

This is the final deployable HVAC v3.1 baseline-calibration bundle. It combines:

1. DesignBuilder-calibrated reduced-order HVAC v3.1 solver
2. DesignBuilder-informed schedule proxy
3. Seasonal monthly calibration
4. Residual daily calibration layer
5. Streamlit Cloud-ready app
6. CLI runner and validation reports

## Main file for Streamlit Cloud

Use this as the application entry file:

```text
streamlit_app.py
```

## Fast local run

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

On Windows:

```text
RUN_STREAMLIT_LOCAL.bat
```

## CLI validation run

```bash
python run_hvac_v31_auto.py
```

This automatically uses the included files in `examples/`:

- `ALL DATA - Design builder Data.xlsx`
- `baseline_no_degradation_daily.csv`
- `building data.csv`
- `designbuilder_extracted_schedule_seasonal.json`

## Output files

The app and CLI produce:

| File | Description |
|---|---|
| `hvac_v31_daily_outputs.csv` | Daily DesignBuilder vs v3.1 physics/seasonal/final outputs |
| `hvac_v31_metrics.csv` | Full-period metrics from the Streamlit app |
| `hvac_v31_metrics_before_after.csv` | CLI metrics for physics, seasonal, final stages |
| `hvac_v31_metrics_holdout.csv` | Holdout-year metrics |
| `hvac_v31_calibration_coefficients.json` | Reusable coefficients for your solver |
| `v31_lag_scan.csv` | Daily shift/lag diagnostic |
| `v31_monthly_bias.csv` | Monthly bias before/after calibration |
| `v31_daily_energy_before_after.png` | Time-series validation plot |
| `v31_scatter_before_after.png` | Scatter validation plot |
| `v31_monthly_bias_before_after.png` | Monthly bias plot |

## Scientific interpretation

Use the final column:

```text
energy_kwh_v31_final
```

as the validated clean baseline before rebuilding:

1. S0 reactive degradation
2. S1 preventive scheduled maintenance
3. S2 condition-based maintenance
4. S3 predictive full APO
5. Severity × Strategy matrix

Do not claim the physics-only solver is fully validated. Report the stages as an ablation sequence:

```text
v3.1 physics only → v3.1 seasonal correction → v3.1 final calibrated
```

This is the correct defensible framing for PhD thesis and Q1 journal discussion.

## Streamlit Cloud deployment steps

1. Upload all files in this folder to a GitHub repository.
2. In Streamlit Cloud, choose `streamlit_app.py` as the app file.
3. Make sure `requirements.txt` is in the repository root.
4. Deploy.
5. Keep “Use included example files” checked for a quick test.
6. For your final results, uncheck it and upload your latest DesignBuilder workbook and solver/weather CSV.

## Important limitation

The DesignBuilder building-input CSV does not include exact hourly schedules. The included schedule layer is a DesignBuilder-informed proxy based on activity type, weekday/weekend operation, winter break, and summer reduction. For stronger journal validation, export exact DesignBuilder schedules and replace the proxy function in `hvac_v31_schedule_seasonal_patch.py`.
