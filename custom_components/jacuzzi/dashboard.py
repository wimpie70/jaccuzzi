"""Registreer het Jacuzzi Lovelace-dashboard als yaml-mode panel.

Storage-dashboards zijn alleen via de interne DashboardsCollection te
maken, en die is in recente HA-versies niet meer bereikbaar via
hass.data. Daarom registreren we een yaml-dashboard zoals
`lovelace: dashboards:` in configuration.yaml dat ook zou doen: een
LovelaceYAML-object in lovelace.dashboards + een built-in panel.

Voordeel: het dashboard updatet automatisch mee met elke release (het
yaml-bestand zit in de integratie). Nadeel: niet bewerkbaar in de UI.
Alles is defensief — bij een gewijzigde interne API loggen we een
waarschuwing en werkt de integratie gewoon door.
"""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

URL_PATH = "jacuzzi"
TITLE = "Jacuzzi"
ICON = "mdi:hot-tub"
YAML_FILE = os.path.join(os.path.dirname(__file__), "dashboard.yaml")


def _dashboards(lovelace) -> dict | None:
    """LovelaceData object (nieuw) of dict (oud) — vind de dashboards-map."""
    dashboards = getattr(lovelace, "dashboards", None)
    if dashboards is None and isinstance(lovelace, dict):
        dashboards = lovelace.get("dashboards")
    return dashboards if isinstance(dashboards, dict) else None


async def async_setup_dashboard(hass: HomeAssistant) -> None:
    """Maak het Jacuzzi yaml-dashboard aan als het nog niet bestaat."""
    try:
        lovelace = hass.data.get("lovelace")
        dashboards = _dashboards(lovelace) if lovelace is not None else None
        if dashboards is None:
            if not hass.is_running:
                # lovelace laadt mogelijk later tijdens dezelfde start —
                # eenmalig opnieuw proberen zodra HA draait
                hass.bus.async_listen_once(
                    "homeassistant_started",
                    lambda _e: hass.async_create_task(async_setup_dashboard(hass)),
                )
                _LOGGER.debug("Lovelace nog niet geladen — retry bij HA-started")
            else:
                _LOGGER.warning("Lovelace niet geladen — dashboard overgeslagen")
            return
        if URL_PATH in dashboards:
            return

        from homeassistant.components import frontend
        from homeassistant.components.lovelace.dashboard import LovelaceYAML

        # absolute filename wordt door hass.config.path() ongemoeid gelaten.
        # Volledige conf meegeven: de dashboards-lijst (Settings ->
        # Dashboards) en de sidebar lezen title/icon/show_in_sidebar hieruit
        yaml_conf = {
            "mode": "yaml",
            "title": TITLE,
            "icon": ICON,
            "show_in_sidebar": True,
            "require_admin": False,
            "filename": YAML_FILE,
        }
        yaml_dash = LovelaceYAML(hass, URL_PATH, yaml_conf)
        dashboards[URL_PATH] = yaml_dash
        yaml_dashboards = getattr(lovelace, "yaml_dashboards", None)
        if isinstance(yaml_dashboards, dict):
            yaml_dashboards[URL_PATH] = yaml_dash
        elif isinstance(lovelace, dict) and isinstance(
            lovelace.get("yaml_dashboards"), dict
        ):
            lovelace["yaml_dashboards"][URL_PATH] = yaml_dash

        frontend.async_register_built_in_panel(
            hass,
            "lovelace",
            sidebar_title=TITLE,
            sidebar_icon=ICON,
            frontend_url_path=URL_PATH,
            config={"mode": "yaml"},
            require_admin=False,
            update=True,
        )
        _LOGGER.info("Jacuzzi-dashboard geregistreerd (/%s)", URL_PATH)
    except Exception as err:  # noqa: BLE001 - interne API kan wijzigen
        _LOGGER.warning("Dashboard-registratie mislukt (niet fataal): %s", err)


def teardown_dashboard(hass: HomeAssistant) -> None:
    """Verwijder het panel en de dashboard-registratie (unload)."""
    try:
        lovelace = hass.data.get("lovelace")
        dashboards = _dashboards(lovelace) if lovelace is not None else None
        if dashboards is not None:
            dashboards.pop(URL_PATH, None)
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, URL_PATH)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Dashboard-teardown overgeslagen: %s", err)
