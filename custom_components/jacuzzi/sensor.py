"""Sensor platform: kuip-temperatuur mirror + energie-integratie (W -> kWh)."""

from __future__ import annotations

import time
from collections import deque
from datetime import timedelta

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
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData
from homeassistant.util import dt as dt_util

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE, _parse_hhmm
from .const import (
    CONF_JACUZZI_CLIMATE,
    CONF_JACUZZI_POWER_SENSOR,
    CONF_OVERSHOOT,
    CONF_PEAK_END,
    CONF_PEAK_START,
    CONF_POOLEX_CLIMATE,
    CONF_POOLEX_MAX_W,
    CONF_POOLEX_POWER_SENSOR,
    CONF_PUMP_FAN,
    CONF_TEMP_MARGIN,
    DEFAULT_AMBIENT_SENSOR,
    DEFAULT_OVERSHOOT_K,
    DEFAULT_PEAK_END,
    DEFAULT_PEAK_START,
    DEFAULT_POOLEX_MAX_W,
    DEFAULT_TEMP_MARGIN,
    DOMAIN,
    MAX_TUB_C,
    POOLEX_OUTLET_SENSOR,
)

ENERGY_SENSORS = (
    (CONF_POOLEX_POWER_SENSOR, "poolex_energy"),
    (CONF_JACUZZI_POWER_SENSOR, "jacuzzi_energy"),
)

# tuya-local entities van de Poolex (vaste id's in deze setup)
POOLEX_FAN_SENSOR = "sensor.pool_heat_pump_fan_speed"
POOLEX_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
POOLEX_COMPRESSOR_SENSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
# ventilator ~1000 rpm max -> als % plotten naast compressor duty cycle
POOLEX_FAN_MAX_RPM = 1000.0
# compressor duty cycle is een percentage 0-100 (waargenomen 16-76
# tijdens stoken, exact 0 idle). Eerst aangenomen 0-1500 ruw, maar dat
# gaf ~75 W bij duty 70 terwijl de compressor echt ~1 kW trok -> COP
# zou onmogelijk hoog uitvallen. Klopt de schaal toch niet, dan toont
# power_estimate het meteen (te hoog/laag) -> kalibratie blijft aan de
# gebruiker via poolex_max_watts.
POOLEX_COMPRESSOR_MAX = 100.0
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
        JacuzziHeatLossSensor(entry),
        power_est,
        heating,
        JacuzziCopEstimateSensor(entry),
        JacuzziPoolexFanSensor(entry),
        JacuzziPoolexFaultSensor(entry),
        JacuzziPoolexCompressorSensor(entry),
        JacuzziRegelstatusSensor(entry),
        JacuzziMeetbronSensor(entry),
        JacuzziRetourMargeSensor(entry),
        JacuzziOvershootAdviceSensor(entry),
        JacuzziPeakEndForecastSensor(entry),
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


