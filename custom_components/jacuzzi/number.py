"""Number entities: PV-overschot drempel bewerkbaar vanaf het dashboard.

Net als de time-entities: schrijft naar de config-entry options; de
update-listener reloadt de integratie zodat de nieuwe drempel geldt.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .const import CONF_SOLAR_MIN_W, DEFAULT_SOLAR_MIN_W, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PV surplus threshold number."""
    async_add_entities([JacuzziSolarMinNumber(entry)])


class JacuzziSolarMinNumber(NumberEntity):
    """Drempel (W) voor vervroegd piek-einde op PV-overschot."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "solar_min_watts"
    _attr_icon = "mdi:solar-power"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 10000.0
    _attr_native_step = 100.0
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "W"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind to the config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{CONF_SOLAR_MIN_W}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def native_value(self) -> float:
        """Huidige drempel (options > data > default)."""
        return float(
            self._entry.options.get(
                CONF_SOLAR_MIN_W,
                self._entry.data.get(CONF_SOLAR_MIN_W, DEFAULT_SOLAR_MIN_W),
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        """Sla de drempel op in options; de update-listener reloadt."""
        options = dict(self._entry.options)
        options[CONF_SOLAR_MIN_W] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=options)
