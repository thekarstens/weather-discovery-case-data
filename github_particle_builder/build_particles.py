"""Build compact Leaflet Velocity fields from NOAA PSL NARR archives."""
from __future__ import annotations

import argparse
import json
import math
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import xarray as xr
from scipy.interpolate import griddata


PSL_DODS = "https://psl.noaa.gov/thredds/dodsC/Datasets/NARR"
PRODUCTS = {"surface", "500mb"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--start-utc", required=True, help="YYYY-MM-DDTHH")
    parser.add_argument("--end-utc", required=True, help="YYYY-MM-DDTHH")
    parser.add_argument("--step-hours", type=int, default=3)
    parser.add_argument("--products", default="surface,500mb")
    parser.add_argument("--south", type=float, default=20.0)
    parser.add_argument("--west", type=float, default=-130.0)
    parser.add_argument("--north", type=float, default=60.0)
    parser.add_argument("--east", type=float, default=-60.0)
    parser.add_argument("--spacing", type=float, default=0.5)
    parser.add_argument("--output", default="output")
    return parser.parse_args()


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def times(start: datetime, end: datetime, step: int) -> list[datetime]:
    if end < start or not 1 <= step <= 24:
        raise ValueError("Invalid time range or step.")
    result = []
    current = start
    while current <= end:
        result.append(current)
        current += timedelta(hours=step)
    return result


def source_urls(product: str, valid: datetime) -> tuple[str, str, int | None]:
    if product == "surface":
        year = valid.strftime("%Y")
        return (
            f"{PSL_DODS}/monolevel/uwnd.10m.{year}.nc",
            f"{PSL_DODS}/monolevel/vwnd.10m.{year}.nc",
            None,
        )
    month = valid.strftime("%Y%m")
    return (
        f"{PSL_DODS}/pressure/uwnd.{month}.nc",
        f"{PSL_DODS}/pressure/vwnd.{month}.nc",
        500,
    )


def select_field(dataset: xr.Dataset, variable: str, valid: datetime, level: int | None) -> tuple[np.ndarray, datetime]:
    field = dataset[variable]
    if level is not None and "level" in field.dims:
        field = field.sel(level=level, method="nearest")
    selected = field.sel(time=np.datetime64(valid.replace(tzinfo=None)), method="nearest")
    selected_time = np.datetime64(selected["time"].values).astype("datetime64[s]").astype(datetime).replace(tzinfo=timezone.utc)
    return np.asarray(selected.values, dtype=np.float32).squeeze(), selected_time


def regular_grid(
    lat: np.ndarray,
    lon: np.ndarray,
    values: np.ndarray,
    south: float,
    west: float,
    north: float,
    east: float,
    spacing: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon = np.where(lon > 180, lon - 360, lon)
    target_lon = np.arange(west, east + spacing * 0.25, spacing, dtype=np.float32)
    target_lat = np.arange(north, south - spacing * 0.25, -spacing, dtype=np.float32)
    mesh_lon, mesh_lat = np.meshgrid(target_lon, target_lat)
    valid = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(values)
    points = np.column_stack((lon[valid], lat[valid]))
    linear = griddata(points, values[valid], (mesh_lon, mesh_lat), method="linear")
    missing = ~np.isfinite(linear)
    if missing.any():
        nearest = griddata(points, values[valid], (mesh_lon[missing], mesh_lat[missing]), method="nearest")
        linear[missing] = nearest
    return target_lat, target_lon, np.asarray(linear, dtype=np.float32)


def velocity_records(u: np.ndarray, v: np.ndarray, valid: datetime, level_name: str, west: float, north: float, spacing: float) -> list[dict]:
    common = {
        "nx": int(u.shape[1]),
        "ny": int(u.shape[0]),
        "lo1": float(west),
        "la1": float(north),
        "dx": float(spacing),
        "dy": float(spacing),
        "refTime": valid.isoformat(),
        "gridUnits": "degrees",
        "parameterCategory": 2,
        "parameterUnit": "m/s",
    }
    return [
        {"header": {**common, "parameterNumber": 2, "parameterNumberName": f"u-wind {level_name}"}, "data": u.ravel().round(4).tolist()},
        {"header": {**common, "parameterNumber": 3, "parameterNumberName": f"v-wind {level_name}"}, "data": v.ravel().round(4).tolist()},
    ]


def build_product(product: str, requested_times: list[datetime], root: Path, bounds: dict[str, float], spacing: float) -> dict:
    product_root = root / product
    frames_root = product_root / "frames"
    frames_root.mkdir(parents=True, exist_ok=True)
    opened: dict[str, xr.Dataset] = {}
    frames = []
    try:
        for requested in requested_times:
            u_url, v_url, level = source_urls(product, requested)
            if u_url not in opened:
                print(f"Opening NOAA PSL {u_url}", flush=True)
                opened[u_url] = xr.open_dataset(u_url, engine="netcdf4")
            if v_url not in opened:
                print(f"Opening NOAA PSL {v_url}", flush=True)
                opened[v_url] = xr.open_dataset(v_url, engine="netcdf4")
            u_ds, v_ds = opened[u_url], opened[v_url]
            u, u_time = select_field(u_ds, "uwnd", requested, level)
            v, v_time = select_field(v_ds, "vwnd", requested, level)
            valid = min(u_time, v_time)
            lat = np.asarray(u_ds["lat"].values, dtype=np.float32)
            lon = np.asarray(u_ds["lon"].values, dtype=np.float32)
            _, _, u_grid = regular_grid(lat, lon, u, **bounds, spacing=spacing)
            _, _, v_grid = regular_grid(lat, lon, v, **bounds, spacing=spacing)
            level_name = "10m" if product == "surface" else "500mb"
            filename = f"narr_{level_name}_{valid:%Y%m%d_%H%M}z_leaflet_velocity.json"
            (frames_root / filename).write_text(
                json.dumps(velocity_records(u_grid, v_grid, valid, level_name, bounds["west"], bounds["north"], spacing), separators=(",", ":")),
                encoding="utf-8",
            )
            frames.append({"filename": f"frames/{filename}", "refTime": valid.isoformat()})
            print(f"Built {product} {valid.isoformat()}", flush=True)
    finally:
        for dataset in opened.values():
            dataset.close()
    manifest = {
        "id": f"narr_{product}",
        "title": "NARR 10 m Surface Winds" if product == "surface" else "NARR 500 mb Winds",
        "dataset": "NOAA NCEP North American Regional Reanalysis (NARR)",
        "product_type": "velocity",
        "level": "10m" if product == "surface" else "500mb",
        "units": "m/s",
        "bounds": [[bounds["south"], bounds["west"]], [bounds["north"], bounds["east"]]],
        "frames": frames,
    }
    (product_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    args = parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.case_id):
        raise SystemExit("case-id must use only letters, numbers, hyphens, and underscores")
    selected = [item.strip().lower() for item in args.products.split(",") if item.strip().lower() in PRODUCTS]
    if not selected:
        raise SystemExit("Choose surface, 500mb, or both.")
    bounds = {"south": args.south, "west": args.west, "north": args.north, "east": args.east}
    if not (bounds["south"] < bounds["north"] and bounds["west"] < bounds["east"] and 0.25 <= args.spacing <= 2):
        raise SystemExit("Invalid bounds or spacing.")
    requested = times(parse_time(args.start_utc), parse_time(args.end_utc), args.step_hours)
    output = Path(args.output).resolve()
    package_root = output / f"particles_{args.case_id}"
    package_root.mkdir(parents=True, exist_ok=True)
    manifests = {product: build_product(product, requested, package_root, bounds, args.spacing) for product in selected}
    index = {
        "case_id": args.case_id,
        "dataset": "NOAA NARR",
        "products": {key: {"frames": len(value["frames"]), "manifest": f"{key}/manifest.json"} for key, value in manifests.items()},
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (package_root / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    zip_path = output / f"NARR_particles_{args.case_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in package_root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(package_root))
    print(f"PACKAGE_PATH={zip_path}", flush=True)


if __name__ == "__main__":
    main()

