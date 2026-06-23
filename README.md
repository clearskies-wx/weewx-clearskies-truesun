# weewx-clearskies-truesun

A weewx extension that replaces the built-in Ryan-Stolzenbach `maxSolarRad` calculation with pvlib's Simplified Solis clear-sky model. Solis includes diffuse radiation and produces physically correct GHI values at all solar elevations, eliminating the near-zero values that R-S produces at sunrise and sunset.

Part of the [Clear Skies](https://github.com/inguy24) weather dashboard project. Licensed under GPL v3.

## Requirements

- weewx 5.x
- Python 3.10+
- pvlib (`pip install pvlib`)
- cdsapi (`pip install cdsapi`) — only if using real-time CAMS AOD

## Installation

```bash
pip install pvlib cdsapi
weectl extension install /path/to/weewx-clearskies-truesun
```

The installer adds a `[ClearSkiesTruesun]` section to `weewx.conf` and registers the service in `xtype_services`.

## Configuration

In `weewx.conf`:

```ini
[ClearSkiesTruesun]
    # CAMS API key for aerosol optical depth forecast.
    # Register free at https://ads.atmosphere.copernicus.eu/
    cams_api_key = REPLACE_ME
    # Fallback AOD at 700nm when CAMS is unavailable (0.06 = typical clean coastal)
    fallback_aod700 = 0.06
    # How often to refresh CAMS AOD forecast (hours)
    aod_fetch_interval_hours = 12
```

Station coordinates and altitude are read from the existing `[Station]` section.

## How it works

1. A weewx XType registered before `StdWXXTypes` intercepts every `maxSolarRad` request.
2. Solar position is computed from the record timestamp and station coordinates.
3. Precipitable water is derived from the record's `outTemp` and `outHumidity` using Gueymard (1994).
4. Aerosol optical depth (AOD) at 700nm comes from a cached CAMS forecast value, fetched daily in a background thread.
5. `pvlib.clearsky.simplified_solis()` computes the clear-sky GHI.
6. The result is returned as a `ValueTuple` — weewx's `prefer_hardware` directive treats it as "hardware" and does not overwrite it.

## Fallback behavior

- **No CAMS API key:** Uses `fallback_aod700` from config (default 0.06, typical clean coastal air).
- **CAMS fetch fails:** Retains previous cached value; retries at next interval.
- **Missing temp/humidity:** Raises `CannotCalculate`; weewx falls back to the built-in R-S formula.
- **Extension not installed:** weewx uses its built-in R-S maxSolarRad (no regression).
