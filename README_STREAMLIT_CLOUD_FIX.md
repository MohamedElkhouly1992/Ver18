# Streamlit Cloud fix for HVAC v3.1

The previous app failed with:

`FileNotFoundError: Example DesignBuilder workbook or driver CSV was not found in examples/.`

This happened because the Streamlit deployment did not include the `examples/` folder at the same level as `streamlit_app.py`, or the bundle was uploaded with the wrong nested structure.

## Correct Streamlit Cloud deployment

Upload/push the contents of this ZIP directly to your GitHub repository root. The repository root must contain:

- `streamlit_app.py`
- `requirements.txt`
- `hvac_v31_engine.py`
- `calibrate_hvac_v31_designbuilder.py`
- `examples/ALL DATA - Design builder Data.xlsx`
- `examples/baseline_no_degradation_daily.csv`

Set Streamlit Cloud main file to:

```text
streamlit_app.py
```

## If examples are not uploaded

The patched app will no longer crash. It will ask you to upload:

1. DesignBuilder daily workbook `.xlsx`
2. Solver/weather daily CSV `.csv`

Then press **Run HVAC v3.1 calibration**.
