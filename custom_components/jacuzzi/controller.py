"""Core control logic for the Jacuzzi Poolex Control integration.

Ported from the YAML automations in packages/jacuzzi.yaml:

- peak block (Poolex off + Watercare peak during a time window)
- circulation pump on when the Poolex wants to heat (tub temp below
  Poolex setpoint)
- circulation pump off when the setpoint is reached — but only if WE
  turned the pump on, and never while the compressor runs
- failsafe: compressor running while pump is off -> pump on + notify
- monitoring: fault-bit and "heat demand without compressor" alerts
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import homeassistant.helpers.entity_registry as er
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_JACUZZI_CLIMATE,
    CONF_NORMAL_SETPOINT,
    CONF_NOTIFY_SERVICE,
    CONF_PEAK_END,
    CONF_PEAK_MODE,
    CONF_PEAK_SETPOINT,
    CONF_PEAK_START,
    EVAL_INTERVAL_S,
    FAILSAFE_DELAY_S,
    NO_COMPRESSOR_S,
    PEAK_MODE_OFF,
    PROBLEM_DELAY_S,
    PUMP_RUNON_S,
    RELOAD_COOLDOWN_S,
    UNREACHABLE_S,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = "jacuzzi_controller_update"


def _float(state: State | None, default: float = 0.0) -> float:
    """Parse a state's value, with fallback."""
    try:
        return float(state.state)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return default


def _attr_float(state: State | None, attr: str, default: float = 99.0) -> float:
    """Parse a state attribute, with fallback."""
    try:
        return float(state.attributes[attr])  # type: ignore[index]
    except (TypeError, ValueError, KeyError):
        return default


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    hour, minute = value.split(":")
    return int(hour), int(minute)