class JacuzziHeatLossSensor(RestoreSensor):
    """Passief warmteverlies van de kuip, W/K (genormaliseerd op ΔT).

    De controller meet afkoeling tijdens stille periodes (alle pompen +
    compressor uit, >=3 u, >=0.5 K daling). W/K = afkoelsnelheid *
    watermassa / (kuip - buiten) — schaalt mee naar winterse nachten.
    Dek open/dicht verschijnt als spreiding tussen de samples.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "heat_loss"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W/K"
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:heat-wave"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_heat_loss"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def native_value(self) -> float | None:
        """W/K van de laatste voltooide meting."""
        if not self._entry.runtime_data.heat_loss_samples:
            return None
        return self._entry.runtime_data.heat_loss_samples[-1]["w_per_k"]

    @property
    def extra_state_attributes(self) -> dict:
        """De laatste meting + het sample-spoor voor spreiding."""
        samples = self._entry.runtime_data.heat_loss_samples
        if not samples:
            return {"metingen": 0}
        last = samples[-1]
        return {
            "metingen": len(samples),
            "gemeten_op": last["at"],
            "afkoeling_k": last["drop_k"],
            "afkoeling_k_per_u": last["rate_k_h"],
            "delta_t_kuip_buiten": last["delta_t_k"],
            "buiten_gemiddeld_c": last["ambient_mean_c"],
            "duur_u": last["hours"],
            "samples": samples,
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """De samples-lijsten zelf restoren (buiten de state om)."""
        return RestoredExtraData(
            {
                "samples": self._entry.runtime_data.heat_loss_samples,
                "gain_samples": self._entry.runtime_data.heat_gain_samples,
            }
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe op controller-updates + herstel de samples."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._async_update)
        )
        if (last := await self.async_get_last_extra_data()) is not None:
            stored = last.as_dict()
            heat_loss = stored.get("samples")
            if heat_loss and not self._entry.runtime_data.heat_loss_samples:
                self._entry.runtime_data.heat_loss_samples = list(heat_loss)
            gain = stored.get("gain_samples")
            if gain and not self._entry.runtime_data.heat_gain_samples:
                self._entry.runtime_data.heat_gain_samples = list(gain)

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()


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


def _hours_until(conf: dict, key: str, default: str) -> float:
    """Uren tot het eerstvolgende HH:MM-moment uit de config."""
    hh, mm = _parse_hhmm(conf.get(key, default))
    now = dt_util.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 3600


class _ControllerSensor(SensorEntity):
    """Base: sensor die direct een controller-waarde exposeert.

    Anders dan een attribuut op climate: echte sensoren zijn niet
    'editable' in kaarten, tonen netjes de waarde, en hebben history.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: JacuzziConfigEntry, key: str) -> None:
        """Bind aan de controller."""
        self._entry = entry
        self._controller = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe op controller-updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._async_update)
        )

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()

    def _tub_ambient(self) -> tuple[float | None, float | None]:
        """Kuip- (Gecko) en buitentemp, of None als een ontbreekt."""
        conf = {**self._entry.data, **self._entry.options}
        gecko = self.hass.states.get(conf[CONF_JACUZZI_CLIMATE])
        amb_st = self.hass.states.get(DEFAULT_AMBIENT_SENSOR)
        try:
            tub = float(gecko.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError, AttributeError):
            tub = None
        try:
            amb = float(amb_st.state)
        except (TypeError, ValueError, AttributeError):
            amb = None
        return tub, amb

    @staticmethod
    def _cooling_rate_k_h(samples: list[dict], tub: float, amb: float) -> float:
        """Verwacht passief verlies in K/h bij de huidige kuip-buiten ΔT.

        De gemeten W/K van de laatste stille periode schaalt mee naar
        elk seizoens-ΔT: rate = W/K x ΔT x 3600 / (kg x J/kgK).
        """
        delta_t = tub - amb
        if delta_t <= 0:
            return 0.0
        return samples[-1]["w_per_k"] * delta_t * 3600 / TUB_HEAT_CAPACITY


class JacuzziRegelstatusSensor(_ControllerSensor):
    """Eén leesbare regeltoestand:

    piekblokkade | meng-check | rem (reden) | vraag (bron) | idle.
    """

    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        super().__init__(entry, "regelstatus")

    @property
    def native_value(self) -> str | None:
        """Huidige regeltoestand."""
        return self._controller.regelstatus


class JacuzziMeetbronSensor(_ControllerSensor):
    """Leidende bron: uitsluitend kuip, met meng-check als verificatiefase."""

    _attr_icon = "mdi:thermometer-probe"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        super().__init__(entry, "meetbron")

    @property
    def native_value(self) -> str | None:
        """Leidende bron of leeg tijdens idle."""
        return self._controller.demand_bron or None


