"""Package already-rendered HRRR case data without decoding model grids."""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path


def main(case_id: str) -> Path:
    data_root = Path(os.environ.get("WDL_DATA_DIR") or (Path.home() / "WeatherCaseControllerData"))
    source = data_root / "cases" / case_id / "model" / "hrrr"
    index_path = source / "index.json"
    if not index_path.is_file():
        raise RuntimeError(f"No rendered HRRR index was found for case {case_id}.")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    run_id = str(index.get("active_run") or "")
    if not re.fullmatch(r"\d{8}_\d{2}z", run_id):
        raise RuntimeError("The case HRRR index has no valid active run.")
    output = Path.cwd() / f"HRRR_{case_id}_{run_id}.zip"
    count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(index_path, "index.json")
        for product in ("temp", "dewpoint", "cape", "radar", "winds", "pressure"):
            product_dir = source / run_id / product
            manifest = product_dir / "manifest.json"
            if not manifest.is_file():
                continue
            archive.write(manifest, f"{run_id}/{product}/manifest.json")
            for frame in sorted((product_dir / "frames").glob("*.png")):
                archive.write(frame, f"{run_id}/{product}/frames/{frame.name}")
                count += 1
    if not count:
        output.unlink(missing_ok=True)
        raise RuntimeError("No rendered HRRR PNG frames were found.")
    print(f"Created {output} with {count} pre-rendered frames.")
    return output


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python make_hrrr_import_package.py CASE_ID")
    try:
        main(sys.argv[1])
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}")
