"""Installer for the ClearSkiesTruesun weewx extension.

Replaces weewx's built-in maxSolarRad (Ryan-Stolzenbach) with pvlib's
Simplified Solis clear-sky model, which includes diffuse radiation.

Install:  weectl extension install <path-to-this-directory>
Remove:   weectl extension uninstall clearskies_truesun
"""

from io import StringIO

import configobj
from weewx.extensioninstaller import ExtensionInstaller


def loader():
    return ClearSkiesTruesunInstaller()


CLEARSKIES_TRUESUN_CONFIG = """
[ClearSkiesTruesun]
    # CAMS API key for aerosol optical depth forecast.
    # Register at https://ads.atmosphere.copernicus.eu/
    cams_api_key = REPLACE_ME
    # Fallback AOD at 700 nm when CAMS is unavailable (0.06 = typical clean coastal)
    fallback_aod700 = 0.06
    # How often to refresh CAMS AOD forecast (hours)
    aod_fetch_interval_hours = 12
"""

clearskies_truesun_dict = configobj.ConfigObj(StringIO(CLEARSKIES_TRUESUN_CONFIG))


class ClearSkiesTruesunInstaller(ExtensionInstaller):
    def __init__(self):
        super().__init__(
            version="0.1.0",
            name="clearskies_truesun",
            description=(
                "Replaces weewx maxSolarRad with pvlib Simplified Solis "
                "clear-sky model."
            ),
            author="Clear Skies Contributors",
            author_email="",
            xtype_services="user.clearskies_truesun.ClearSkiesTruesunService",
            config=clearskies_truesun_dict,
            files=[
                ("bin/user", ["bin/user/clearskies_truesun.py"]),
            ],
        )
