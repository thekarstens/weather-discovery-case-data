# NOAA NARR particle builder

This builder runs in GitHub Actions, reads only the requested times from NOAA PSL's NARR OPeNDAP archive, regrids the native Lambert grid to a compact regular latitude/longitude grid, and creates Leaflet Velocity U/V JSON frames.

Products:

- `surface`: NARR 10 m U and V wind
- `500mb`: NARR 500 mb U and V wind

The resulting ZIP is a finished presentation asset. The controller does not open NetCDF files or run SciPy on the laptop.

