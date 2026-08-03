"""Build a compact Weather Discovery Lab HRRR package in GitHub Actions."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from make_hrrr_import_package import main as package_case
from model_renderer import build_hrrr_case


ALLOWED_PRODUCTS = ("temp", "dewpoint", "cape", "radar", "winds", "pressure")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--run-hour", type=int, required=True)
    parser.add_argument("--f-start", type=int, default=0)
    parser.add_argument("--f-end", type=int, default=12)
    parser.add_argument("--f-step", type=int, default=1)
    parser.add_argument("--products", default="temp,dewpoint,radar,winds,cape,pressure")
    parser.add_argument("--south", type=float, default=40.0)
    parser.add_argument("--west", type=float, default=-104.5)
    parser.add_argument("--north", type=float, default=47.5)
    parser.add_argument("--east", type=float, default=-91.0)
    parser.add_argument("--station-id", default="FSD")
    parser.add_argument("--station-name", default="Sioux Falls")
    parser.add_argument("--station-lat", type=float, default=43.5820)
    parser.add_argument("--station-lon", type=float, default=-96.7419)
    parser.add_argument("--pixels", type=int, default=1280)
    parser.add_argument("--output", default="output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.case_id):
        raise SystemExit("case-id must use only letters, numbers, hyphens, and underscores")
    if not re.fullmatch(r"\d{8}", args.run_date) or not 0 <= args.run_hour <= 23:
        raise SystemExit("run date/hour is invalid")
    if args.f_end < args.f_start or args.f_step < 1:
        raise SystemExit("forecast-hour range is invalid")
    products = [item.strip() for item in args.products.split(",") if item.strip() in ALLOWED_PRODUCTS]
    if not products:
        raise SystemExit("select at least one supported product")

    workspace = Path("work").resolve()
    case_dir = workspace / "cases" / args.case_id
    os.environ["WDL_DATA_DIR"] = str(workspace)

    def progress(product: str, forecast_hour: int, completed: int, total: int, message: str) -> None:
        print(f"[{completed}/{total}] {product} F{forecast_hour:02d}: {message}", flush=True)

    build_hrrr_case(
        case_dir=case_dir,
        run_date=args.run_date,
        run_hour=args.run_hour,
        forecast_hours=list(range(args.f_start, args.f_end + 1, args.f_step)),
        products=products,
        bounds={"south": args.south, "west": args.west, "north": args.north, "east": args.east},
        station={"id": args.station_id, "name": args.station_name, "lat": args.station_lat, "lon": args.station_lon},
        pixels=max(800, min(1920, args.pixels)),
        progress=progress,
    )
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    previous = Path.cwd()
    try:
        os.chdir(output)
        package = package_case(args.case_id)
    finally:
        os.chdir(previous)
    print(f"PACKAGE_PATH={package}", flush=True)


if __name__ == "__main__":
    main()
