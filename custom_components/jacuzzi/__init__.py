"""Jacuzzi Poolex Control — relay-less jacuzzi heat pump control."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_INLET_COMPENSATION,
    CONF_MIX_ENABLED,
    CONF_MIX_INTERVAL_MIN,
    CONF_MIX_PULSE_S,
    CONF_NOTIFY_SERVICE,
    CONF_OVERSHOOT,
    CONF_POOLEX_ALWAYS_ON,
    CONF_SOLAR_MIN_W,
    CONF_SOLAR_SENSOR,
    CONF_TEMP_MARGIN,
    CONF_WATERCARE_NORMAL,
    CONF_WATERCARE_PEAK,
)
from .controller import JacuzziController
from .dashboard import async_setup_dashboard, teardown_dashboard

PLATFORMS = [
    "binary_sensor",
    "button",
    "climate",
    "number",
    "sensor",
    "switch",
    "time",
]

type JacuzziConfigEntry = ConfigEntry[JacuzziController]

# Opties die de controller elke evaluatie-tick uit ``conf`` leest. Een
# wijziging daarvan geldt bij de eerstvolgende evaluatie en mag de
# entry NIET reloaden: een reload remt een lopende run onmiddellijk
# op de vloer (Poolex-setpoint -> 15 °C) en verliest run-/leerstate.
# Entity-bindings, piek-tijden, normal_setpoint en maintenance blijven
# reloaden — die zijn bij constructie/subscription vastgelegd of
# sturen de shutdown-choreografie aan.
_LIVE_OPTION_KEYS = frozenset(
    {
        CONF_OVERSHOOT,
        CONF_TEMP_MARGIN,
        CONF_MIX_PULSE_S,
        CONF_MIX_INTERVAL_MIN,
        CONF_MIX_ENABLED,
        CONF_POOLEX_ALWAYS_ON,
        CONF_SOLAR_SENSOR,
        CONF_SOLAR_MIN_W,
        CONF_NOTIFY_SERVICE,
        CONF_WATERCARE_PEAK,
        CONF_WATERCARE_NORMAL,
        CONF_INLET_COMPENSATION,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: JacuzziConfigEntry) -> bool:
    """Set up the controller and entities from a config entry."""
    conf = {**entry.data, **entry.options}
    controller = JacuzziController(hass, conf)
    entry.runtime_data = controller
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Eerst het opgeslagen kuip-doel en hvac-mode herstellen; nooit met
    # het default-doel verwarmen tijdens startup of een options-reload.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    await async_setup_dashboard(hass)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: JacuzziConfigEntry
) -> None:
    """Reload when options change, zonder het dashboard-panel te verliezen."""
    controller = entry.runtime_data
    new_conf = {**entry.data, **entry.options}
    changed = {
        key
        for key in {*new_conf, *controller.conf}
        if controller.conf.get(key) != new_conf.get(key)
    }
    if changed <= _LIVE_OPTION_KEYS:
        # Alleen live leesbare tuning-opties (of een no-op schrijf):
        # conf in plaats bijwerken, de volgende evaluatie past ze toe.
        controller.apply_options(new_conf)
        return
    # Een dashboard-teardown tijdens reload laat de frontend naar het
    # home-dashboard springen; het panel komt meteen weer terug maar de
    # locatie niet. Markeer de reload zodat unload het panel laat staan.
    controller.mark_reloading()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JacuzziConfigEntry) -> bool:
    """Unload: stop listeners and remove entities."""
    controller = entry.runtime_data
    await controller.async_shutdown()
    if not controller._reloading:
        # Alleen bij echte verwijdering het panel afbreken; bij een
        # options-reload blijft het geregistreerd en blijft de UI staan.
        teardown_dashboard(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
