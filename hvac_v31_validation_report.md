# HVAC v3.1 DesignBuilder-Calibrated Baseline Solver Report

Training years: [2020, 2021, 2022, 2023]

Validation years: [2024]

Best lag scan result: 7 days. A small lag should not be treated as the only correction unless it strongly reduces CVRMSE.


## Metrics

| case                     |   n_records |   total_designbuilder_kwh |   total_model_kwh |   total_difference_kwh |   overall_percentage_error_pct |   NMBE_pct |   MAPE_pct |   WMAPE_pct |   CVRMSE_pct |   MAE_kwh |   RMSE_kwh |   mean_daily_percentage_error_pct |
|:-------------------------|------------:|--------------------------:|------------------:|-----------------------:|-------------------------------:|-----------:|-----------:|------------:|-------------:|----------:|-----------:|----------------------------------:|
| v31_physics_uncalibrated |        1825 |               8.09162e+06 |       8.21263e+06 |              121016    |                      1.49557   |  1.49639   |    75.2536 |     69.6287 |      83.079  |  3087.17  |   3683.53  |                          12.6049  |
| v31_component_seasonal   |        1825 |               8.09162e+06 |       7.91291e+06 |             -178711    |                     -2.20859   | -2.2098    |    22.9397 |     22.1615 |      29.6225 |   982.589 |   1313.39  |                           2.12469 |
| v31_final_calibrated     |        1825 |               8.09162e+06 |       8.08809e+06 |               -3523.54 |                     -0.0435455 | -0.0435694 |    14.6735 |     14.099  |      19.2964 |   625.118 |    855.556 |                           3.69524 |


## Holdout Metrics

| case                             |   n_records |   total_designbuilder_kwh |   total_model_kwh |   total_difference_kwh |   overall_percentage_error_pct |   NMBE_pct |   MAPE_pct |   WMAPE_pct |   CVRMSE_pct |   MAE_kwh |   RMSE_kwh |   mean_daily_percentage_error_pct |
|:---------------------------------|------------:|--------------------------:|------------------:|-----------------------:|-------------------------------:|-----------:|-----------:|------------:|-------------:|----------:|-----------:|----------------------------------:|
| holdout_v31_physics_uncalibrated |         365 |               1.62211e+06 |       1.63336e+06 |               11246.5  |                       0.693323 |   0.695227 |    74.257  |     68.6578 |      82.0795 |  3051.25  |   3647.73  |                          11.8251  |
| holdout_v31_component_seasonal   |         365 |               1.62211e+06 |       1.5798e+06  |              -42306.3  |                      -2.6081   |  -2.61527  |    23.3167 |     22.6345 |      30.5084 |  1005.91  |   1355.83  |                           1.94339 |
| holdout_v31_final_calibrated     |         365 |               1.62211e+06 |       1.61864e+06 |               -3471.87 |                      -0.214034 |  -0.214622 |    14.8252 |     14.2884 |      20.0322 |   634.997 |    890.261 |                           3.6831  |


## Interpretation

HVAC v3.1 adds the physical layers missing from the earlier baseline: envelope UA, infiltration, solar gains, internal gains, thermal mass lag, heating/cooling deadband, PLR-COP, and separate fan/pump/auxiliary terms. The final calibration layer is intended to correct remaining DesignBuilder-specific schedules and residual daily timing effects without replacing the physical solver.
