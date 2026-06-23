"""weewx XType extension — ClearSkiesTruesun.

Replaces weewx's built-in maxSolarRad (Ryan-Stolzenbach direct-beam model)
with pvlib's Simplified Solis clear-sky model, which includes diffuse
radiation and produces physically correct values at all sun angles.

Atmospheric inputs:
  - Precipitable water: derived from station outTemp/outHumidity via
    pvlib.atmosphere.gueymard94_pw().
  - Aerosol optical depth at 700 nm (AOD700): fetched from the CAMS global
    atmospheric composition forecast once per interval in a background thread.
    Falls back to a configurable constant when CAMS is unavailable.

Install via:  weectl extension install <path-to-this-directory>

This file runs inside the weewx process where weewx is already on sys.path.
Do not import it from the API or any other service.

Copyright (C) 2026  Clear Skies Contributors
Licensed under the GNU General Public License v3 — see LICENSE.
"""

from __future__ import annotations

import datetime
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import pandas as pd
from pvlib.atmosphere import angstrom_aod_at_lambda, gueymard94_pw
from pvlib.clearsky import simplified_solis
from pvlib.solarposition import get_solarposition

import weewx
import weewx.engine
import weewx.units
import weewx.xtypes

logger = logging.getLogger(__name__)

# weewx unit system constants
_US = 1       # US customary (°F, inHg, mph, …)
_METRIC = 16  # METRIC (°C, mbar, km/h, …)
_METRICWX = 17  # METRICWX (°C, hPa, m/s, …)

# ---------------------------------------------------------------------------
# Thread-safe AOD cache
# ---------------------------------------------------------------------------

class _AODCache:
    """Thread-safe cache for the current AOD700 value.

    The CAMS background thread writes; the XType's get_scalar() reads.
    If no CAMS data has arrived yet, the fallback value is returned.
    """

    def __init__(self, fallback_aod700: float) -> None:
        self._lock = threading.Lock()
        self._aod700: float = fallback_aod700
        self._fallback: float = fallback_aod700
        self._last_fetch: float | None = None

    def get(self) -> float:
        with self._lock:
            return self._aod700

    def set(self, aod700: float, fetch_time: float | None = None) -> None:
        with self._lock:
            self._aod700 = aod700
            self._last_fetch = fetch_time

    @property
    def fallback(self) -> float:
        return self._fallback


# ---------------------------------------------------------------------------
# XType: maxSolarRad via Simplified Solis
# ---------------------------------------------------------------------------

