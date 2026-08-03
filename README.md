# Weather Discovery case data

This repository is the off-laptop data factory for the Weather Discovery Lab Case Controller.

## Automated builders

- **Build HRRR case package** downloads only selected NOAA HRRR fields, crops and renders them in GitHub Actions, and publishes a stable Release ZIP.
- **Build NOAA NARR particle package** reads requested 10 m and 500 mb winds from NOAA PSL, converts them to compact Leaflet Velocity JSON, and publishes a stable Release ZIP.

The presentation laptop does not decode GRIB or NetCDF model grids. Controller v0.7.3 connects to this repository and imports finished HRRR Release packages from its Model tab.

## Data sources

- NOAA HRRR public archive
- NOAA PSL North American Regional Reanalysis (NARR)

Use the **Actions** tab to run a builder. Start with one time and one product when testing a new case, then expand the range after the small test succeeds.
