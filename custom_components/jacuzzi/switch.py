"""Switch platform: Poolex aan/uit (stelt de climate hvac_mode)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import CONF_POOLEX_CLIMATE, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Poolex on/off switch."""
    async_add_entities([JacuzziPoolexSwitch(entry)])


class JacuzziPoolexSwitch(SwitchEntity):
    """Aan/uit voor de warmtepomp — stelt hvac_mode van de climate."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_power"
    _attr_icon = "mdi:heat-pump"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_power"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    def _poolex_entity(self) -> str:
        conf = {**self._entry.data, **self._entry.options}
        return conf[CONF_POOLEX_CLIMATE]

    @property
    def is_on(self) -> bool | None:
        """Aan als de Poolex-climate niet op 'off' staat."""
        state = self.hass.states.get(self._poolex_entity())
        if state is None:
            return None
        return state.state != "off"

    async def async_turn_on(self, **kwargs) -> None:
        """Zet de Poolex op heat."""
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._poolex_entity(), "hvac_mode": "heat"},
            blocking=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Zet de Poolex uit."""
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._poolex_entity(), "hvac_mode": "off"},
            blocking=True,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe op controller-updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._async_update)
        )

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()