class ClearSkiesTruesunXType(weewx.xtypes.XType):
    """Calculates maxSolarRad using pvlib's Simplified Solis model.

    Registered at position 0 in the xtypes list so it runs before
    StdWXXTypes.  Because StdWXCalculate uses ``prefer_hardware`` for
    maxSolarRad, our value is treated as "hardware" and not overwritten.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        altitude: float,
        aod_cache: _AODCache,
    ) -> None:
        super().__init__()
        self._latitude = latitude
        self._longitude = longitude
        self._altitude = altitude
        self._aod_cache = aod_cache

    def get_scalar(
        self,
        obs_type: str,
        record: dict[str, Any] | None,
        db_manager: Any = None,  # noqa: ANN401
    ) -> weewx.units.ValueTuple:
        if obs_type != "maxSolarRad":
            raise weewx.UnknownType(obs_type)

        if record is None:
            raise weewx.CannotCalculate(obs_type)

        timestamp = record.get("dateTime")
        out_temp = record.get("outTemp")
        out_humidity = record.get("outHumidity")

        if any(v is None for v in (timestamp, out_temp, out_humidity)):
            raise weewx.CannotCalculate(obs_type)

        # --- Unit conversion: ensure temp is in °C ---
        us_units = record.get("usUnits", _US)
        if us_units == _US:
            temp_c = (out_temp - 32.0) * 5.0 / 9.0
        else:
            # METRIC and METRICWX both store temperature in °C
            temp_c = float(out_temp)

        # --- Precipitable water from station sensors (cm) ---
        pw = float(gueymard94_pw(temp_c, out_humidity))

        # --- AOD700 from cached CAMS value ---
        aod700 = self._aod_cache.get()

        # --- Solar position ---
        dt_index = pd.DatetimeIndex(
            [pd.Timestamp(timestamp, unit="s", tz="UTC")]
        )
        solar_pos = get_solarposition(
            dt_index,
            latitude=self._latitude,
            longitude=self._longitude,
            altitude=self._altitude,
        )
        apparent_elevation = float(solar_pos["apparent_elevation"].iloc[0])

        # Sun below horizon — no clear-sky radiation
        if apparent_elevation <= 0:
            return weewx.units.ValueTuple(
                0.0, "watt_per_meter_squared", "group_radiation"
            )

        # --- Simplified Solis clear-sky GHI ---
        cs = simplified_solis(
            apparent_elevation,
            aod700=aod700,
            precipitable_water=pw,
        )
        # simplified_solis returns an OrderedDict when given scalar inputs
        ghi = float(cs["ghi"])

        return weewx.units.ValueTuple(
            ghi, "watt_per_meter_squared", "group_radiation"
        )


# ---------------------------------------------------------------------------
# CAMS AOD background fetch thread
# ---------------------------------------------------------------------------

class _CAMSFetchThread(threading.Thread):
    """Daemon thread that periodically fetches AOD550 from CAMS and converts
    it to AOD700 for use by the XType.

    Uses the cdsapi library to retrieve
    ``cams-global-atmospheric-composition-forecasts`` and reads the result
    with netCDF4 (or h5py as a fallback).
    """

    def __init__(
        self,
        api_key: str,
        latitude: float,
        longitude: float,
        aod_cache: _AODCache,
        fetch_interval_hours: float,
    ) -> None:
        super().__init__(daemon=True, name="clearskies-truesun-cams")
        self._api_key = api_key
        self._lat = latitude
        self._lon = longitude
        self._cache = aod_cache
        self._interval = fetch_interval_hours * 3600  # seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._fetch_aod()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CAMS AOD fetch failed (will retry in %.0f h): %s",
                    self._interval / 3600,
                    exc,
                )
            # Sleep in small increments so stop() is responsive
            self._stop_event.wait(timeout=self._interval)

    # ---------------------------------------------------------------
    # .cdsapirc management
    # ---------------------------------------------------------------

    @staticmethod
    def _ensure_cdsapirc(api_key: str) -> None:
        """Write ``~/.cdsapirc`` if it doesn't already exist."""
        rc_path = Path.home() / ".cdsapirc"
        if rc_path.exists():
            return
        rc_path.write_text(
            f"url: https://ads.atmosphere.copernicus.eu/api\n"
            f"key: {api_key}\n",
            encoding="utf-8",
        )
        # Restrict permissions (best-effort; Windows ignores chmod)
        try:
            os.chmod(rc_path, 0o600)
        except OSError:
            pass
        logger.info("ClearSkiesTruesun: wrote %s", rc_path)

    # ---------------------------------------------------------------
    # CAMS retrieval + parsing
    # ---------------------------------------------------------------

    def _fetch_aod(self) -> None:
        """Retrieve today's CAMS AOD550 forecast, convert to AOD700, cache."""
        import cdsapi  # noqa: PLC0415 — deferred import; optional dependency

        self._ensure_cdsapirc(self._api_key)

        today = datetime.date.today().isoformat()

        # Small bounding box around the station (±0.5°)
        north = min(self._lat + 0.5, 90.0)
        south = max(self._lat - 0.5, -90.0)
        east = self._lon + 0.5
        west = self._lon - 0.5

        with tempfile.TemporaryDirectory(prefix="truesun_cams_") as tmp_dir:
            download_path = os.path.join(tmp_dir, "cams_aod.nc")

            client = cdsapi.Client(quiet=True)
            client.retrieve(
                "cams-global-atmospheric-composition-forecasts",
                {
                    "variable": "total_aerosol_optical_depth_550nm",
                    "date": today,
                    "time": "00:00",
                    "leadtime_hour": [str(h) for h in range(0, 25, 3)],
                    "type": "forecast",
                    "area": [north, west, south, east],
                    "data_format": "netcdf",
                },
                download_path,
            )

            aod550_median = self._read_aod550(download_path)

        # Convert AOD550 → AOD700 using Angstrom turbidity model.
        # angstrom_aod_at_lambda(aod0, lambda0, alpha=1.14, lambda1=700)
        # With defaults: alpha=1.14, lambda1=700 → returns AOD at 700 nm.
        aod700 = float(angstrom_aod_at_lambda(aod550_median, 550))

        self._cache.set(aod700, fetch_time=datetime.datetime.now().timestamp())
        logger.info(
            "ClearSkiesTruesun: CAMS AOD updated — "
            "AOD550=%.4f → AOD700=%.4f (date=%s)",
            aod550_median,
            aod700,
            today,
        )

    @staticmethod
    def _read_aod550(nc_path: str) -> float:
        """Read AOD550 values from a CAMS netCDF file and return the median.

        Tries netCDF4 first (standard for atmospheric science), falls back
        to h5py (netCDF4 files are HDF5 under the hood).
        """
        try:
            return _CAMSFetchThread._read_aod550_netcdf4(nc_path)
        except ImportError:
            return _CAMSFetchThread._read_aod550_h5py(nc_path)

    @staticmethod
    def _read_aod550_netcdf4(nc_path: str) -> float:
        """Read AOD550 using the netCDF4 library."""
        import netCDF4  # noqa: PLC0415, N813

        with netCDF4.Dataset(nc_path, "r") as ds:
            # CAMS variable name for total AOD at 550 nm
            # Common names: 'aod550', 'aod550_aer', 'AOD_550nm', 'aod'
            var_name = _find_aod_variable(list(ds.variables.keys()))
            data = ds.variables[var_name][:]
            import numpy as np  # noqa: PLC0415

            values = np.ma.filled(data, fill_value=np.nan).flatten()
            values = values[~np.isnan(values)]
            if len(values) == 0:
                msg = "No valid AOD550 values in CAMS download"
                raise ValueError(msg)  # noqa: TRY301
            return float(np.median(values))

    @staticmethod
    def _read_aod550_h5py(nc_path: str) -> float:
        """Read AOD550 using h5py (fallback for netCDF4-format files)."""
        import h5py  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        with h5py.File(nc_path, "r") as f:
            var_name = _find_aod_variable(list(f.keys()))
            data = f[var_name][:]
            values = np.array(data, dtype=float).flatten()
            values = values[~np.isnan(values)]
            if len(values) == 0:
                msg = "No valid AOD550 values in CAMS download"
                raise ValueError(msg)  # noqa: TRY301
            return float(np.median(values))


