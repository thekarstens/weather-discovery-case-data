from __future__ import annotations

import json
import gc
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

PRODUCTS: dict[str, dict[str, Any]] = {
    "temp": {
        "title": "HRRR 2-Meter Temperature",
        "units": "°F",
        "matches": [":TMP:2 m above ground:"],
        "convert": "k_to_f",
        "cmap": "turbo",
        "vmin": -20,
        "vmax": 110,
    },
    "dewpoint": {
        "title": "HRRR 2-Meter Dew Point",
        "units": "°F",
        "matches": [":DPT:2 m above ground:"],
        "convert": "k_to_f",
        "cmap": "YlGn",
        "vmin": 20,
        "vmax": 80,
    },
    "cape": {
        "title": "HRRR Surface-Based CAPE",
        "units": "J/kg",
        "matches": [":CAPE:surface:"],
        "convert": "identity",
        "cmap": "plasma",
        "vmin": 0,
        "vmax": 5000,
    },
    "radar": {
        "title": "HRRR Composite Reflectivity",
        "units": "dBZ",
        "matches": [":REFC:entire atmosphere:", ":REFD:1000 m above ground:"],
        "convert": "identity",
        "cmap": "turbo",
        "vmin": 5,
        "vmax": 75,
        "mask_below": 5,
    },
    "winds": {
        "title": "HRRR Maximum Surface Wind Gust",
        "units": "mph",
        "matches": [":GUST:surface:"],
        "convert": "ms_to_mph",
        "cmap": "YlOrRd",
        "vmin": 10,
        "vmax": 90,
    },
    "pressure": {
        "title": "HRRR Mean Sea-Level Pressure",
        "units": "mb",
        "matches": [":PRMSL:mean sea level:"],
        "convert": "pa_to_mb",
        "cmap": "viridis",
        "vmin": 970,
        "vmax": 1040,
    },
}


def _json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _source_url(run_date: str, run_hour: int, forecast_hour: int) -> str:
    return (
        f"{HRRR_BUCKET}/hrrr.{run_date}/conus/"
        f"hrrr.t{run_hour:02d}z.wrfsfcf{forecast_hour:02d}.grib2"
    )


