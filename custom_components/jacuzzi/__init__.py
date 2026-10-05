"""Jacuzzi Poolex Control — relay-less jacuzzi heat pump control."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .controller import JacuzziController
from .dashboard import async_setup_dashboard

PLATFORMS = ["binary_sensor", "sensor", "switch"]

type JacuzziConfigEntry = ConfigEntry[JacuzziController]


async def async_setup_entry(hass: HomeAssistant, entry: JacuzziConfigEntry) -> bool:
    """Set up the controller and entities from a config entry."""
    conf = {**entry.data, **entry.options}
    controller = JacuzziController(hass, conf)
    await controller.async_start()
    entry.runtime_data = controller
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_dashboard(hass)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: JacuzziConfigEntry
) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JacuzziConfigEntry) -> bool:
    """Unload: stop listeners and remove entities."""
    entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
