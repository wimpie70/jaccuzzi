"""Sensor platform: kuip-temperatuur mirror + energie-integratie (W -> kWh)."""

from __future__ import annotations

import time
from collections import deque

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
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
    CONF_POOLEX_CLIMATE,
    CONF_POOLEX_MAX_W,
    CONF_POOLEX_POWER_SENSOR,
    CONF_PUMP_FAN,
    DEFAULT_POOLEX_MAX_W,
    DOMAIN,
)

ENERGY_SENSORS = (
    (CONF_POOLEX_POWER_SENSOR, "poolex_energy"),
    (CONF_JACUZZI_POWER_SENSOR, "jacuzzi_energy"),
)

# tuya-local entities van de Poolex (vaste id's in deze setup)
POOLEX_FAN_SENSOR = "sensor.pool_heat_pump_fan_speed"
POOLEX_OUTLET_SENSOR = "sensor.pool_heat_pump_outflow_temperature"
POOLEX_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
POOLEX_COMPRESSOR_SENSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
# ventilator ~1000 rpm max -> als % plotten naast compressor duty cycle
POOLEX_FAN_MAX_RPM = 1000.0
# compressor duty cycle is 0-1500 ruw -> /15 = %
POOLEX_COMPRESSOR_MAX = 1500.0
# Kuip als calorimeter: kg water x 4.186 kJ/kgK -> J per K
TUB_MASS_KG = 1500.0
TUB_HEAT_CAPACITY = TUB_MASS_KG * 4186.0  # J/K
# Regressie-window: korter dan ~20 min is de 0.5 °C-quantisatie ruis,
# langer dan ~45 min dempt hij echte verandering te veel.
HEATING_WINDOW_S = 30 * 60
HEATING_MIN_S = 10 * 60  # minimaal zoveel data voor een betrouwbare helling
# Alleen sampelen ná een meng-puls (circulatie loopt door): tijdens de
# puls meet de inlaat de overgang; ~15-180 s erna is de bulk gemengd.
MIX_PUMPS = ("fan.jaccuzzi_pump_1", "fan.jaccuzzi_pump_2")
MIX_DELAY_S = 15
MIXED_VALID_S = 180


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mirror sensor + configured energy sensors."""
    heating = JacuzziHeatingPowerSensor(entry)
    power_est = JacuzziPoolexPowerEstimateSensor(entry)
    cop = JacuzziCopEstimateSensor(entry)
    cop.bind(heating, power_est)
    entities: list[SensorEntity] = [
        JacuzziTubTempSensor(entry),
        JacuzziPoolexInletTempSensor(entry),
        JacuzziPoolexDeltaTSensor(entry),
        power_est,
        heating,
        cop,
        JacuzziPoolexFanSensor(entry),
        JacuzziPoolexFaultSensor(entry),
        JacuzziPoolexCompressorSensor(entry),
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


class JacuzziPoolexInletTempSensor(SensorEntity):
    """Mirror van climate.<poolex>.current_temperature (inlaat-water).

    tuya-local publiceert DP16 alleen als climate-attribuut, niet als
    aparte sensor — zonder mirror kan hij niet in history-graphs.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_inlet_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_inlet_temperature"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """Huidige inlaat-temperatuur uit het climate-attribuut."""
        conf = {**self._entry.data, **self._entry.options}
        state = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
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


class JacuzziPoolexDeltaTSensor(SensorEntity):
    """Temperatuurstijging over de warmtewisselaar (uitlaat - inlaat).

    Inlaat = climate.<poolex>.current_temperature, uitlaat = DP25 via
    tuya-local. Samen met de flow geeft dit het afgegeven vermogen —
    eerste stap richting een COP-inschatting.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_delta_t"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_delta_t"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """uitlaat - inlaat; None als een van beide ontbreekt."""
        conf = {**self._entry.data, **self._entry.options}
        climate = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
        outlet = self.hass.states.get(POOLEX_OUTLET_SENSOR)
        if climate is None or outlet is None or outlet.state in (
            "unavailable",
            "unknown",
        ):
            return None
        try:
            return round(
                float(outlet.state)
                - float(climate.attributes["current_temperature"]),
                1,
            )
        except (TypeError, ValueError, KeyError):
            return None

    async def async_added_to_hass(self) -> None:
        """Volg uitlaat-sensor én inlaat (climate-attribuut)."""
        conf = {**self._entry.data, **self._entry.options}
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [POOLEX_OUTLET_SENSOR, conf[CONF_POOLEX_CLIMATE]],
                self._on_source,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._on_source)
        )

    @callback
    def _on_source(self, *_args) -> None:
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


