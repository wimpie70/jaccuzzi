"""Sensor platform: kuip-temperatuur mirror + energie-integratie (W -> kWh)."""

from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import (
    CONF_JACUZZI_CLIMATE,
    CONF_JACUZZI_POWER_SENSOR,
    CONF_POOLEX_POWER_SENSOR,
    DOMAIN,
)

ENERGY_SENSORS = (
    (CONF_POOLEX_POWER_SENSOR, "poolex_energy"),
    (CONF_JACUZZI_POWER_SENSOR, "jacuzzi_energy"),
)

# tuya-local entities van de Poolex (vaste id's in deze setup)
POOLEX_FAN_SENSOR = "sensor.pool_heat_pump_fan_speed"
POOLEX_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
# ventilator ~1000 rpm max -> als % plotten naast compressor duty cycle
POOLEX_FAN_MAX_RPM = 1000.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mirror sensor + configured energy sensors."""
    entities: list[SensorEntity] = [
        JacuzziTubTempSensor(entry),
        JacuzziPoolexFanSensor(entry),
        JacuzziPoolexFaultSensor(entry),
    ]
    conf = {**entry.data, **entry.options}
    for conf_key, translation_key in ENERGY_SENSORS:
        if conf.get(conf_key):
            entities.append(JacuzziEnergySensor(entry, conf_key, translation_key))
    async_add_entities(entities)


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


class JacuzziPoolexFanSensor(SensorEntity):
    """Poolex-ventilatorsnelheid als % van max (~1000 rpm).

    Zodat ventilator en compressor duty cycle op dezelfde 0-100-as in
    één history-graph kunnen.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_fan_speed"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:fan"
    _attr_suggested_display_precision = 0

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_fan_speed"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """rpm / 1000 * 100, afgekapt op 100."""
        state = self.hass.states.get(POOLEX_FAN_SENSOR)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return min(100.0, float(state.state) / POOLEX_FAN_MAX_RPM * 100)
        except ValueError:
            return None

    async def async_added_to_hass(self) -> None:
        """Volg de bron-rpm sensor."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [POOLEX_FAN_SENSOR], self._on_source
            )
        )

    @callback
    def _on_source(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()


class JacuzziPoolexFaultSensor(SensorEntity):
    """Poolex-foutstatus als 0/100-lijn voor de aan/uit-grafiek.

    binary_sensor.pool_heat_pump_problem geeft alleen aan/uit — als
    numerieke 0/100-sensor overlapt hij de compressor/ventilator-as.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_fault"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:alert-circle"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_fault"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """100 bij fault, 0 bij ok."""
        state = self.hass.states.get(POOLEX_PROBLEM_SENSOR)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return 100.0 if state.state == "on" else 0.0

    async def async_added_to_hass(self) -> None:
        """Volg de fault binary sensor."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [POOLEX_PROBLEM_SENSOR], self._on_source
            )
        )

    @callback
    def _on_source(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()


class JacuzziEnergySensor(RestoreSensor):
    """Integreert een gekozen vermogenssensor (W) naar energie (kWh).

    Links-Riemann integratie op state-changes; overleeft restarts via
    RestoreSensor. Bedoeld voor een smart plug die nog geïnstalleerd
    wordt — entity verschijnt zodra de bron in Configure is gekozen.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self, entry: JacuzziConfigEntry, conf_key: str, translation_key: str
    ) -> None:
        """Bind aan de config entry en bron-optie."""
        self._entry = entry
        self._conf_key = conf_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{conf_key}_energy"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        self._kwh = 0.0
        self._last_w: float | None = None
        self._last_ts = None

    async def async_added_to_hass(self) -> None:
        """Herstel teller en abboneer op de bron-sensor."""
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._kwh = float(last.native_value)
            except (TypeError, ValueError):
                pass
        self._attr_native_value = round(self._kwh, 3)
        conf = {**self._entry.data, **self._entry.options}
        src = conf.get(self._conf_key)
        if not src:
            return
        st = self.hass.states.get(src)
        if st is not None:
            try:
                self._last_w = float(st.state)
            except ValueError:
                pass
            self._last_ts = dt_util.utcnow()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [src], self._on_source)
        )

    @callback
    def _on_source(self, event: Event[EventStateChangedData]) -> None:
        """Integreer vermogen over tijd (links-Riemann)."""
        new = event.data.get("new_state")
        if new is None or new.state in ("unavailable", "unknown"):
            return
        try:
            watts = float(new.state)
        except ValueError:
            return
        now = dt_util.utcnow()
        if self._last_ts is not None and self._last_w is not None:
            dt_h = (now - self._last_ts).total_seconds() / 3600
            # gaten > 1 uur (HA down) niet meetellen
            if 0 < dt_h < 1:
                self._kwh += self._last_w * dt_h / 1000
        self._last_ts = now
        self._last_w = watts
        self._attr_native_value = round(self._kwh, 3)
        self.async_write_ha_state()
