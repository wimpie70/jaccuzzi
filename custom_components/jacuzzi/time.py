"""Time entities: piekblokkade-tijden bewerkbaar vanaf het dashboard.

Schrijven naar deze entity werkt de config-entry options bij; de
update-listener reloadt de integratie zodat de nieuwe tijden meteen
gelden. Dezelfde waarden als in Configure.
"""

from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .const import (
    CONF_PEAK_END,
    CONF_PEAK_START,
    DEFAULT_PEAK_END,
    DEFAULT_PEAK_START,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the peak-window time entities."""
    async_add_entities(
        [
            JacuzziPeakTime(
                entry, CONF_PEAK_START, "peak_start", DEFAULT_PEAK_START, "mdi:clock-start"
            ),
            JacuzziPeakTime(
                entry, CONF_PEAK_END, "peak_end", DEFAULT_PEAK_END, "mdi:clock-end"
            ),
        ]
    )


class JacuzziPeakTime(TimeEntity):
    """Editable time entity bound to a config-entry option."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: JacuzziConfigEntry,
        conf_key: str,
        translation_key: str,
        default: str,
        icon: str,
    ) -> None:
        """Bind to the config entry."""
        self._entry = entry
        self._conf_key = conf_key
        self._default = default
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{conf_key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def native_value(self) -> dt_time:
        """Huidige ingestelde tijd (options > data > default)."""
        raw = self._entry.options.get(
            self._conf_key, self._entry.data.get(self._conf_key, self._default)
        )
        hour, minute = str(raw).split(":")
        return dt_time(int(hour), int(minute))

    async def async_set_value(self, value: dt_time) -> None:
        """Sla de tijd op in options; de update-listener reloadt."""
        options = dict(self._entry.options)
        options[self._conf_key] = value.strftime("%H:%M")
        self.hass.config_entries.async_update_entry(self._entry, options=options)