class JacuzziRetourMargeSensor(_ControllerSensor):
    """Meetafwijking van de kuipsensor: EMA van stopmeting minus mengmeting.

    Hoeveel de kuipsensor bij een stop boven de gemengde bulk las
    (pocket/stratificatie). Puur diagnostiek — geen regelcorrectie.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:shield-thermometer"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        super().__init__(entry, "meetafwijking")

    @property
    def native_value(self) -> float:
        """Geleerde meng-offset in K; verhoogt geen temperatuurgrens."""
        return round(self._controller.retour_offset, 1)

    @property
    def extra_state_attributes(self) -> dict:
        """Hoeveel eigen meng-metingen in deze offset zitten."""
        return {"leermetingen_sinds_herstart": self._controller.retour_samples}


class JacuzziOvershootAdviceSensor(_ControllerSensor):
    """Advies voor de doorstook-instelling; puur informatief.

    De vraag-cyclus (vraag -> stoken -> stop -> afkoelen) wordt met
    de huidige doorstook gesimuleerd tot de blokkade: zo valt de
    echte laatste stop. Advies = gemeten W/K x huidige ΔT x uren
    vanaf die laatste stop tot piek-einde — precies de doorstook
    die die stop had moeten halen. Zonder geleerde stooksnelheid
    valt hij terug op het verlies over de blokkade alleen.
    Begrensd zodat doel + advies nooit boven MAX_TUB_C uitkomt.
    Zonder afkoelingsmeting is er geen advies.
    De gebruiker beslist zelf of hij 'm in 'Doorstook' overneemt.
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:arrow-expand-up"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        super().__init__(entry, "advies_doorstook")

    def _advies(self) -> dict | None:
        """Reken het advies uit, of None als inputs ontbreken."""
        samples = self._controller.heat_loss_samples
        if not samples:
            return None
        conf = {**self._entry.data, **self._entry.options}
        tub, amb = self._tub_ambient()
        if tub is None or amb is None:
            return None
        s_h, s_m = _parse_hhmm(conf.get(CONF_PEAK_START, DEFAULT_PEAK_START))
        e_h, e_m = _parse_hhmm(conf.get(CONF_PEAK_END, DEFAULT_PEAK_END))
        block_h = ((e_h * 60 + e_m) - (s_h * 60 + s_m)) / 60
        if block_h < 0:  # blokkade loopt over middernacht
            block_h += 24
        now = dt_util.now()
        start = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
        if start > now:
            # start ligt vandaag nog voor ons; we kunnen wél in een
            # gisteren gestarte blokkade zitten (eindigt na middernacht)
            start -= timedelta(days=1)
        in_block = now < start + timedelta(hours=block_h)
        if not in_block:
            start += timedelta(days=1)
        end = start + timedelta(hours=block_h)
        hours_end = (end - now).total_seconds() / 3600
        hours_start = max((start - now).total_seconds() / 3600, 0.0)
        rate = self._cooling_rate_k_h(samples, tub, amb)
        doel = self._controller.heat_setpoint
        try:
            marge = float(conf.get(CONF_TEMP_MARGIN, DEFAULT_TEMP_MARGIN))
        except (TypeError, ValueError):
            marge = DEFAULT_TEMP_MARGIN
        try:
            d_cur = float(conf.get(CONF_OVERSHOOT, DEFAULT_OVERSHOOT_K))
        except (TypeError, ValueError):
            d_cur = DEFAULT_OVERSHOOT_K
        gain_samples = self._controller.heat_gain_samples
        heat_rate = gain_samples[-1]["rate_k_h"] if gain_samples else None
        last_stop = end_b = b_start = None
        runs = 0
        if heat_rate and not in_block:
            # Simuleer de vraag-cyclus met de huidige doorstook tot de
            # blokkade; de tijdstippen van runs/stops bepaalt mee hoe
            # lang de laatste stop moet overbruggen.
            stop_temp = min(doel + d_cur, MAX_TUB_C)
            t, b = now, tub
            demand = self._controller.warmtevraag or b < doel - marge
            for _ in range(40):
                if t >= start:
                    break
                if demand:
                    heat_h = max((stop_temp - b) / heat_rate, 0)
                    t_end = t + timedelta(hours=heat_h)
                    runs += 1
                    if t_end >= start:
                        last_stop = start
                        b += heat_rate * (start - t).total_seconds() / 3600
                        t = start
                    else:
                        last_stop = t_end
                        if heat_h:
                            b = stop_temp
                        t = t_end
                    demand = False
                else:
                    cool_h = (
                        max((b - (doel - marge)) / rate, 0)
                        if rate > 1e-6
                        else float("inf")
                    )
                    t_low = t + timedelta(hours=cool_h)
                    if t_low >= start:
                        b -= rate * (start - t).total_seconds() / 3600
                        t = start
                    else:
                        b, t = doel - marge, t_low
                        demand = True
            b_start = b
            end_b = b - rate * (end - t).total_seconds() / 3600
        # De doorstook die de laatste stop had moeten halen: verlies
        # van die stop tot piek-einde. Geen run meer voor de blokkade
        # -> de huidige warmte moet het hele traject dragen.
        coast_h = (
            (end - last_stop).total_seconds() / 3600
            if last_stop is not None
            else (block_h if in_block else hours_end)
        )
        advies = min(max(rate * coast_h, 0.0), MAX_TUB_C - doel)
        return {
            "advies": round(advies, 1),
            "rate": round(rate, 2),
            "hours_end": round(hours_end, 1),
            "hours_start": round(hours_start, 1),
            "block_h": round(block_h, 1),
            "loss_now": round(rate * hours_end, 1),
            "in_block": in_block,
            "heat_rate": heat_rate,
            "last_stop": last_stop.strftime("%H:%M") if last_stop else None,
            "runs": runs if heat_rate else None,
            "b_start": round(b_start, 1) if b_start is not None else None,
            "end_b": round(end_b, 1) if end_b is not None else None,
            "peak_start": conf.get(CONF_PEAK_START, DEFAULT_PEAK_START),
            "peak_end": conf.get(CONF_PEAK_END, DEFAULT_PEAK_END),
            "at": samples[-1]["at"],
        }

    @property
    def native_value(self) -> float | None:
        """Geadviseerde doorstook in K boven het doel."""
        advies = self._advies()
        return advies["advies"] if advies else None

    @property
    def extra_state_attributes(self) -> dict:
        """Onderbouwing: gebruikte meetwaarden en blokkadevenster."""
        advies = self._advies()
        if advies is None:
            return {"reden": "geen afkoelingsmeting of kuip/buiten-sensor ontbreekt"}
        conf = {**self._entry.data, **self._entry.options}
        return {
            "afkoeling_k_per_u": advies["rate"],
            "blokkade_uren": advies["block_h"],
            "verlies_vanaf_nu_k": advies["loss_now"],
            "uren_tot_piek_einde": advies["hours_end"],
            "uren_tot_piek_start": advies["hours_start"],
            "stijgsnelheid_k_per_u": advies["heat_rate"],
            "laatste_stop_gesimuleerd": advies["last_stop"],
            "runs_voor_blokkade": advies["runs"],
            "bulk_bij_blokkade_start": advies["b_start"],
            "verwacht_piek_einde_huidig": advies["end_b"],
            "in_blokkade": advies["in_block"],
            "piek_start": advies["peak_start"],
            "piek_einde": advies["peak_end"],
            "gebaseerd_op_meting_van": advies["at"],
            "huidige_doorstook_k": conf.get(CONF_OVERSHOOT, DEFAULT_OVERSHOOT_K),
            "advies_stopgrens_c": round(
                self._controller.heat_setpoint + advies["advies"], 1
            ),
        }


