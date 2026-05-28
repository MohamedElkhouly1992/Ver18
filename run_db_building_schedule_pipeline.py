from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from db_building_extractor import (
    extract_all,
    export_extraction_outputs,
    learn_monthly_factors_from_reference,
)


def main():
    parser = argparse.ArgumentParser(description="DesignBuilder building-input extraction + operational schedule generation for HVAC v3/v3.1.")
    parser.add_argument("--building_file", required=True, help="DesignBuilder building-data export. It may have .csv extension but be Excel format.")
    parser.add_argument("--output_dir", default="db_building_outputs")
    parser.add_argument("--start_year", type=int, default=2020)
    parser.add_argument("--end_year", type=int, default=2024)
    parser.add_argument("--designbuilder_daily", default=None, help="Optional daily DesignBuilder reference output for learning monthly seasonal profile.")
    parser.add_argument("--solver_daily", default=None, help="Optional solver daily output for learning DB/solver monthly correction factors.")
    parser.add_argument("--db_energy_col", default=None)
    parser.add_argument("--solver_energy_col", default=None)
    args = parser.parse_args()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    extracted = extract_all(args.building_file)
    paths = export_extraction_outputs(extracted, outdir, args.start_year, args.end_year)

    if args.designbuilder_daily:
        db = pd.read_excel(args.designbuilder_daily) if args.designbuilder_daily.lower().endswith((".xlsx", ".xls")) else pd.read_csv(args.designbuilder_daily)
        solver = None
        if args.solver_daily:
            solver = pd.read_csv(args.solver_daily)
        monthly = learn_monthly_factors_from_reference(db, solver, args.db_energy_col, args.solver_energy_col)
        p = outdir / "monthly_seasonal_factors_from_reference.csv"
        monthly.to_csv(p, index=False)
        paths[p.name] = str(p)

    print("Created outputs:")
    for k, v in paths.items():
        print(f"- {k}: {v}")


if __name__ == "__main__":
    main()
