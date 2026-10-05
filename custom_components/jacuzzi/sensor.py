"""Sensor platform: mirror van de kuip-temperatuur uit de Gecko climate."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import CONF_JACUZZI_CLIMATE, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the tub-temperature mirror sensor."""
    async_add_entities([JacuzziTubTempSensor(entry)])


class JacuzziTubTempSensor(SensorEntity):
    """Mirror van climate.<gecko>.current_temperature als echte sensor.

    Nodig omdat climate-attributen niet in history-graphs/cards kunnen.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "tub_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tub_temperature"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """Huidige kuip-temperatuur uit het climate-attribuut."""
        conf = {**self._entry.data, **self._entry.options}
        state = self.hass.states.get(conf[CONF_JACUZZI_CLIMATE])
        if state is None:
            return None
        try:
            return float(state.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError):
            return None

    async def async_added_to_hass(self) -> None:
        """Subscribe op controller-updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._async_update)
        )

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()