class JacuzziPeakEndForecastSensor(_ControllerSensor):
    """Voorspelde kuip-temp op het eerstvolgende piek-einde.

    Afkoeling = gemeten W/K x huidige (kuip - buiten); W/K schaalt
    mee naar winterse nachten. Zonder afkoelingsmeting of sensor
    is er geen voorspelling — de sensor toont dan niets.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:thermometer-clock"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de controller."""
        super().__init__(entry, "verwacht_piek_einde")

    def _forecast(self) -> dict | None:
        """Reken de voorspelling uit, of None als inputs ontbreken."""
        samples = self._controller.heat_loss_samples
        if not samples:
            return None
        conf = {**self._entry.data, **self._entry.options}
        tub, amb = self._tub_ambient()
        if tub is None or amb is None:
            return None
        hours = _hours_until(conf, CONF_PEAK_END, DEFAULT_PEAK_END)
        rate = self._cooling_rate_k_h(samples, tub, amb)
        return {
            "predicted": round(tub - rate * hours, 1),
            "hours": round(hours, 1),
            "rate": round(rate, 2),
            "tub": tub,
            "amb": amb,
            "at": samples[-1]["at"],
            "peak_end": conf.get(CONF_PEAK_END, DEFAULT_PEAK_END),
        }

    @property
    def native_value(self) -> float | None:
        """Voorspelde bulk-temperatuur op piek-einde."""
        forecast = self._forecast()
        return forecast["predicted"] if forecast else None

    @property
    def extra_state_attributes(self) -> dict:
        """Onderbouwing: gebruikte meetwaarden en resttijd."""
        forecast = self._forecast()
        if forecast is None:
            return {"reden": "geen afkoelingsmeting of sensor ontbreekt"}
        return {
            "uren_tot_piek_einde": forecast["hours"],
            "piek_einde": forecast["peak_end"],
            "afkoeling_k_per_u": forecast["rate"],
            "kuip_nu_c": forecast["tub"],
            "buiten_nu_c": forecast["amb"],
            "gebaseerd_op_meting_van": forecast["at"],
        }
