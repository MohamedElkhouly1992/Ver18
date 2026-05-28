# Streamlit Cloud fix

This version fixes the `AttributeError` caused by duplicate calendar columns (`year`, `month`, `day_of_year`) after combining DesignBuilder data with solver outputs.

## What changed
`streamlit_app_v31.py` now removes duplicated calendar columns before concatenation and rebuilds `date`, `year`, `month`, and `day_of_year` as clean Series.

## Run locally
```bash
pip install -r requirements.txt
streamlit run streamlit_app_v31.py
```

## Streamlit Cloud
Use `streamlit_app_v31.py` as the main file.