class JacuzziController:
    """Implements the pump/peak/monitoring logic."""

    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        """Store config; listeners attach in async_start()."""
        self.hass = hass
        self.conf = conf
        self.pump_by_us = False          # wij hebben de pomp aangezet
        self.warmtevraag = False          # warmte gevraagd (voor sensor)
        self._compressor_recently_on = False  # compressor heeft gedraaid (nadraai)
        self._peak_saved_setpoint: float | None = None  # setpoint vóór de piek
        self._since: dict[str, datetime] = {}
        self._notified: set[str] = set()
        self._last_gecko_reload = 0.0
        self._unsubs = []

    # --- helpers -----------------------------------------------------

    def _state(self, key: str) -> State | None:
        return self.hass.states.get(self.conf[key])

    def _since_true(self, key: str, predicate: bool, delay_s: float) -> bool:
        """True when `predicate` has been continuously true for `delay_s`."""
        if not predicate:
            self._since.pop(key, None)
            return False
        now = dt_util.now()
        if key not in self._since:
            self._since[key] = now
        return (now - self._since[key]).total_seconds() >= delay_s

    def _notify_once(self, key: str, active: bool, title: str, message: str) -> None:
        """Send a notification once per episode (until `active` clears)."""
        if not active:
            self._notified.discard(key)
            return
        if key in self._notified:
            return
        self._notified.add(key)
        service = self.conf[CONF_NOTIFY_SERVICE]
        _LOGGER.warning("%s: %s", title, message)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "notify", service, {"title": title, "message": message}
            )
        )

    async def _async_call(self, domain: str, service: str, data: dict) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001 - log en ga door
            _LOGGER.error("%s.%s faalde: %s", domain, service, err)

    def _maybe_reload_gecko(self) -> None:
        """Reload de Gecko config-entry als die in een dode sessie hangt.

        De geckoal-integratie herstelt een runtime-gestorven
        MQTT-verbinding niet altijd vanzelf (503 tijdens token-refresh ->
        'No active connection'). Een reload forceert een nieuwe sessie —
        net als handmatig herstarten. Max 1x per RELOAD_COOLDOWN_S.
        """
        now = time.monotonic()
        if now - self._last_gecko_reload < RELOAD_COOLDOWN_S:
            return
        entity_entry = er.async_get(self.hass).async_get(
            self.conf[CONF_JACUZZI_CLIMATE]
        )
        if entity_entry is None or entity_entry.config_entry_id is None:
            return
        self._last_gecko_reload = now
        _LOGGER.warning(
            "Gecko-entities al %d s unavailable — config entry reloaden",
            UNREACHABLE_S,
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entity_entry.config_entry_id)
        )

    # --- lifecycle ---------------------------------------------------

    async def async_start(self) -> None:
        """Attach all listeners."""
        watched = [
            self.conf["poolex_climate"],
            self.conf["jacuzzi_climate"],
            self.conf["pump_fan"],
            self.conf["compressor_sensor"],
            self.conf["problem_sensor"],
        ]
        self._unsubs.append(
            async_track_state_change_event(self.hass, watched, self._on_change)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._on_tick, timedelta(seconds=EVAL_INTERVAL_S)
            )
        )
        hh, mm = _parse_hhmm(self.conf[CONF_PEAK_START])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_peak_start, hour=hh, minute=mm, second=0
            )
        )
        hh, mm = _parse_hhmm(self.conf[CONF_PEAK_END])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_peak_end, hour=hh, minute=mm, second=0
            )
        )
        # HA-restart midden in het piekvenster -> blokkeer alsnog
        if self._in_peak_window():
            await self._on_peak_start(None)
        await self._async_evaluate()

    def _in_peak_window(self) -> bool:
        """Nu binnen het piekvenster? (handelt over-middernacht af)."""
        now = dt_util.now().time()
        s_h, s_m = _parse_hhmm(self.conf[CONF_PEAK_START])
        e_h, e_m = _parse_hhmm(self.conf[CONF_PEAK_END])
        start = now.replace(hour=s_h, minute=s_m, second=0)
        end = now.replace(hour=e_h, minute=e_m, second=0)
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def async_stop(self) -> None:
        """Detach listeners."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _on_change(self, _event) -> None:
        self.hass.async_create_task(self._async_evaluate())

    @callback
    def _on_tick(self, _now) -> None:
        self.hass.async_create_task(self._async_evaluate())

    # --- peak block ----------------------------------------------------

    def _watercare_option(self, key: str) -> str:
        """Normaliseer watercare-optie naar Gecko-enum (SUPER_SAVINGS)."""
        return str(self.conf[key]).strip().upper().replace(" ", "_")

    async def _on_peak_start(self, _now) -> None:
        """Piekblokkade AAN: Poolex dicht + Watercare naar piek-mode.

        'setpoint'-mode (default): setpoint naar het dieptepunt — de unit
        blijft aan (telemetrie + vorstbeveiliging blijven werken) maar
        de compressor krijgt nooit vraag. 'off'-mode: hvac_mode uit.
        """
        _LOGGER.info("Piekblokkade AAN (%s)", self.conf[CONF_PEAK_START])
        if self.conf.get(CONF_PEAK_MODE) == PEAK_MODE_OFF:
            await self._async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.conf["poolex_climate"], "hvac_mode": "off"},
            )
        else:
            poolex = self._state("poolex_climate")
            current = _attr_float(poolex, "temperature", 38.0)
            # niet overschrijven als we midden in de piek (her)starten
            if current > self.conf.get(CONF_PEAK_SETPOINT, 4.0):
                self._peak_saved_setpoint = current
            await self._async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.conf["poolex_climate"],
                    "temperature": self.conf.get(CONF_PEAK_SETPOINT, 4.0),
                },
            )
        await self._async_call(
            "select",
            "select_option",
            {
                "entity_id": self.conf["watercare_select"],
                "option": self._watercare_option("watercare_peak"),
            },
        )

    async def _on_peak_end(self, _now) -> None:
        """Piekblokkade UIT: setpoint/mode herstellen + Watercare normaal."""
        _LOGGER.info("Piekblokkade UIT (%s)", self.conf[CONF_PEAK_END])
        if self.conf.get(CONF_PEAK_MODE) == PEAK_MODE_OFF:
            await self._async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.conf["poolex_climate"], "hvac_mode": "heat"},
            )
        else:
            restore = (
                self._peak_saved_setpoint
                if self._peak_saved_setpoint is not None
                else self.conf.get(CONF_NORMAL_SETPOINT, 38.0)
            )
            await self._async_call(
                "climate",
                "set_temperature",
                {"entity_id": self.conf["poolex_climate"], "temperature": restore},
            )
            self._peak_saved_setpoint = None
        await self._async_call(
            "select",
            "select_option",
            {
                "entity_id": self.conf["watercare_select"],
                "option": self._watercare_option("watercare_normal"),
            },
        )

    # --- main evaluation -----------------------------------------------

    async def _async_evaluate(self) -> None:
        """Evaluate all rules against the current state."""
        poolex = self._state("poolex_climate")
        jacuzzi = self._state("jacuzzi_climate")
        pump = self._state("pump_fan")
        compressor = self._state("compressor_sensor")
        problem = self._state("problem_sensor")
        # Monitor 0: Poolex onbereikbaar — stroomstoring of de
        # PRCD-stekker heeft getrapt (unit fysiek spanningsloos).
        poolex_gone = poolex is None or poolex.state in ("unavailable", "unknown")
        self._notify_once(
            "poolex_unreachable",
            self._since_true("poolex_unavail", poolex_gone, UNREACHABLE_S),
            "Jacuzzi: Poolex onbereikbaar",
            "De warmtepomp is al 15 min niet bereikbaar — stroomstoring of "
            "de test/reset-stekker getrapt? Check de unit.",
        )
        gecko_gone = jacuzzi is None or jacuzzi.state in ("unavailable", "unknown")
        gecko_down = self._since_true("gecko_unavail", gecko_gone, UNREACHABLE_S)
        self._notify_once(
            "gecko_unreachable",
            gecko_down,
            "Jacuzzi: Gecko onbereikbaar",
            "De jacuzzi-entities zijn al 15 min unavailable — Gecko-cloud "
            "onbereikbaar of in.touch offline? Check de app.",
        )
        if gecko_down:
            self._maybe_reload_gecko()
        if None in (poolex, jacuzzi, pump, compressor):
            return  # integraties nog niet klaar

        poolex_off = poolex.state == "off"
        setpoint = _attr_float(poolex, "temperature", 38.0)
        tub_temp = _attr_float(jacuzzi, "current_temperature")
        compressor_on = _float(compressor) > 0
        pump_on = pump.state == "on"
        self.warmtevraag = not poolex_off and tub_temp < setpoint - self.conf["temp_margin"]

        # Onthoud dat de compressor echt gedraaid heeft — de nadraai-timer
        # (comp_idle) mag alleen tellen ná een echte run, anders is
        # 'compressor al 3 min uit' permanent waar en slaat de pomp direct af.
        if compressor_on:
            self._compressor_recently_on = True
            self._since.pop("comp_idle", None)

        # FAILSAFE: compressor draait maar pomp staat uit -> restwarmte
        # kan niet weg -> d1. Pomp direct weer aan.
        compressor_running = self._since_true(
            "comp_running", compressor_on, FAILSAFE_DELAY_S
        )
        if compressor_running and not pump_on:
            _LOGGER.warning("Compressor draait zonder flow — pomp aan")
            await self._async_call(
                "fan", "turn_on", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = True
            self._notify_once(
                "failsafe", True, "Jacuzzi: pomp hersteld",
                "De circulatiepomp stond uit terwijl de compressor draaide — weer aangezet.",
            )
        else:
            self._notify_once("failsafe", False, "", "")

        # Pomp AAN bij warmtevraag (onmiddellijk, zoals template-trigger)
        if self.warmtevraag and not pump_on:
            _LOGGER.info("Warmtevraag (%.1f < %.1f) — pomp aan", tub_temp, setpoint)
            await self._async_call(
                "fan", "turn_on", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = True

        # Pomp UIT: een van de drie herkansingspaden is lang genoeg waar.
        # comp_idle telt alleen als de compressor echt heeft gedraaid —
        # nadraaien na een run, geen 'al eeuwen idle'.
        off_due = (
            self._since_true("temp_reached", tub_temp >= setpoint, PUMP_RUNON_S)
            or self._since_true("poolex_off", poolex_off, PUMP_RUNON_S)
            or self._since_true(
                "comp_idle",
                not compressor_on and self._compressor_recently_on,
                PUMP_RUNON_S,
            )
        )
        if off_due and pump_on and self.pump_by_us and not compressor_on:
            _LOGGER.info("Setpoint bereikt / geen vraag — pomp uit")
            await self._async_call(
                "fan", "turn_off", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = False
            self._compressor_recently_on = False

        # Monitor 1: echte fault-bit van de unit
        problem_active = problem is not None and problem.state == "on"
        self._notify_once(
            "problem",
            self._since_true("problem", problem_active, PROBLEM_DELAY_S),
            "Jacuzzi: Poolex fault",
            "De Poolex rapporteert een probleem (problem-sensor aan). "
            "Check de unit — bij d1: te weinig doorstroom, bypass verder dichtknijpen.",
        )

        # Monitor 2: warmtevraag zonder compressor = flow-error proxy
        self._notify_once(
            "no_compressor",
            self._since_true(
                "no_compressor", self.warmtevraag and not compressor_on, NO_COMPRESSOR_S
            ),
            "Jacuzzi: Poolex mogelijk in storing",
            f"De warmtepomp staat aan en het water is te koud ({tub_temp} °C), "
            "maar de compressor draait niet — waarschijnlijk een flow-error. "
            "Draait de circulatiepomp? Check de unit.",
        )

        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
