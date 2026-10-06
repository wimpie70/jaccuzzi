"""Binary sensors exposing controller state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import CONF_JACUZZI_CLIMATE, CONF_PUMP_FAN, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status binary sensors."""
    async_add_entities(
        [
            JacuzziFlagBinarySensor(entry),
            JacuzziDemandBinarySensor(entry),
            JacuzziFlowBinarySensor(entry),
            JacuzziGeckoHeaterBinarySensor(entry),
        ]
    )


class _JacuzziBinarySensor(BinarySensorEntity):
    """Base: controller-backed binary sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind to the controller."""
        self._entry = entry
        self._controller = entry.runtime_data
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self._async_update
            )
        )

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()


class JacuzziFlagBinarySensor(_JacuzziBinarySensor):
    """On while the circulation pump was started by this integration."""

    _attr_translation_key = "pomp_door_ha"
    _attr_icon = "mdi:pump"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Set unique id."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_pomp_door_ha"

    @property
    def is_on(self) -> bool:
        """True when we started the pump (so we may stop it)."""
        return self._controller.pump_by_us


class JacuzziDemandBinarySensor(_JacuzziBinarySensor):
    """On while the Poolex has an unsatisfied heat demand."""

    _attr_translation_key = "warmtevraag"
    _attr_icon = "mdi:thermometer-plus"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Set unique id."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_warmtevraag"

    @property
    def is_on(self) -> bool:
        """True when tub temp is below the Poolex setpoint minus margin."""
        return self._controller.warmtevraag


class JacuzziFlowBinarySensor(_JacuzziBinarySensor):
    """Flow-proxy: de circulatiepomp is de enige flow-bron door de unit.

    De Poolex publiceert geen flow-sensor (flow-schakelaar zit niet in
    het DP-schema) — 'pomp aan' is daarom de beste benadering.
    """

    _attr_translation_key = "flow"
    _attr_icon = "mdi:waves"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Set unique id."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_flow"

    @property
    def is_on(self) -> bool | None:
        """True wanneer de circulatiepomp draait."""
        conf = {**self._entry.data, **self._entry.options}
        state = self.hass.states.get(conf[CONF_PUMP_FAN])
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return state.state == "on"


class JacuzziGeckoHeaterBinarySensor(_JacuzziBinarySensor):
    """Aan wanneer het elektrische element van de Gecko-pack stookt.

    Bron: hvac_action-attribuut van de Gecko climate-entity
    (gecko zet die uit heaterActivationStatus_ van de pack).
    """

    _attr_translation_key = "gecko_heater"
    _attr_icon = "mdi:heating-coil"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Set unique id."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_gecko_heater"

    @property
    def is_on(self) -> bool | None:
        """True wanneer hvac_action 'heating' is."""
        conf = {**self._entry.data, **self._entry.options}
        state = self.hass.states.get(conf[CONF_JACUZZI_CLIMATE])
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return state.attributes.get("hvac_action") == "heating"