def _index_ranges(index_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in index_text.splitlines():
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        try:
            rows.append({"offset": int(parts[1]), "text": ":" + parts[2]})
        except ValueError:
            continue
    for i, row in enumerate(rows):
        row["end"] = rows[i + 1]["offset"] - 1 if i + 1 < len(rows) else None
    return rows


def _select_message(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any] | None:
    for pattern in config["matches"]:
        for row in rows:
            if pattern in row["text"]:
                return row
    return None


def _download_message(url: str, row: dict[str, Any], destination: Path) -> None:
    if row.get("end") is None:
        head = requests.head(url, timeout=45)
        head.raise_for_status()
        length = int(head.headers.get("Content-Length", "0"))
        if length <= row["offset"]:
            raise RuntimeError("The selected GRIB message had no safe byte-range end.")
        row = {**row, "end": length - 1}
    headers = {"Range": f"bytes={row['offset']}-{row['end']}", "User-Agent": "WeatherDiscoveryLab/0.6.4"}
    response = requests.get(url, headers=headers, timeout=90)
    if response.status_code != 206:
        raise RuntimeError(f"HRRR byte-range download returned HTTP {response.status_code} instead of 206.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)


def _convert(values, mode: str):
    if mode == "k_to_f":
        return (values - 273.15) * 9.0 / 5.0 + 32.0
    if mode == "ms_to_mph":
        return values * 2.2369362921
    if mode == "pa_to_mb":
        return values / 100.0
    return values


def _decode_crop(grib_path: Path, bounds: dict[str, float], config: dict[str, Any]):
    try:
        import numpy as np
        import xarray as xr
    except Exception as exc:
        raise RuntimeError(
            "Archived HRRR processing needs xarray, cfgrib, and eccodes. "
            "Close the controller and run run_controller_level2_py312.bat once."
        ) from exc

    dataset = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        if not dataset.data_vars:
            raise RuntimeError("The selected HRRR message contained no decodable field.")
        field = dataset[next(iter(dataset.data_vars))].squeeze(drop=True)
        # float32 halves the largest temporary arrays without reducing useful
        # map or point-value precision.
        lat = np.asarray(dataset["latitude"].values, dtype=np.float32)
        lon = np.asarray(dataset["longitude"].values, dtype=np.float32)
        lon = np.where(lon > 180.0, lon - 360.0, lon)
        values = _convert(np.asarray(field.values, dtype=np.float32), config["convert"])
        if values.ndim != 2 or lat.ndim != 2 or lon.ndim != 2:
            raise RuntimeError("The HRRR grid was not a two-dimensional CONUS field.")
        inside = (
            (lat >= bounds["south"]) & (lat <= bounds["north"])
            & (lon >= bounds["west"]) & (lon <= bounds["east"])
        )
        rows, cols = np.where(inside)
        if not len(rows):
            raise RuntimeError("The requested map bounds did not intersect the HRRR grid.")
        r0, r1 = max(0, int(rows.min()) - 1), min(values.shape[0], int(rows.max()) + 2)
        c0, c1 = max(0, int(cols.min()) - 1), min(values.shape[1], int(cols.max()) + 2)
        return lat[r0:r1, c0:c1], lon[r0:r1, c0:c1], values[r0:r1, c0:c1]
    finally:
        dataset.close()


def _render_png(lat, lon, values, output: Path, bounds: dict[str, float], config: dict[str, Any], pixels: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plot_values = np.ma.masked_invalid(values)
    if config.get("mask_below") is not None:
        plot_values = np.ma.masked_less(plot_values, float(config["mask_below"]))
    dpi = 100
    width = max(8.0, pixels / dpi)
    aspect = max(0.45, (bounds["north"] - bounds["south"]) / max(0.1, bounds["east"] - bounds["west"]))
    fig = plt.figure(figsize=(width, width * aspect), dpi=dpi, frameon=False)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(bounds["west"], bounds["east"])
    ax.set_ylim(bounds["south"], bounds["north"])
    ax.pcolormesh(
        lon,
        lat,
        plot_values,
        shading="auto",
        cmap=config["cmap"],
        vmin=config["vmin"],
        vmax=config["vmax"],
        rasterized=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, transparent=True, bbox_inches=None, pad_inches=0)
    plt.close(fig)


def _nearest_value(lat, lon, values, point_lat: float, point_lon: float) -> float | None:
    import numpy as np

    distance = (lat - point_lat) ** 2 + ((lon - point_lon) * math.cos(math.radians(point_lat))) ** 2
    valid = np.isfinite(values)
    if not valid.any():
        return None
    distance = np.where(valid, distance, np.inf)
    index = np.unravel_index(int(np.argmin(distance)), distance.shape)
    value = float(values[index])
    return round(value, 1) if math.isfinite(value) else None


def build_hrrr_case(
    case_dir: Path,
    run_date: str,
    run_hour: int,
    forecast_hours: list[int],
    products: list[str],
    bounds: dict[str, float],
    station: dict[str, Any],
    pixels: int = 1600,
    progress=None,
) -> dict[str, Any]:
    datetime.strptime(run_date, "%Y%m%d")
    if run_hour < 0 or run_hour > 23:
        raise ValueError("HRRR run hour must be from 0 through 23 UTC.")
    selected_products = [p for p in products if p in PRODUCTS]
    if not selected_products:
        raise ValueError("Select at least one supported HRRR product.")

    run_time = datetime.strptime(f"{run_date}{run_hour:02d}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    run_id = f"{run_date}_{run_hour:02d}z"
    model_root = case_dir / "model" / "hrrr"
    run_root = model_root / run_id
    manifests: dict[str, Any] = {}
    errors: dict[str, str] = {}

    total = len(selected_products) * len(forecast_hours)
    completed = 0
    for product in selected_products:
        config = PRODUCTS[product]
        frames: list[dict[str, Any]] = []
        product_root = run_root / product
        for forecast_hour in forecast_hours:
            lat = lon = values = None
            url = _source_url(run_date, run_hour, forecast_hour)
            try:
                idx_response = requests.get(url + ".idx", timeout=45, headers={"User-Agent": "WeatherDiscoveryLab/0.6.4"})
                idx_response.raise_for_status()
                row = _select_message(_index_ranges(idx_response.text), config)
                if not row:
                    raise RuntimeError(f"{config['title']} was not found in the HRRR surface-file index.")
                raw_path = product_root / "raw" / f"{product}_f{forecast_hour:02d}.grib2"
                if not raw_path.exists():
                    _download_message(url, row, raw_path)
                lat, lon, values = _decode_crop(raw_path, bounds, config)
                frame_name = f"{product}_f{forecast_hour:02d}.png"
                frame_path = product_root / "frames" / frame_name
                _render_png(lat, lon, values, frame_path, bounds, config, pixels)
                valid = run_time + timedelta(hours=forecast_hour)
                point_value = _nearest_value(lat, lon, values, float(station["lat"]), float(station["lon"]))
                frames.append({
                    "time": valid.isoformat(),
                    "epoch": valid.timestamp(),
                    "file": f"frames/{frame_name}",
                    "label": f"F{forecast_hour:02d}",
                    "forecast_hour": forecast_hour,
                    "run_utc": run_time.isoformat(),
                    "station_value": point_value,
                })
            except Exception as exc:
                errors[f"{product}:F{forecast_hour:02d}"] = str(exc)
            finally:
                completed += 1
                # Drop cfgrib/matplotlib arrays after every frame. The outer
                # worker process provides a second hard memory boundary.
                lat = lon = values = None
                gc.collect()
                if progress:
                    progress(product, forecast_hour, completed, total, f"{product.upper()} F{forecast_hour:02d} • {completed}/{total}")

        existing_manifest = {}
        existing_manifest_path = product_root / "manifest.json"
        if existing_manifest_path.exists():
            try:
                existing_manifest = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
            except Exception:
                existing_manifest = {}
        if existing_manifest.get("run_id") == run_id:
            merged_frames = {int(item.get("forecast_hour", -1)): item for item in existing_manifest.get("frames", [])}
            merged_frames.update({int(item.get("forecast_hour", -1)): item for item in frames})
            frames = [merged_frames[key] for key in sorted(merged_frames) if key >= 0]
        manifest = {
            "id": f"hrrr-{product}",
            "model": "HRRR",
            "product": config["title"],
            "product_id": product,
            "units": config["units"],
            "run_utc": run_time.isoformat(),
            "run_id": run_id,
            "bounds": bounds,
            "station": station,
            "frames": frames,
            "source": "NOAA HRRR public archive",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _json(product_root / "manifest.json", manifest)
        manifests[product] = manifest

    previous = {}
    previous_path = model_root / "index.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    previous_products = previous.get("products", {}) if previous.get("active_run") == run_id else {}
    index = {
        "model": "HRRR",
        "active_run": run_id,
        "run_utc": run_time.isoformat(),
        "products": {**previous_products, **{
            product: {
                "available": bool(manifest["frames"]),
                "frames": len(manifest["frames"]),
                "manifest": f"{run_id}/{product}/manifest.json",
            }
            for product, manifest in manifests.items()
        }},
        "bounds": bounds,
        "station": station,
        "errors": errors,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _json(model_root / "index.json", index)
    return index
