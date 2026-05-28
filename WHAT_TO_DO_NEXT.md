# What to do next after HVAC v3.1

1. Run `python run_hvac_v31_auto.py` and confirm the output metrics.
2. Use `energy_kwh_v31_final` as the clean baseline daily energy.
3. Replace the old baseline column in your HVAC v3 project with the v3.1 final baseline.
4. Rerun clean validation against DesignBuilder.
5. Only after daily CVRMSE is acceptable, rerun S0, S1, S2, and S3.
6. Generate the two-axis Severity × Strategy matrix.
7. Use CatBoost only as a surrogate/acceleration layer, not as the main validation proof.

Target metrics for the clean baseline:

| Metric | Acceptable target |
|---|---:|
| NMBE | within ±5% |
| Daily CVRMSE | below 25–30% |
| Daily MAPE | 15–25%, preferably lower |
| Annual total error | within ±5% |
