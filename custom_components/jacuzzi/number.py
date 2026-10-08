"""Number entities: instelbare drempels/tijden bewerkbaar vanaf het dashboard.

Net als de time-entities: schrijft naar de config-entry options; de
update-listener reloadt de integratie zodat de nieuwe waarde geldt.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .const import (
    CONF_INLET_COMPENSATION,
    CONF_MIX_INTERVAL_MIN,
    CONF_MIX_PULSE_S,
    CONF_POOLEX_MAX_W,
    CONF_SOLAR_MIN_W,
    DEFAULT_INLET_COMPENSATION_K,
    DEFAULT_MIX_INTERVAL_MIN,
    DEFAULT_MIX_PULSE_S,
    DEFAULT_POOLEX_MAX_W,
    DEFAULT_SOLAR_MIN_W,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the option-backed number entities."""
    async_add_entities(
        [
            JacuzziOptionNumber(
                entry,
                key=CONF_SOLAR_MIN_W,
                default=DEFAULT_SOLAR_MIN_W,
                translation_key="solar_min_watts",
                icon="mdi:solar-power",
                min_v=0.0,
                max_v=10000.0,
                step=100.0,
                unit="W",
            ),
            JacuzziOptionNumber(
                entry,
                key=CONF_POOLEX_MAX_W,
                default=DEFAULT_POOLEX_MAX_W,
                translation_key="poolex_max_watts",
                icon="mdi:lightning-bolt",
                min_v=500.0,
                max_v=4000.0,
                step=50.0,
                unit="W",
            ),
            JacuzziOptionNumber(
                entry,
                key=CONF_MIX_INTERVAL_MIN,
                default=DEFAULT_MIX_INTERVAL_MIN,
                translation_key="mix_interval",
                icon="mdi:timer-cog-outline",
                min_v=5.0,
                max_v=120.0,
                step=5.0,
                unit="min",
            ),
            JacuzziOptionNumber(
                entry,
                key=CONF_INLET_COMPENSATION,
                default=DEFAULT_INLET_COMPENSATION_K,
                translation_key="inlet_compensation_k",
                icon="mdi:thermometer-lines",
                min_v=0.0,
                max_v=0.5,
                step=0.01,
                unit="K/K",
            ),
            JacuzziOptionNumber(
                entry,
                key=CONF_MIX_PULSE_S,
                default=DEFAULT_MIX_PULSE_S,
                translation_key="mix_pulse",
                icon="mdi:waves-arrow-up",
                min_v=10.0,
                max_v=300.0,
                step=10.0,
                unit="s",
            ),
        ]
    )


class JacuzziOptionNumber(NumberEntity):
    """Number dat rechtstreeks naar de config-entry options schrijft."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry: JacuzziConfigEntry,
        *,
        key: str,
        default: float,
        translation_key: str,
        icon: str,
        min_v: float,
        max_v: float,
        step: float,
        unit: str,
    ) -> None:
        """Bind to the config entry."""
        self._entry = entry
        self._key = key
        self._default = default
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_native_min_value = min_v
        self._attr_native_max_value = max_v
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def native_value(self) -> float:
        """Huidige waarde (options > data > default)."""
        return float(
            self._entry.options.get(
                self._key, self._entry.data.get(self._key, self._default)
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        """Sla de waarde op in options; de update-listener reloadt."""
        options = dict(self._entry.options)
        options[self._key] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=options)
