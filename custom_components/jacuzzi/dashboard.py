"""Registreer een Lovelace dashboard vanuit de integratie.

Gebruikt HA's interne lovelace-dashboards-collection. Dat is geen
stabiele API — vandaar defensief: elke fout wordt gelogd en de
integratie werkt gewoon door.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

URL_PATH = "jacuzzi"
TITLE = "Jacuzzi"
ICON = "mdi:hot-tub"

ENTITY_POOLEX = "climate.pool_heat_pump"
ENTITY_KUIP = "climate.jaccuzzi_thermostat_1"
ENTITY_POMP = "fan.jaccuzzi_waterfall"
ENTITY_COMPRESSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
ENTITY_PROBLEM = "binary_sensor.pool_heat_pump_problem"
ENTITY_WARMTEVRAAG = "binary_sensor.jacuzzi_heat_demand"
ENTITY_POMP_DOOR_HA = "binary_sensor.jacuzzi_pump_started_by_ha"
ENTITY_TUB_TEMP = "sensor.jacuzzi_tub_temperature"

DASHBOARD_CONFIG: dict[str, Any] = {
    "views": [
        {
            "title": TITLE,
            "path": "jacuzzi",
            "icon": ICON,
            "cards": [
                {
                    "type": "horizontal-stack",
                    "cards": [
                        {"type": "thermostat", "entity": ENTITY_KUIP, "name": "Kuip"},
                        {"type": "thermostat", "entity": ENTITY_POOLEX, "name": "Poolex"},
                    ],
                },
                {
                    "type": "glance",
                    "title": "Regelstatus",
                    "entities": [
                        {"entity": "switch.jacuzzi_poolex_power", "name": "Poolex"},
                        {"entity": ENTITY_WARMTEVRAAG, "name": "Warmtevraag"},
                        {"entity": ENTITY_POMP_DOOR_HA, "name": "Pomp door HA"},
                        {"entity": ENTITY_POMP, "name": "Circulatie"},
                        {"entity": ENTITY_COMPRESSOR, "name": "Compressor"},
                        {"entity": ENTITY_PROBLEM, "name": "Fault"},
                    ],
                },
                {
                    "type": "history-graph",
                    "title": "Temperaturen (6 uur)",
                    "hours_to_show": 6,
                    "entities": [
                        {"entity": ENTITY_TUB_TEMP, "name": "Kuip"},
                        {"entity": "sensor.pool_heat_pump_temperature", "name": "Wisselaar inlaat"},
                        {"entity": "sensor.pool_heat_pump_outflow_temperature", "name": "Uitlaat"},
                        {"entity": "sensor.pool_heat_pump_vent_temperature", "name": "Heetgas"},
                        {"entity": "sensor.pool_heat_pump_coil_temperature", "name": "Verdamper"},
                        {"entity": "sensor.pool_heat_pump_temperature_2", "name": "Buiten"},
                    ],
                },
                {
                    "type": "entities",
                    "title": "Poolex — sturing",
                    "entities": [
                        {"entity": "select.pool_heat_pump_auxiliary_heating", "name": "Bijstook (C4)"},
                        {"entity": "select.pool_heat_pump_circulation_pump", "name": "Pomprelais (C8)"},
                        {"entity": "switch.pool_heat_pump_defrost", "name": "Defrost (handmatig)"},
                        {"entity": "binary_sensor.pool_heat_pump_defrost", "name": "Defrost actief"},
                        {"entity": "number.pool_heat_pump_sampling_interval", "name": "Meetinterval (C9)"},
                    ],
                },
                {
                    "type": "entities",
                    "title": "Gecko — kuip",
                    "entities": [
                        {"entity": "select.jaccuzzi_watercare_mode", "name": "Watercare"},
                        {"entity": "fan.jaccuzzi_pump_1", "name": "Massagepomp 1"},
                        {"entity": "fan.jaccuzzi_pump_2", "name": "Massagepomp 2"},
                        {"entity": "binary_sensor.jaccuzzi_spa_status", "name": "Spa status"},
                    ],
                },
            ],
        }
    ]
}


async def async_setup_dashboard(hass: HomeAssistant) -> None:
    """Maak het Jacuzzi-dashboard aan als het nog niet bestaat."""
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            _LOGGER.warning("Lovelace niet geladen — dashboard overgeslagen")
            return

        dashboards = getattr(lovelace, "dashboards", None) or {}
        if URL_PATH in dashboards:
            _LOGGER.debug("Jacuzzi-dashboard bestaat al — niet overschreven")
            return

        collection = getattr(lovelace, "dashboards_collection", None)
        if collection is None:
            _LOGGER.warning("Geen dashboards_collection — dashboard overgeslagen")
            return

        await collection.async_create_item(
            {
                "url_path": URL_PATH,
                "title": TITLE,
                "icon": ICON,
                "show_in_sidebar": True,
                "require_admin": False,
                "mode": "storage",
            }
        )
        dashboard = lovelace.dashboards[URL_PATH]
        await dashboard.async_save(DASHBOARD_CONFIG)
        _LOGGER.info("Jacuzzi-dashboard aangemaakt (/%s)", URL_PATH)
    except Exception as err:  # noqa: BLE001 - interne API kan wijzigen
        _LOGGER.warning("Dashboard-registratie mislukt (niet fataal): %s", err)
