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
# Sampelen zolang compressor stookt + circulatie draait — de inlaat is
# dan continu het gemengde bulk-water. Buffer wissen als de stook
# stopt: elke cyclus krijgt zijn eigen schone helling.


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mirror sensor + configured energy sensors."""
    heating = JacuzziHeatingPowerSensor(entry)
    power_est = JacuzziPoolexPowerEstimateSensor(entry)
    entities: list[SensorEntity] = [
        JacuzziTubTempSensor(entry),
        JacuzziPoolexInletTempSensor(entry),
        JacuzziPoolexDeltaTSensor(entry),
        power_est,
        heating,
        JacuzziCopEstimateSensor(entry),
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

    def _heating_conditions(self) -> bool:
        """Compressor stookt én circulatie draait."""
        conf = {**self._entry.data, **self._entry.options}
        pump = self.hass.states.get(conf[CONF_PUMP_FAN])
        if pump is None or pump.state != "on":
            return False
        compressor = self.hass.states.get(POOLEX_COMPRESSOR_SENSOR)
        try:
            return float(compressor.state) > 0
        except (TypeError, ValueError, AttributeError):
            return False

    def _bulk_temp(self) -> float | None:
        """Inlaat-temp (DP16) — tijdens circulatie het gemengde bulk."""
        conf = {**self._entry.data, **self._entry.options}
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
        if not self._heating_conditions():
            # Stook stopt -> cyclus voorbij; schone helling bij de
            # volgende run i.p.v. overgang te mengen.
            self._samples.clear()
            self._kw = None
            return
        temp = self._bulk_temp()
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
    """COP per stook-cyclus: ΔT bulk-water / geïntegreerd elektrisch verbruik.

    Alleen post-mix-metingen tellen als bulk-temp: de massagepompen
    (niet de circulatie) mengen de kuip echt. Tijdens een cyclus:
    eerste post-mix-venster -> T_start, elk volgend venster -> T_eind,
    Wh_est loopt door. Bij cyclus-einde: COP = ΔT*Q / ΔWh.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "cop_estimate"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:gauge"

    MIX_PUMPS = ("fan.jaccuzzi_pump_1", "fan.jaccuzzi_pump_2")
    MIX_DELAY_S = 15      # na einde puls: overgang voorbij
    MIX_WINDOW_S = 180    # daarna geldt inlaat als bulk

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cop_estimate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }
        self._cycle = False
        self._cum_wh = 0.0          # geïntegreerde Wh deze cyclus
        self._last_ts: float | None = None
        self._last_w: float | None = None
        self._first_temp: float | None = None   # T_start (post-mix)
        self._first_wh: float | None = None     # Wh-stand bij T_start
        self._last_temp: float | None = None    # T_eind (post-mix)
        self._last_wh: float | None = None      # Wh-stand bij T_eind
        self._mix_active = False
        self._mix_end = 0.0
        self._cop: float | None = None
        self._cycle_kwh_th: float | None = None
        self._cycle_kwh_el: float | None = None
        self._cycle_dt: float | None = None

    @property
    def native_value(self) -> float | None:
        """COP van de laatst afgeronde stook-cyclus."""
        return self._cop

    @property
    def extra_state_attributes(self) -> dict:
        """De getallen achter de COP voor verificatie."""
        return {
            "delta_t_k": self._cycle_dt,
            "kwh_thermisch": self._cycle_kwh_th,
            "kwh_elektrisch": self._cycle_kwh_el,
        }

    async def async_added_to_hass(self) -> None:
        """Volg alle bronnen + controller-ticks voor de integratie."""
        conf = {**self._entry.data, **self._entry.options}
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [
                    POOLEX_COMPRESSOR_SENSOR,
                    conf[CONF_POOLEX_CLIMATE],
                    conf[CONF_PUMP_FAN],
                    *self.MIX_PUMPS,
                ],
                self._on_event,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._on_event)
        )

    @callback
    def _on_event(self, *_args) -> None:
        self._tick()
        self.async_write_ha_state()

    # --- helpers -----------------------------------------------------

    def _conf(self) -> dict:
        return {**self._entry.data, **self._entry.options}

    def _power_w(self, conf: dict) -> float | None:
        st = self.hass.states.get(POOLEX_COMPRESSOR_SENSOR)
        if st is None:
            return None
        try:
            frac = min(1.0, float(st.state) / POOLEX_COMPRESSOR_MAX)
        except ValueError:
            return None
        return frac * float(conf.get(CONF_POOLEX_MAX_W, DEFAULT_POOLEX_MAX_W))

    def _inlet_temp(self, conf: dict) -> float | None:
        st = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
        if st is None:
            return None
        try:
            return float(st.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError):
            return None

    def _conditions(self, conf: dict) -> bool:
        """Compressor stookt én circulatie draait."""
        pump = self.hass.states.get(conf[CONF_PUMP_FAN])
        w = self._power_w(conf)
        return pump is not None and pump.state == "on" and (
            w is not None and w > 0
        )

    def _mixing(self) -> bool:
        for ent in self.MIX_PUMPS:
            st = self.hass.states.get(ent)
            if st is not None and st.state == "on":
                return True
        return False

    # --- cyclus-state-machine ----------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        conf = self._conf()

        # Wh integreren (links-Riemann) zolang de cyclus loopt
        if self._cycle and self._last_w is not None and self._last_ts is not None:
            dt_h = (now - self._last_ts) / 3600.0
            if 0 < dt_h < 1:
                self._cum_wh += self._last_w * dt_h
        self._last_ts = now
        self._last_w = self._power_w(conf)

        # Massagepomp: on -> off markeert einde van een meng-puls
        mixing = self._mixing()
        if self._mix_active and not mixing:
            self._mix_end = now
        self._mix_active = mixing

        heating = self._conditions(conf)
        if heating and not self._cycle:
            # cyclus-start: buffer/integratie op nul
            self._cycle = True
            self._cum_wh = 0.0
            self._first_temp = self._first_wh = None
            self._last_temp = self._last_wh = None
        elif not heating and self._cycle:
            self._cycle = False
            self._finalize()

        # Post-mix-venster: 15-180 s na einde van een meng-puls
        in_window = (
            self._cycle
            and not mixing
            and self.MIX_DELAY_S <= now - self._mix_end <= self.MIX_WINDOW_S
        )
        if in_window:
            temp = self._inlet_temp(conf)
            if temp is not None:
                if self._first_temp is None:
                    self._first_temp = temp
                    self._first_wh = self._cum_wh
                else:
                    self._last_temp = temp
                    self._last_wh = self._cum_wh

    def _finalize(self) -> None:
        """Cyclus afgelopen — COP uitrekenen uit de markers."""
        if (
            self._first_temp is None
            or self._last_temp is None
            or self._first_wh is None
            or self._last_wh is None
        ):
            return
        dt_k = self._last_temp - self._first_temp
        d_wh = self._last_wh - self._first_wh
        if d_wh <= 10 or dt_k <= 0:  # min 10 Wh + positieve stijging
            return
        kwh_th = dt_k * TUB_HEAT_CAPACITY / 3.6e6
        kwh_el = d_wh / 1000.0
        self._cop = round(kwh_th / kwh_el, 1)
        self._cycle_dt = round(dt_k, 1)
        self._cycle_kwh_th = round(kwh_th, 2)
        self._cycle_kwh_el = round(kwh_el, 3)


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
