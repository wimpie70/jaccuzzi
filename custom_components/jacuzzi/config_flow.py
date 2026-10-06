"""Config flow for Jacuzzi Poolex Control."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_COMPRESSOR_SENSOR,
    CONF_JACUZZI_CLIMATE,
    CONF_NOTIFY_SERVICE,
    CONF_PEAK_END,
    CONF_PEAK_START,
    CONF_POOLEX_CLIMATE,
    CONF_PROBLEM_SENSOR,
    CONF_PUMP_FAN,
    CONF_NORMAL_SETPOINT,
    CONF_PEAK_MODE,
    CONF_PEAK_SETPOINT,
    CONF_JACUZZI_POWER_SENSOR,
    CONF_POOLEX_POWER_SENSOR,
    CONF_SOLAR_MIN_W,
    CONF_SOLAR_SENSOR,
    CONF_TEMP_MARGIN,
    CONF_WATERCARE_NORMAL,
    CONF_WATERCARE_PEAK,
    CONF_WATERCARE_SELECT,
    DEFAULT_COMPRESSOR_SENSOR,
    DEFAULT_JACUZZI_CLIMATE,
    DEFAULT_NORMAL_SETPOINT,
    DEFAULT_NOTIFY_SERVICE,
    DEFAULT_PEAK_MODE,
    DEFAULT_PEAK_SETPOINT,
    DEFAULT_SOLAR_MIN_W,
    DEFAULT_PEAK_END,
    DEFAULT_PEAK_START,
    DEFAULT_POOLEX_CLIMATE,
    DEFAULT_PROBLEM_SENSOR,
    DEFAULT_PUMP_FAN,
    DEFAULT_TEMP_MARGIN,
    DEFAULT_WATERCARE_NORMAL,
    DEFAULT_WATERCARE_PEAK,
    DEFAULT_WATERCARE_SELECT,
    DOMAIN,
)

ENTITY_SELECTORS = {
    CONF_POOLEX_CLIMATE: "climate",
    CONF_JACUZZI_CLIMATE: "climate",
    CONF_PUMP_FAN: "fan",
    CONF_COMPRESSOR_SENSOR: "sensor",
    CONF_PROBLEM_SENSOR: "binary_sensor",
    CONF_WATERCARE_SELECT: "select",
}

DEFAULTS = {
    CONF_POOLEX_CLIMATE: DEFAULT_POOLEX_CLIMATE,
    CONF_JACUZZI_CLIMATE: DEFAULT_JACUZZI_CLIMATE,
    CONF_PUMP_FAN: DEFAULT_PUMP_FAN,
    CONF_COMPRESSOR_SENSOR: DEFAULT_COMPRESSOR_SENSOR,
    CONF_PROBLEM_SENSOR: DEFAULT_PROBLEM_SENSOR,
    CONF_WATERCARE_SELECT: DEFAULT_WATERCARE_SELECT,
    CONF_NOTIFY_SERVICE: DEFAULT_NOTIFY_SERVICE,
    CONF_PEAK_START: DEFAULT_PEAK_START,
    CONF_PEAK_END: DEFAULT_PEAK_END,
    CONF_WATERCARE_PEAK: DEFAULT_WATERCARE_PEAK,
    CONF_WATERCARE_NORMAL: DEFAULT_WATERCARE_NORMAL,
    CONF_TEMP_MARGIN: DEFAULT_TEMP_MARGIN,
    CONF_PEAK_MODE: DEFAULT_PEAK_MODE,
    CONF_PEAK_SETPOINT: DEFAULT_PEAK_SETPOINT,
    CONF_NORMAL_SETPOINT: DEFAULT_NORMAL_SETPOINT,
    CONF_SOLAR_MIN_W: DEFAULT_SOLAR_MIN_W,
}


def _schema(current: dict | None = None) -> vol.Schema:
    """Build the config/options schema; `current` overrides defaults."""
    current = current or {}
    data: dict = {}
    for key, domain in ENTITY_SELECTORS.items():
        data[
            vol.Required(key, default=current.get(key, DEFAULTS[key]))
        ] = selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))
    data[vol.Optional(
        CONF_NOTIFY_SERVICE, default=current.get(CONF_NOTIFY_SERVICE, DEFAULT_NOTIFY_SERVICE)
    )] = selector.TextSelector()
    data[vol.Required(
        CONF_PEAK_START, default=current.get(CONF_PEAK_START, DEFAULT_PEAK_START)
    )] = selector.TimeSelector()
    data[vol.Required(
        CONF_PEAK_END, default=current.get(CONF_PEAK_END, DEFAULT_PEAK_END)
    )] = selector.TimeSelector()
    data[vol.Optional(
        CONF_WATERCARE_PEAK, default=current.get(CONF_WATERCARE_PEAK, DEFAULT_WATERCARE_PEAK)
    )] = selector.TextSelector()
    data[vol.Optional(
        CONF_WATERCARE_NORMAL, default=current.get(CONF_WATERCARE_NORMAL, DEFAULT_WATERCARE_NORMAL)
    )] = selector.TextSelector()
    data[vol.Required(
        CONF_TEMP_MARGIN, default=current.get(CONF_TEMP_MARGIN, DEFAULT_TEMP_MARGIN)
    )] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=0.1, max=5.0, step=0.1, unit_of_measurement="°C")
    )
    data[vol.Required(
        CONF_PEAK_MODE, default=current.get(CONF_PEAK_MODE, DEFAULT_PEAK_MODE)
    )] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="setpoint", label="Laag setpoint (blijft aan)"),
                selector.SelectOptionDict(value="off", label="Helemaal uit (hvac_mode off)"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    data[vol.Required(
        CONF_PEAK_SETPOINT, default=current.get(CONF_PEAK_SETPOINT, DEFAULT_PEAK_SETPOINT)
    )] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=4.0, max=20.0, step=0.5, unit_of_measurement="°C")
    )
    data[vol.Required(
        CONF_NORMAL_SETPOINT, default=current.get(CONF_NORMAL_SETPOINT, DEFAULT_NORMAL_SETPOINT)
    )] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=20.0, max=42.0, step=0.5, unit_of_measurement="°C")
    )
    data[vol.Optional(
        CONF_SOLAR_SENSOR, default=current.get(CONF_SOLAR_SENSOR, "")
    )] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    )
    data[vol.Optional(
        CONF_SOLAR_MIN_W, default=current.get(CONF_SOLAR_MIN_W, DEFAULT_SOLAR_MIN_W)
    )] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=500, max=10000, step=100, unit_of_measurement="W")
    )
    data[vol.Optional(
        CONF_POOLEX_POWER_SENSOR, default=current.get(CONF_POOLEX_POWER_SENSOR, "")
    )] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    )
    data[vol.Optional(
        CONF_JACUZZI_POWER_SENSOR, default=current.get(CONF_JACUZZI_POWER_SENSOR, "")
    )] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    )
    return vol.Schema(data)


def _normalize_hhmm(val) -> str:
    """Normalise a TimeSelector result (str/dict/time) to 'HH:MM'."""
    if isinstance(val, str):
        return val[:5]
    if isinstance(val, dict):
        return f"{int(val['hour']):02d}:{int(val['minute']):02d}"
    return f"{val.hour:02d}:{val.minute:02d}"


def _normalize(user_input: dict) -> dict:
    """Normalise time fields in the submitted data."""
    for key in (CONF_PEAK_START, CONF_PEAK_END):
        user_input[key] = _normalize_hhmm(user_input[key])
    return user_input


class JacuzziConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Single-step setup: pick the entities, adjust timings."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Jacuzzi", data=_normalize(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return JacuzziOptionsFlow(config_entry)


class JacuzziOptionsFlow(OptionsFlow):
    """Edit the same fields post-install (self.config_entry is set by HA)."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(data=_normalize(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
