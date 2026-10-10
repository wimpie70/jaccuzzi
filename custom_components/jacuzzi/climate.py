"""Climate entity: de kuip-thermostaat (gebruikersdoel).

Dit is de enige plek waar de gewenste kuip-temperatuur wordt gezet.
De Poolex-climate is puur de actuator: de controller schrijft daar
het setpoint naar de vloer (compressor-rem) of herstelt het doel.

- `temperature`         = gewenste kuip-temp (controller._heat_setpoint)
- `current_temperature` = de bron die de controller leidend vindt
                          (alleen geldige kuipmeting; nooit de inlaat)
- `hvac_mode` off       = warmtevraag uit; normaal Poolex op 'heat'
                          met laag setpoint voor standby. Ook bij een niet
                          bevestigde stop blijft de rem 15 °C, NOOIT Poolex off.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import DOMAIN, MAX_TUB_C

ATTR_SOURCE = "demand_bron"  # welke sensor leidend was (kuip/inlaat)
ATTR_SUPPRESSED = "rem_actief"  # rem-reden of 'uit'
ATTR_STATUS = "regelstatus"  # een leesbare regeltoestand
ATTR_RETOUR = "retour_marge"  # geleerde kuip-retour offset (K)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the tub-target climate entity."""
    async_add_entities([JacuzziTubClimate(entry)])


class JacuzziTubClimate(ClimateEntity, RestoreEntity):
    """Thermostaat voor de kuip — stuurt controller._heat_setpoint."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "tub_target"
    _attr_icon = "mdi:hot-tub"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = 20.0
    _attr_max_temp = MAX_TUB_C

    @property
    def target_temperature_step(self) -> float:
        """Kuip-doel is onafhankelijk van de actuator en diens afronding."""
        return 0.5

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry en de controller."""
        self._entry = entry
        self._controller = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_tub_target"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    async def async_added_to_hass(self) -> None:
        """Luister naar controller-updates; herstel laatste doel."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self._async_update
            )
        )
        # Doel + hvac-mode overleven een restart: anders valt alles
        # terug op de config-default (38 °C, vraag aan).
        if (last := await self.async_get_last_state()) is not None:
            # De geleerde K blijft diagnostiek, maar mag niet bij elke
            # options-wijziging of onderhoudsreload verloren gaan.
            self._controller.async_restore_retour_offset(last.attributes.get(ATTR_RETOUR))
            enabled = last.state == "heat"
            try:
                # Oude installs konden doelen >40 opslaan. Ongeldig herstel
                # mag startup niet laten mislukken met de actuator nog hoog.
                self._controller.async_set_target(float(last.attributes["temperature"]))
            except (ValueError, TypeError, KeyError):
                enabled = False
            self._controller.async_set_heating_enabled(enabled)

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()

    @property
    def current_temperature(self) -> float | None:
        """De temperatuur-bron die de controller leidend vindt."""
        return self._controller.demand_temp

    @property
    def target_temperature(self) -> float | None:
        """Het gewenste kuip-doel."""
        return self._controller.heat_setpoint

    @property
    def hvac_mode(self) -> HVACMode:
        """off = warmtevraag uit (Poolex zelf blijft op heat)."""
        return (
            HVACMode.HEAT
            if self._controller.heating_enabled
            else HVACMode.OFF
        )

    @property
    def hvac_action(self) -> HVACAction | None:
        """Wat er fysiek gebeurt."""
        if not self._controller.heating_enabled:
            return HVACAction.OFF
        if self._controller.compressor_on:
            return HVACAction.HEATING
        if self._controller.warmtevraag:
            return HVACAction.PREHEATING  # vraag, compressor nog uit
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict:
        """Welke bron de huidige temperatuur levert + rem-status."""
        return {
            ATTR_SOURCE: self._controller.demand_bron,
            ATTR_SUPPRESSED: self._controller.rem_reden,
            ATTR_STATUS: self._controller.regelstatus,
            ATTR_RETOUR: round(self._controller.retour_offset, 1),
        }

    async def async_set_temperature(self, **kwargs) -> None:
        """Nieuw kuip-doel, in halve graden; actuator-echo verandert dit nooit."""
        if (temp := kwargs.get("temperature")) is not None:
            step = self.target_temperature_step
            self._controller.async_set_target(
                round(round(temp / step) * step, 1)
            )
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Warmtevraag aan/uit — raakt de Poolex-hvac niet aan."""
        self._controller.async_set_heating_enabled(
            hvac_mode != HVACMode.OFF
        )
        self.async_write_ha_state()
