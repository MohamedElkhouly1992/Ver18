# DesignBuilder Building Extraction Report

## Extracted building constants
- **number_of_zones**: 312
- **zone_count_extracted**: 312
- **activity_type**: Generic Office Area
- **floor_area_m2**: 17948.18
- **volume_m3**: 97074.29
- **external_area_m2**: 30155.8
- **area_weighted_u_value_w_m2k**: 1.137
- **heating_setpoint_c_median**: 22.0
- **cooling_setpoint_c_median**: 24.0
- **ua_wall_w_k**: 22789.1937
- **ua_glazing_w_k**: 2748.1199
- **ua_roof_w_k**: 1484.3643
- **ua_ground_floor_w_k**: 1580.0067
- **ua_external_floor_w_k**: 82.1291
- **ua_infiltration_w_k**: 24495.564
- **ua_envelope_no_infiltration_w_k**: 28683.8137
- **ua_total_with_infiltration_w_k**: 53179.3777

## Scientific limitation
The uploaded DesignBuilder building report does not include the exact hourly occupancy, lighting, equipment, HVAC, holiday, or thermostat schedules. The generated `operational_schedule_enhanced.csv` is therefore a transparent activity-based proxy derived from the activity type, setpoints, and building area. For exact matching, export the actual schedules from DesignBuilder and replace the proxy factors.

## How to use in HVAC v3/v3.1
Merge `operational_schedule_enhanced.csv` into the daily weather/driver file by `date`, then use `occupancy_factor`, `lighting_schedule_factor`, `equipment_schedule_factor`, `fan_schedule_factor`, `pump_schedule_factor`, and mode weights inside the core solver.