def _find_aod_variable(var_names: list[str]) -> str:
    """Find the AOD550 variable name among dataset variables.

    CAMS datasets use various names depending on the product version.
    """
    # Exact matches first, then substring matches
    candidates = [
        "aod550",
        "aod550_aer",
        "total_aerosol_optical_depth_550nm",
        "AOD_550nm",
        "aod",
    ]
    lower_names = {n.lower(): n for n in var_names}
    for candidate in candidates:
        if candidate.lower() in lower_names:
            return lower_names[candidate.lower()]

    # Substring fallback — look for any variable containing "aod" or "aerosol"
    for name in var_names:
        nl = name.lower()
        if "aod" in nl or "aerosol_optical" in nl:
            return name

    msg = (
        f"Cannot identify AOD550 variable in CAMS data. "
        f"Available variables: {var_names}"
    )
    raise KeyError(msg)


# ---------------------------------------------------------------------------
# weewx Service: lifecycle management
# ---------------------------------------------------------------------------

class ClearSkiesTruesunService(weewx.engine.StdService):
    """Manages the ClearSkiesTruesun XType and CAMS background thread.

    Reads configuration from the ``[ClearSkiesTruesun]`` section in
    weewx.conf and station coordinates from ``[Station]``.
    """

    def __init__(self, engine: Any, config_dict: Any) -> None:  # noqa: ANN401
        super().__init__(engine, config_dict)

        # -- Read extension config --
        truesun_conf = config_dict.get("ClearSkiesTruesun", {})
        cams_api_key = str(truesun_conf.get("cams_api_key", "")).strip()
        fallback_aod700 = float(truesun_conf.get("fallback_aod700", 0.06))
        fetch_interval_hours = float(
            truesun_conf.get("aod_fetch_interval_hours", 12)
        )

        # -- Station coordinates from [Station] --
        stn = config_dict.get("Station", {})
        latitude = float(stn.get("latitude", 0))
        longitude = float(stn.get("longitude", 0))

        # weewx stores altitude as "value, unit" string like "40, foot"
        altitude_str = str(stn.get("altitude", "0, meter"))
        alt_parts = altitude_str.split(",")
        alt_val = float(alt_parts[0].strip())
        alt_unit = alt_parts[1].strip().lower() if len(alt_parts) > 1 else "meter"
        altitude_m = alt_val * 0.3048 if alt_unit in ("foot", "feet") else alt_val

        # -- Thread-safe AOD cache --
        self._aod_cache = _AODCache(fallback_aod700)

        # -- Create and register XType (PREPEND to run before StdWXXTypes) --
        self._xtype = ClearSkiesTruesunXType(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude_m,
            aod_cache=self._aod_cache,
        )
        weewx.xtypes.xtypes.insert(0, self._xtype)

        # -- Start CAMS AOD background fetch thread --
        self._cams_thread: _CAMSFetchThread | None = None
        if cams_api_key and cams_api_key != "REPLACE_ME":
            self._cams_thread = _CAMSFetchThread(
                api_key=cams_api_key,
                latitude=latitude,
                longitude=longitude,
                aod_cache=self._aod_cache,
                fetch_interval_hours=fetch_interval_hours,
            )
            self._cams_thread.start()
        else:
            logger.warning(
                "ClearSkiesTruesun: no cams_api_key configured — "
                "using fallback AOD700=%.3f "
                "(register at https://ads.atmosphere.copernicus.eu/)",
                fallback_aod700,
            )

        logger.info(
            "ClearSkiesTruesun: registered XType for maxSolarRad "
            "(lat=%.4f, lon=%.4f, alt=%.0f m, fallback_aod700=%.3f)",
            latitude,
            longitude,
            altitude_m,
            fallback_aod700,
        )

    def shutDown(self) -> None:  # noqa: N802 — weewx naming convention
        """Stop the CAMS thread and remove the XType from the registry."""
        if self._cams_thread is not None:
            self._cams_thread.stop()
            self._cams_thread.join(timeout=10)
            if self._cams_thread.is_alive():
                logger.warning(
                    "ClearSkiesTruesun: CAMS thread did not exit within 10 s"
                )

        try:
            weewx.xtypes.xtypes.remove(self._xtype)
        except ValueError:
            pass

        logger.info("ClearSkiesTruesun: shut down")