class JacuzziPoolexCompressorSensor(SensorEntity):
    """Compressor duty cycle (0-1500 ruw) als % voor gezamenlijke as.

    De tuya-sensor heeft geen eenheid; history-graph splitst per eenheid,
    dus een afgeleide %-sensor deelt de grafiek met ventilator/fault.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_compressor"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:hvac"
    _attr_suggested_display_precision = 0

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_compressor"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """raw / 1500 * 100, afgekapt op 100."""
        state = self.hass.states.get(POOLEX_COMPRESSOR_SENSOR)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return min(100.0, float(state.state) / POOLEX_COMPRESSOR_MAX * 100)
        except ValueError:
            return None

    async def async_added_to_hass(self) -> None:
        """Volg de ruwe duty cycle sensor."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [POOLEX_COMPRESSOR_SENSOR], self._on_source
            )
        )

    @callback
    def _on_source(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()


class JacuzziPoolexPowerEstimateSensor(SensorEntity):
    """Geschat elektrisch vermogen: duty% × ingesteld max-vermogen.

    Geen echte meting — kalibreer number.jacuzzi_poolex_max_watts met
    een P1-stap of energiemeter. Te gebruiken als bron voor de
    poolex_energy-integratie (Configure → Poolex vermogenssensor).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_power_estimate"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_power_estimate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """duty_frac × max_w; 0 als de unit uit staat."""
        conf = {**self._entry.data, **self._entry.options}
        duty = self.hass.states.get(POOLEX_COMPRESSOR_SENSOR)
        if duty is None or duty.state in ("unavailable", "unknown"):
            return None
        try:
            frac = min(1.0, float(duty.state) / POOLEX_COMPRESSOR_MAX)
        except ValueError:
            return None
        max_w = float(conf.get(CONF_POOLEX_MAX_W, DEFAULT_POOLEX_MAX_W))
        return round(frac * max_w)

    async def async_added_to_hass(self) -> None:
        """Volg de duty-sensor."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [POOLEX_COMPRESSOR_SENSOR], self._on_source
            )
        )

    @callback
    def _on_source(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()


class JacuzziHeatingPowerSensor(SensorEntity):
    """Thermisch vermogen in de kuip, geschat uit de temperatuurhelling.

    De kuip is de calorimeter (1500 kg x 4.19 kJ/kgK). Als bron de
    Poolex-*inlaat* (DP16): tijdens circulatie is dat het gemengde
    kuip-water zelf — representatiever dan de Gecko-sensor die op een
    vaste plek bij de pack meet (stratificatie). Alleen sampelen als
    de pomp draait: stilstaand water in de wisselaar is géén kuip-temp.
    Least-squares over 30 min i.v.m. 0.1 °C-quantisatie + cloud-jitter.
    Negatief = warmteverlies i.p.v. opbrengst.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "heating_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:fire"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_heating_power"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }
        self._samples: deque[tuple[float, float]] = deque()
        self._kw: float | None = None
        self._last_mix = 0.0  # monotonic ts: massagepomp aan/liep net

    def _bulk_temp(self, now: float) -> float | None:
        """Inlaat-temp (DP16) rond een meng-puls bij lopende circulatie.

        Tussen pulsen aanzuigt de wisselaar mogelijk een gestratificeerde
        laag — dan is de inlaat géén bulk-temp meer.
        """
        conf = {**self._entry.data, **self._entry.options}
        mixing = False
        for ent in MIX_PUMPS:
            st = self.hass.states.get(ent)
            if st is not None and st.state == "on":
                mixing = True
                break
        if mixing:
            self._last_mix = now  # eindt pas als de puls stopt
        since_mix = now - self._last_mix
        if not (MIX_DELAY_S <= since_mix <= MIXED_VALID_S):
            return None
        pump = self.hass.states.get(conf[CONF_PUMP_FAN])
        if pump is None or pump.state != "on":
            return None
        state = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
        if state is None:
            return None
        try:
            return float(state.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError):
            return None

    @property
    def native_value(self) -> float | None:
        """Geleverd (of verloren) thermisch vermogen in kW."""
        return self._kw

    async def async_added_to_hass(self) -> None:
        """Sample op inlaat-updates, pomp-stand én controller-ticks."""
        conf = {**self._entry.data, **self._entry.options}
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [conf[CONF_POOLEX_CLIMATE], conf[CONF_PUMP_FAN]],
                self._on_sample,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._on_sample)
        )
        self._sample()

    @callback
    def _on_sample(self, *_args) -> None:
        self._sample()
        self.async_write_ha_state()

    def _sample(self) -> None:
        now = time.monotonic()
        temp = self._bulk_temp(now)
        if temp is not None:
            # Alleen toevoegen als de waarde veranderde — anders wordt
            # de regressie door dubbele punten vertekend.
            if not self._samples or self._samples[-1][1] != temp:
                self._samples.append((now, temp))
            while self._samples and self._samples[0][0] < now - HEATING_WINDOW_S:
                self._samples.popleft()
        self._kw = self._slope_kw(now)

    def _slope_kw(self, now: float) -> float | None:
        pts = list(self._samples)
        if len(pts) < 4 or pts[-1][0] - pts[0][0] < HEATING_MIN_S:
            return None
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        n = len(pts)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in pts)
        denom = n * sxx - sx * sx
        if denom <= 0:
            return None
        slope = (n * sxy - sx * sy) / denom  # K/s
        return round(slope * TUB_HEAT_CAPACITY / 1000.0, 2)


class JacuzziCopEstimateSensor(SensorEntity):
    """Grove COP: thermisch vermogen / elektrisch vermogen (beide geschat).

    Alleen zinvol als de compressor draait en de kuip echt opwarmt;
    anders None. Vergelijkbare schatting aan beide kanten, dus vooral
    als trend-indicator gebruiken.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "cop_estimate"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:gauge"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cop_estimate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }
        self._heating: JacuzziHeatingPowerSensor | None = None
        self._power: JacuzziPoolexPowerEstimateSensor | None = None

    def bind(
        self,
        heating: JacuzziHeatingPowerSensor,
        power: JacuzziPoolexPowerEstimateSensor,
    ) -> None:
        """Koppel de bron-sensoren (aangeroepen in async_setup_entry)."""
        self._heating = heating
        self._power = power

    @property
    def native_value(self) -> float | None:
        """COP-schatting, None als niet berekenbaar."""
        if (
            self._heating is None
            or self._power is None
            or self._heating.native_value is None
            or self._power.native_value is None
            or self._power.native_value < 100.0
            or self._heating.native_value <= 0.0
        ):
            return None
        return round(
            self._heating.native_value / (self._power.native_value / 1000.0),
            1,
        )

    async def async_added_to_hass(self) -> None:
        """Herbereken op elke controller-tick."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._on_update)
        )

    @callback
    def _on_update(self, *_args) -> None:
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
