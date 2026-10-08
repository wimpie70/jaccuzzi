"""Button platform: eenmalige acties (reset warmteverlies-metingen)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up de reset-knoppen."""
    async_add_entities([JacuzziHeatLossResetButton(entry)])


class JacuzziHeatLossResetButton(ButtonEntity):
    """Wis de warmteverlies-metingen (samples + huidige W/K)."""

    _attr_has_entity_name = True
    _attr_translation_key = "heat_loss_reset"
    _attr_icon = "mdi:restart"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_heat_loss_reset"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    async def async_press(self) -> None:
        """Leeg de sample-lijst; sensor gaat terug naar 'onbekend'."""
        self._entry.runtime_data.heat_loss_samples.clear()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
