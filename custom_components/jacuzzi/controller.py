"""Kuip-regelaar met fail-safe compressorremming.

Veiligheidsinvarianten (niet vervangen door een model van Poolex-pockets):
- Gebruikersdoel komt uitsluitend van de eigen climate/restore, nooit Poolex.
- Alleen recente, geldige Gecko-kuipmeting geeft toestemming tot verwarmen.
- Stop bij doel + doorstook (maximaal 40 °C), ongeacht minimale looptijd.
- Anti-pendel beperkt HERSTART; geen sensorfallback of externe rem-override.
- Rem pas bevestigd bij laag actuator-setpoint én recente compressor-duty=0.
- Poolex NOOIT op off: 15 °C blijven vragen, stop controleren en alarmeren.
- Nakoeling blijft actief bij onbekende compressor/uitlaat; flow-fault en
  onderhoud hebben hun eigen stopbeleid voor mogelijk drooglopen.

HA kan geen fysieke uitschakeling garanderen bij communicatie-/stroomuitval,
of een sensor herkennen die verse maar foutieve waarden rapporteert.
De onafhankelijke hardwarebeveiligingen blijven noodzakelijk.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

import homeassistant.helpers.entity_registry as er
from homeassistant.loader import async_get_integration
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
    CONF_MAINT_SAVED,
    CONF_MAINTENANCE,
    CONF_MIX_ENABLED,
    CONF_MIX_INTERVAL_MIN,
    CONF_MIX_PULSE_S,
    CONF_NORMAL_SETPOINT,
    CONF_NOTIFY_SERVICE,
    CONF_OVERSHOOT,
    CONF_PEAK_END,
    CONF_PEAK_START,
    CONF_POOLEX_ALWAYS_ON,
    CONF_SOLAR_MIN_W,
    CONF_SOLAR_SENSOR,
    COOLDOWN_MIN_DROP_K,
    COOLDOWN_MIN_H,
    DEFAULT_AMBIENT_SENSOR,
    DEFAULT_MIX_ENABLED,
    DEFAULT_MIX_INTERVAL_MIN,
    DEFAULT_MIX_PULSE_S,
    DEFAULT_NORMAL_SETPOINT,
    DEFAULT_OVERSHOOT_K,
    DEFAULT_POOLEX_ALWAYS_ON,
    DOMAIN,
    EVAL_INTERVAL_S,
    PUMP_RESCUE_RETRY_S,
    GECKO_MAINT_TARGET_C,
    FAILSAFE_NOTIFY_MIN,
    FAILSAFE_NOTIFY_WINDOW_S,
    FLOW_FAULT_OFF_S,
    HEAT_LOSS_SAMPLES,
    MAINT_PUMP_GRACE_S,
    MAINT_PUMP_RETRY_S,
    MIX_PUMPS,
    NO_COMPRESSOR_S,
    OVERHEAT_MARGIN_K,
    POOLEX_OFF_GUARD_S,
    POOLEX_OUTLET_SENSOR,
    POOLEX_SETPOINT_FLOOR,
    POST_HEAT_OUTLET_C,
    PROBLEM_DELAY_S,
    PUMP_CMD_DEBOUNCE_S,
    PUMP_RUNON_S,
    RELOAD_COOLDOWN_S,
    RETOUR_GUARD_K,
    RETOUR_GUARD_MAX_K,
    RETOUR_GUARD_MIN_K,
    SOLAR_SURPLUS_S,
    SP_SUPPRESS_REST_S,
    STARTUP_REST_S,
    MAX_TUB_C,
    SENSOR_MAX_AGE_S,
    BRAKE_CONFIRM_S,
    ACTUATOR_RETRY_S,
    TUB_WATER_KG,
    UNREACHABLE_S,
    VERIFY_MAX_RESUMES,
    VERIFY_MIX_S,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = "jacuzzi_controller_update"


def _float(state: State | None, default: float = 0.0) -> float:
    """Parse a state's value, with fallback."""
    try:
        return float(state.state)  # type: ignore[union-attr]
    except (TypeError, ValueError, AttributeError):
        return default


def _attr_float(state: State | None, attr: str, default: float = 99.0) -> float:
    """Parse a state attribute, with fallback."""
    try:
        return float(state.attributes[attr])  # type: ignore[index]
    except (TypeError, ValueError, KeyError, AttributeError):
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
        self.pump_by_us = False  # wij hebben de pomp aangezet
        self.warmtevraag = False  # warmte gevraagd (voor sensor)
        self._compressor_recently_on = False  # compressor heeft gedraaid (nadraai)
        self._peak_saved_setpoint: float | None = None  # setpoint vóór de piek
        self._since: dict[str, datetime] = {}
        self._notified: set[str] = set()
        self._last_gecko_reload = 0.0
        self._mix_last = 0.0  # monotonic ts laatste meng-puls
        self._mix_until = 0.0  # monotonic ts einde lopende puls
        self._mix_next = 0  # index in MIX_PUMPS (wisselend)
        self._mix_entity: str | None = None  # pomp die nu pulseert
        self._peak_active = False  # piek-blok is toegepast
        self._early_restored = False  # PV-einde al gedaan dit venster
        self._flow_lockout = False  # flow-fault: geen auto-aanzet pomp
        self._flow_saved_watercare: str | None = None  # watercare vóór Away
        self._pump_cmd_at = 0.0  # monotonic ts laatste pomp-commando
        self._maint_off_retry: dict[str, float] = {}  # backoff per pomp
        self._maint_gecko_at = -ACTUATOR_RETRY_S
        self._guard_gecko_at = -ACTUATOR_RETRY_S
        self._cool: dict | None = None  # lopend warmteverlies-meetvenster
        self.heat_loss_samples: list[dict] = []  # laatste N metingen
        # Alleen de eigen climate/restore bepaalt het kuip-doel. De
        # Poolex is actuator; zijn (vertraagde) echo mag niets adopteren.
        try:
            configured_target = float(conf.get(CONF_NORMAL_SETPOINT, DEFAULT_NORMAL_SETPOINT))
        except (TypeError, ValueError):
            configured_target = float("nan")  # ongeldige oude config: veilig uit, niet setup afbreken
        valid_target = (
            math.isfinite(configured_target) and 20 <= configured_target <= MAX_TUB_C
        )
        self._heat_setpoint = (
            configured_target if valid_target else DEFAULT_NORMAL_SETPOINT
        )
        # Reload verliest runhistorie: eerst een bevestigde rem en 30 s
        # startup-rust. Gewone anti-pendelrust na een run blijft apart.
        self._demand_suppressed = True  # gewenste rem, bevestiging apart
        self._suppress_reason = ""  # waarom: oververhitting/hvac-uit/anti-pendel
        self._suppress_at = time.monotonic()  # rust ook na startup/reload
        self._rest_s = STARTUP_REST_S
        self._sp_write_at = 0.0  # monotonic ts laatste sp-write
        self._afterheat_hold = False  # pomp vastgehouden voor nakoeling
        self._verify_until = 0.0  # einde meng-check (monotonic)
        self._verify_pending = False  # check klaar: hervat toegestaan
        self._verify_resumes = 0  # hervattingen deze sessie
        self._verify_tub_at_stop: float | None = None  # kuip bij stop
        # Lerende retour-offset: kuip-las bij stop minus gemengde bulk.
        # Alleen diagnostiek; herstel via climate-attributen bij reload.
        # Geen minimum van 2 K meer: dat hoorde bij het vervallen guard-model.
        self._retour_offset = RETOUR_GUARD_K
        self._failsafe_hits: list[float] = []  # ts van pomp-dips (demping)
        # Compressorhistorie is alleen voor nadraai, nooit sensorselectie.
        self._comp_last_on = 0.0
        self.compressor_on: bool | None = None  # onbekend is niet uit
        self.heating_enabled = valid_target  # ongeldige oude config start veilig uit
        self.demand_temp: float | None = None  # bron-temp voor het display
        self.demand_bron = ""  # welke sensor leidend was
        self._unsubs = []
        self._eval_lock = asyncio.Lock()
        self._started = False
        self._stopped = False
        self._run_active = False
        self._brake_since: float | None = None
        self._brake_write_at = -ACTUATOR_RETRY_S
        self._brake_lockout = False
        self._actuator_at = -ACTUATOR_RETRY_S
        self._mix_check_failed = False

    @property
    def heat_setpoint(self) -> float:
        """Gewenste kuip-temperatuur (voor de climate-entity)."""
        return self._heat_setpoint

    @property
    def demand_suppressed(self) -> bool:
        """Anti-pendel-rem actief: setpoint staat op de vloer."""
        return self._demand_suppressed

    @property
    def retour_offset(self) -> float:
        """Geleerde kuip-retour offset (EMA per meng-check)."""
        return self._retour_offset

    @property
    def rem_reden(self) -> str:
        """Waarom het setpoint laag staat ('uit' als er geen rem is)."""
        if self._brake_lockout:
            return self._suppress_reason or "stop niet bevestigd"
        if self._peak_active:
            return "piekblokkade"
        if self._demand_suppressed:
            return self._suppress_reason or "anti-pendel"
        return "uit"

    @property
    def regelstatus(self) -> str:
        """Eén leesbare regeltoestand voor het dashboard."""
        if self.conf.get(CONF_MAINTENANCE):
            return "onderhoud"
        if self._brake_lockout or self._suppress_reason in (
            "sensoruitval",
            "oververhitting",
            "flow-storing",
            "ongeldige instellingen",
        ):
            return f"rem ({self._suppress_reason})"
        if self._peak_active:
            return "piekblokkade"
        if self._verify_until and time.monotonic() < self._verify_until:
            return "meng-check"
        if self._demand_suppressed:
            return f"rem ({self.rem_reden})"
        if self.warmtevraag:
            # Exacte KUIP-stopgrens zichtbaar; geen afgeronde/inlaat-grens.
            return f"vraag tot kuip ≥ {self.stop_temperature:g} °C"
        return "idle"

    @property
    def stop_temperature(self) -> float:
        """Normale stopgrens: gebruikersdoel + doorstook, nooit boven 40 °C."""
        overshoot = self._option_float(CONF_OVERSHOOT, DEFAULT_OVERSHOOT_K)
        if not math.isfinite(overshoot):
            overshoot = 0.0  # ongeldige instellingen blokkeren bovendien alle vraag
        return min(MAX_TUB_C, self._heat_setpoint + max(0.0, min(2.0, overshoot)))

    # --- helpers -----------------------------------------------------

    @callback
    def async_restore_retour_offset(self, value) -> bool:
        """Bewaar geleerde diagnostiek over reloads, zonder de regeling te wijzigen."""
        try:
            value = float(value)
        except (ValueError, TypeError):
            return False
        if not math.isfinite(value) or not RETOUR_GUARD_MIN_K <= value <= RETOUR_GUARD_MAX_K:
            return False
        self._retour_offset = value
        return True

    def maintenance_gecko_target(self) -> float:
        """Laag pack-doel; dit verandert NOOIT het eigen gebruikersdoel."""
        minimum = _attr_float(self._state("jacuzzi_climate"), "min_temp", GECKO_MAINT_TARGET_C)
        return minimum if math.isfinite(minimum) and 15 <= minimum <= 18 else GECKO_MAINT_TARGET_C

    def _option_float(self, key: str, default: float) -> float:
        """Ongeldige opgeslagen opties worden een veilige blokkade, geen crash."""
        try:
            return float(self.conf.get(key, default))
        except (ValueError, TypeError):
            return float("nan")

    def _fresh(self, state: State | None) -> bool:
        """Een oude/unavailable meting mag geen verwarming toestaan."""
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        # last_reported vernieuwt ook bij GELIJKE waarden. Dit detecteert
        # gestopte HA-rapportage, niet een defecte sensor die verse maar
        # foutieve waarden blijft sturen; hardwarebeveiliging blijft nodig.
        reported = getattr(state, "last_reported", state.last_updated)
        return 0 <= (dt_util.now() - reported).total_seconds() <= SENSOR_MAX_AGE_S

    async def _brake(self) -> bool:
        """Handhaaf de rem; service-succes is NIET hetzelfde als compressor-uit."""
        now = time.monotonic()
        poolex = self._state("poolex_climate")
        live_sp = _attr_float(poolex, "temperature", 99.0)
        low = (
            self._fresh(poolex)
            and math.isfinite(live_sp)
            and (4.0 <= live_sp <= POOLEX_SETPOINT_FLOOR + 0.1)
        )
        stopped = self.compressor_on is False
        confirmed = low and stopped
        if confirmed:
            if self._brake_since is not None:
                # Rust telt vanaf BEVESTIGDE stop, niet vanaf het commando.
                self._suppress_at = now
            self._brake_since = None
        elif self._brake_since is None:
            self._brake_since = now
        failed = False
        if (not low or (self._brake_lockout and not confirmed)) and now - self._brake_write_at >= ACTUATOR_RETRY_S:
            self._brake_write_at = now
            failed = not await self._async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.conf["poolex_climate"],
                    "temperature": POOLEX_SETPOINT_FLOOR,
                },
            )
        # NOOIT Poolex off: daarmee verdwijnen telemetrie en eigen
        # beveiliging/standby. Bij een mislukte of niet-bevestigde rem:
        # hoge vraag blokkeren, 15 °C blijven herhalen en alarmeren.
        # Circulatie blijft behouden zolang compressor-uit niet bevestigd is.
        expired = (
            self._brake_since is not None and now - self._brake_since >= BRAKE_CONFIRM_S
        )
        if failed or expired or self._brake_lockout:
            self._brake_lockout = True
            self.warmtevraag = False
            self._run_active = False
            self._suppress_reason = (
                "rem (herstel nodig)" if confirmed else "stop niet bevestigd"
            )
            self._notify_once(
                "brake_failed",
                True,
                "Jacuzzi: stop niet bevestigd",
                "Compressorstop niet bevestigd — vraag blijft 15 °C; Poolex blijft aan. Controleer de unit; "
                "bij communicatie-uitval kan software geen fysieke stop garanderen.",
            )
        return confirmed

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

    async def _async_call(self, domain: str, service: str, data: dict) -> bool:
        if self._stopped:
            return False
        # Centrale invariant, ook voor oude piek-configs: nooit hvac off.
        if domain == "climate" and data.get("hvac_mode") == "off":
            return False
        # Een wijziging tijdens een await kan een oude evaluatie inhalen.
        # Geen nieuwe warmte/mengpomp als onderhoud of noodstop intussen geldt.
        # Deze bewaking geldt voor Poolex. Gecko krijgt uitsluitend een
        # laag onderhoudsdoel; 18 °C is daar geen hoog warmteverzoek.
        heating_call = domain == "climate" and data.get("entity_id") == self.conf["poolex_climate"] and (
            data.get("hvac_mode") == "heat"
            or data.get("temperature", 0) > POOLEX_SETPOINT_FLOOR
        )
        if heating_call:
            # 'Warmtevraag uit' laat veilige standby/vorstbewaking toe,
            # maar alleen op een reeds bevestigd laag actuator-setpoint.
            poolex = self._state("poolex_climate")
            live_sp = _attr_float(poolex, "temperature", 99.0)
            standby = (
                data.get("hvac_mode") == "heat"
                and self._fresh(poolex)
                and math.isfinite(live_sp)
                and 4.0 <= live_sp <= POOLEX_SETPOINT_FLOOR + 0.1
            )
            if data.get("hvac_mode") == "heat" and not standby:
                return False
            # Herlees vlak vóór warmtecommando: een gebruikerswijziging of
            # sensoruitval tijdens een eerdere await heeft altijd voorrang.
            tub = self._state("jacuzzi_climate")
            temp = _attr_float(tub, "current_temperature", float("nan"))
            if (
                (not self.heating_enabled and not standby)
                or not self._fresh(tub)
                or not math.isfinite(temp)
                or not 1.0 <= temp <= 45.0
                or (temp >= self.stop_temperature and not standby)
            ):
                return False
            problem = self._state("problem_sensor")
            pump = self._state("pump_fan")
            compressor = self._state("compressor_sensor")
            duty = _float(compressor, float("nan"))
            if (
                not self._fresh(problem)
                or problem.state != "off"
                or not self._fresh(compressor)
                or not math.isfinite(duty)
                or not 0 <= duty <= 100
                or not self._fresh(pump)
                or (
                    data.get("temperature", 0) > POOLEX_SETPOINT_FLOOR
                    and pump.state != "on"
                )
            ):
                return False
        if (self.conf.get(CONF_MAINTENANCE) or self._brake_lockout) and (
            heating_call or (domain == "fan" and service == "turn_on")
        ):
            # Circulatie voor restwarmte blijft bij noodstop toegestaan.
            if not (
                domain == "fan"
                and data.get("entity_id") == self.conf["pump_fan"]
                and not self.conf.get(CONF_MAINTENANCE)
            ):
                return False
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(domain, service, data, blocking=True),
                timeout=10,
            )
            return True
        except Exception as err:  # noqa: BLE001 - log en ga door
            _LOGGER.error("%s.%s faalde: %s", domain, service, err)
            return False

    def _track_cooldown(
        self, tub_temp: float, tub_valid: bool, pump_on: bool, compressor_on: bool
    ) -> None:
        """Meet passief warmteverlies tijdens stille periodes.

        Pompen uit + compressor uit + geldige kuip-temp = meetvenster.
        Bij einde venster (pomp/compressor aan): >=3 uur én >=0.5 K
        daling -> W/K-meting (afkoeling genormaliseerd op kuip-buiten).
        Dek open/dicht zie je als spreiding tussen metingen.
        """
        amb_st = self.hass.states.get(DEFAULT_AMBIENT_SENSOR)
        ambient = (
            _float(amb_st)
            if amb_st is not None and amb_st.state not in ("unavailable", "unknown")
            else None
        )
        massage_on = any(
            (st := self.hass.states.get(ent)) is not None and st.state == "on"
            for ent in MIX_PUMPS
        )
        quiet = (
            not (pump_on or compressor_on or massage_on)
            and self._mix_until <= time.monotonic()
        )
        if quiet and tub_valid and ambient is not None:
            if self._cool is None:
                self._cool = {
                    "start": dt_util.now(),
                    "tub0": tub_temp,
                    "tub": tub_temp,
                    "amb_sum": 0.0,
                    "amb_n": 0,
                }
            else:
                self._cool["tub"] = tub_temp
                self._cool["amb_sum"] += ambient
                self._cool["amb_n"] += 1
            return
        if self._cool is None:
            return
        cool, self._cool = self._cool, None
        hours = (dt_util.now() - cool["start"]).total_seconds() / 3600
        drop = cool["tub0"] - cool["tub"]
        if hours < COOLDOWN_MIN_H or drop < COOLDOWN_MIN_DROP_K:
            return
        amb_mean = cool["amb_sum"] / cool["amb_n"] if cool["amb_n"] else None
        delta_t = (cool["tub0"] + cool["tub"]) / 2 - amb_mean if amb_mean else 0.0
        if delta_t < 1.0:  # kuip ≈ buiten: verlies is ~0, W/K niet deelbaar
            return
        rate = drop / hours  # K/h
        # W = kg * kJ/kgK * K/h / 3.6 ; W/K = / (kuip - buiten)
        w_per_k = TUB_WATER_KG * 4.19 / 3.6 * rate / delta_t
        sample = {
            "at": dt_util.now().isoformat(timespec="minutes"),
            "hours": round(hours, 1),
            "drop_k": round(drop, 1),
            "rate_k_h": round(rate, 2),
            "tub_start_c": cool["tub0"],
            "tub_end_c": cool["tub"],
            "ambient_mean_c": round(amb_mean, 1),
            "delta_t_k": round(delta_t, 1),
            "w_per_k": round(w_per_k, 1),
        }
        self.heat_loss_samples.append(sample)
        del self.heat_loss_samples[:-HEAT_LOSS_SAMPLES]
        _LOGGER.info(
            "Warmteverlies-meting: %.1f K in %.1f u (%.2f K/h) bij ΔT %.1f K "
            "-> %.1f W/K",
            drop,
            hours,
            rate,
            delta_t,
            w_per_k,
        )

    def _pump_cmd_ready(self, *, urgent: bool = False) -> bool:
        """Debounce nieuwe pompcommando's bij vertraagde/stale state-echo's.

        De evaluatie is geserialiseerd; deze limiet voorkomt daarnaast
        herhaalde servicecalls voordat Gecko het resultaat rapporteert.
        """
        now = time.monotonic()
        # Flow-herstel heeft voorrang op gewone aan/uit-debounce, maar
        # behoudt een korte retrylimiet bij RF/cloud-fouten.
        delay = PUMP_RESCUE_RETRY_S if urgent else PUMP_CMD_DEBOUNCE_S
        if now - self._pump_cmd_at < delay:
            return False
        self._pump_cmd_at = now
        return True

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
        integration = await async_get_integration(self.hass, DOMAIN)
        _LOGGER.info(
            "Jacuzzi controller gestart — versie %s",
            integration.version or "onbekend",
        )
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
                self.hass, self._on_tick, hour=hh, minute=mm, second=0
            )
        )
        hh, mm = _parse_hhmm(self.conf[CONF_PEAK_END])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_tick, hour=hh, minute=mm, second=0
            )
        )
        self._started = True
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
        self._stopped = True

    async def async_shutdown(self) -> None:
        """Laat geen oude evaluatie of verweesde meng-puls doorlopen na reload."""
        self.async_stop()
        async with self._eval_lock:
            # Circulatie blijft behouden voor restwarmte; alleen onze eigen
            # meng-pomp afronden. De nieuwe controller bevestigt de rem opnieuw.
            commands = [
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.conf["poolex_climate"],
                        "temperature": POOLEX_SETPOINT_FLOOR,
                    },
                )
            ]
            if self._mix_entity:
                commands.append(("fan", "turn_off", {"entity_id": self._mix_entity}))
            for domain, service, data in commands:
                try:
                    await asyncio.wait_for(
                        self.hass.services.async_call(
                            domain, service, data, blocking=True
                        ),
                        10,
                    )
                except Exception:  # shutdown mag de resterende cleanup niet overslaan
                    _LOGGER.exception(
                        "Veilige shutdown-call mislukt: %s.%s", domain, service
                    )

    @callback
    def _on_change(self, _event) -> None:
        self.hass.async_create_task(self._async_evaluate())

    @callback
    def _on_tick(self, _now) -> None:
        self.hass.async_create_task(self._async_evaluate())

    @callback
    def async_set_target(self, value: float) -> None:
        """Doel-temperatuur vanuit de eigen climate-entity.

        Dit IS het gebruikersdoel — het live Poolex-setpoint is alleen
        een actuator-signaal (vloer bij geen vraag). Een piek-opslag
        vervalt: het nieuwe doel geldt meteen bij piek-einde.
        """
        value = float(value)
        if not math.isfinite(value) or not 20.0 <= value <= MAX_TUB_C:
            raise ValueError(f"Kuip-doel moet tussen 20 en {MAX_TUB_C:g} °C liggen")
        self._heat_setpoint = value
        self._peak_saved_setpoint = None
        if self._started:
            self.hass.async_create_task(self._async_evaluate())

    @callback
    def async_set_heating_enabled(self, enabled: bool) -> None:
        """Climate-hvac: off -> geen warmtevraag meer.

        Poolex blijft op 'heat' met laag setpoint, ook bij een niet
        bevestigde stop: blijven remmen en alarmeren, NOOIT hvac off.
        """
        self.heating_enabled = enabled
        if self._started:
            self.hass.async_create_task(self._async_evaluate())

    # --- peak block ----------------------------------------------------

    async def _set_watercare(self, key: str) -> bool:
        """Watercare-optie uit een conf-sleutel zetten."""
        return await self._set_watercare_opt(self.conf.get(key, ""))

    async def _set_watercare_opt(self, want) -> bool:
        """Selecteer een watercare-optie, tolerant voor hoofdletters/spaties.

        De geckoal-select gebruikt Title-Case opties ('Super Savings') of
        SCREAMING ('SUPER_SAVINGS') afhankelijk van de versie; match tegen
        de werkelijke options van de entity. False = retry later.
        """
        want = str(want).strip()
        sel = self._state("watercare_select")
        if sel is None or sel.state in ("unavailable", "unknown"):
            _LOGGER.warning("Watercare-select unavailable — %r overgeslagen", want)
            return False
        options = sel.attributes.get("options") or []
        if not options:
            _LOGGER.warning("Watercare-options nog niet geladen — %r uitgesteld", want)
            return False
        match = next(
            (
                o
                for o in options
                if str(o).strip().lower().replace("_", " ")
                == want.lower().replace("_", " ")
            ),
            None,
        )
        if match is None:
            _LOGGER.warning(
                "Watercare-optie %r niet gevonden; geldig: %s", want, options
            )
            return False
        # Let op: de geckoal-select adverteert Title-Case options maar
        # geckolib's set_mode_by_name accepteert alleen UPPER_SNAKE.
        # Multi-word opties ('Super Savings') falen daardoor altijd
        # stilletjes — gebruik single-word modes (Away/Savings/Weekender).
        if " " in match.strip():
            _LOGGER.warning(
                "Watercare-optie %r bevat een spatie — de gecko-integratie "
                "zet multi-word modes mogelijk niet door (stille fout)",
                match,
            )
        return await self._async_call(
            "select",
            "select_option",
            {
                "entity_id": self.conf["watercare_select"],
                "option": match,
            },
        )

    async def _on_peak_start(self, _now) -> None:
        """Piekblokkade AAN: Poolex dicht + Watercare naar piek-mode.

        Altijd setpoint naar 15 °C; de unit blijft aan voor telemetrie
        en eigen beveiligingen. Legacy 'off'-config wordt niet uitgevoerd.
        """
        if self._stopped or self.conf.get(CONF_MAINTENANCE):
            return
        _LOGGER.info("Piekblokkade AAN (%s)", self.conf[CONF_PEAK_START])
        poolex = self._state("poolex_climate")
        if poolex is None or poolex.state in ("unavailable", "unknown"):
            # _peak_active blijft False -> evaluate probeert het opnieuw
            _LOGGER.warning("Piekblokkade AAN uitgesteld — Poolex unavailable")
            return
        # Ook een oude opgeslagen peak_mode='off' mag de unit NIET
        # uitschrijven. Piekblokkade remt uitsluitend via 15 °C.
        ok = await self._async_call(
            "climate", "set_temperature", {
                "entity_id": self.conf["poolex_climate"],
                "temperature": POOLEX_SETPOINT_FLOOR,
            },
        )
        if ok:
            self._sp_write_at = time.monotonic()
        ok = await self._set_watercare("watercare_peak") and ok
        # False -> evaluate roept _on_peak_start de volgende tick opnieuw
        self._peak_active = ok

    async def _on_peak_end(self, _now) -> None:
        """Piekblokkade UIT: setpoint/mode herstellen + Watercare normaal."""
        if self._stopped or self.conf.get(CONF_MAINTENANCE):
            return
        _LOGGER.info("Piekblokkade UIT (%s)", self.conf[CONF_PEAK_END])
        poolex = self._state("poolex_climate")
        if poolex is None or poolex.state in ("unavailable", "unknown"):
            # _peak_active blijft True -> evaluate probeert het opnieuw
            _LOGGER.warning("Piekblokkade UIT uitgesteld — Poolex unavailable")
            return
        # Piek-einde is GEEN toestemming om te verwarmen. Eerst laag
        # houden; de normale regelaar beoordeelt kuip, sensoren en rusttijd.
        ok = await self._async_call(
            "climate",
            "set_temperature",
            {
                "entity_id": self.conf["poolex_climate"],
                "temperature": POOLEX_SETPOINT_FLOOR,
            },
        )
        self._peak_saved_setpoint = None
        self._run_active = False
        self._demand_suppressed = True
        self._suppress_reason = "piek-einde"
        ok = await self._set_watercare("watercare_normal") and ok
        # False -> evaluate roept _on_peak_end de volgende tick opnieuw
        self._peak_active = not ok

    # --- main evaluation -----------------------------------------------

    async def _async_evaluate(self) -> None:
        """Eén evaluatie tegelijk: geen restore die een gelijktijdige stop overschrijft."""
        async with self._eval_lock:
            if self._stopped or not self._started:
                return
            await self._evaluate_locked()

    async def _evaluate_locked(self) -> None:
        """Evaluate all rules against the current state under the lifecycle lock."""
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
        # Ontbrekende entities zijn GEEN reden om de rem over te slaan.
        # Onbekend betekent: niet verwarmen; aanwezige pomp mag blijven nakoelen.
        poolex = poolex or State(self.conf["poolex_climate"], "unavailable")
        jacuzzi = jacuzzi or State(self.conf["jacuzzi_climate"], "unavailable")
        pump = pump or State(self.conf["pump_fan"], "unavailable")
        compressor = compressor or State(self.conf["compressor_sensor"], "unavailable")

        # Onderhoudsmodus: alles handmatig — de controller doet niets.
        # Niet vechten met failsafe/altijd-aan/piek/menging; de gebruiker
        # bepaalt (bv. water verversen). Monitors hierboven lopen door.
        if self.conf.get(CONF_MAINTENANCE):
            self.warmtevraag = False
            self._run_active = False
            # Ook onderhoud gebruikt 15 °C, NOOIT hvac off. Tot de
            # compressorstop bevestigd is behouden we aanwezige flow.
            duty = _float(compressor, float("nan"))
            self.compressor_on = (
                duty > 0 if self._fresh(compressor) and math.isfinite(duty) and 0 <= duty <= 100
                else None
            )
            self._demand_suppressed = True
            self._suppress_reason = "onderhoud"
            await self._brake()
            # Ook het elektrische Gecko-element geen warmtevraag laten:
            # laag pack-setpoint handhaven naast Poolex=15, zonder doel-adoptie.
            gecko_target = self.maintenance_gecko_target()
            live_gecko_target = _attr_float(jacuzzi, "temperature", float("nan"))
            if (not math.isfinite(live_gecko_target) or abs(live_gecko_target - gecko_target) > 0.1) and (
                time.monotonic() - self._maint_gecko_at >= ACTUATOR_RETRY_S
            ):
                self._maint_gecko_at = time.monotonic()
                ok = await self._async_call("climate", "set_temperature", {
                    "entity_id": self.conf["jacuzzi_climate"], "temperature": gecko_target,
                })
                self._notify_once("maintenance_gecko", not ok, "Jacuzzi: Gecko onderhoudsdoel niet gezet",
                                  "Gecko-doel kon niet laag worden gezet; controleer de pack.")
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
            # Uitzondering: de Gecko-pack start zélf korte check-cycli
            # (~40 s) om water te samplen / vorst te bewaken. Die laten
            # we met rust (anders pingpong: hij aan, wij uit, hij aan).
            # Pas als de pomp langer dan de grace-periode aanhoudt —
            # geen check-cyclus meer — zetten we hem terug. De melding
            # gaat wél meteen weg zodra de pomp aan gaat.
            now = time.monotonic()
            pumps_on = [
                ent
                for ent in (self.conf["pump_fan"], *MIX_PUMPS)
                if (st := self.hass.states.get(ent)) is not None and st.state == "on"
            ]
            self._notify_once(
                "maintenance_pump",
                bool(pumps_on),
                "Jacuzzi: pomp aan tijdens onderhoud",
                "De Gecko startte een circulatie-cyclus. Korte check-runs "
                "laten we; > 90 s wordt hij teruggezet. Bij een lege kuip: "
                "groep uitschakelen in de meterkast.",
            )
            duty = _float(compressor, float("nan"))
            self.compressor_on = (
                duty > 0
                if self._fresh(compressor) and math.isfinite(duty) and 0 <= duty <= 100
                else None
            )
            compressor_stopped = (
                self._fresh(compressor) and math.isfinite(duty) and duty == 0
            )
            for ent in pumps_on:
                # Een mislukte 15 °C-rem mag niet ook de koelende flow
                # wegnemen. Bij lege kuip is fysieke spanningsloosheid nodig.
                if ent == self.conf["pump_fan"] and not compressor_stopped:
                    continue
                if not self._since_true(
                    f"maint_pump_grace_{ent}", True, MAINT_PUMP_GRACE_S
                ):
                    continue
                # De pack weigert turn_off tijdens zijn eigen cycli
                # ("active non-user initiators") — retry met backoff i.p.v.
                # elke tick een gefaalde call + ERROR.
                last = self._maint_off_retry.get(ent, 0.0)
                if now - last < MAINT_PUMP_RETRY_S:
                    continue
                self._maint_off_retry[ent] = now
                _LOGGER.warning(
                    "%s > %d s aan tijdens onderhoud — uitgezet. "
                    "Lege kuip? Schakel de groep uit in de meterkast.",
                    ent,
                    MAINT_PUMP_GRACE_S,
                )
                await self._async_call("fan", "turn_off", {"entity_id": ent})
            for ent in (self.conf["pump_fan"], *MIX_PUMPS):
                if ent not in pumps_on:
                    self._since_true(f"maint_pump_grace_{ent}", False, 0)
            # Watercare handhaven: de pack (of iemand) kan 'm verzetten
            # tijdens een check-cyclus — na de grace-periode terug op de
            # bewaarde standby-stand.
            saved = self.conf.get(CONF_MAINT_SAVED) or {}
            standby = saved.get("standby_mode")
            sel = self._state("watercare_select")
            if (
                standby
                and sel is not None
                and sel.state not in ("unavailable", "unknown")
                and sel.state != standby
                and self._since_true("maint_wc_grace", True, MAINT_PUMP_GRACE_S)
            ):
                _LOGGER.warning(
                    "Watercare stond op %s tijdens onderhoud — terug naar %s",
                    sel.state,
                    standby,
                )
                await self._set_watercare_opt(standby)
            elif standby and sel is not None and sel.state == standby:
                self._since_true("maint_wc_grace", False, 0)
            return

        # Piekvenster stateful bijhouden: niet op de exacte tijd-triggers
        # vertrouwen (die missen als HA down/crasht op dat moment) maar op
        # de overgang in_venster <-> actief per tick.
        in_peak = self._in_peak_window()
        if in_peak and not self._peak_active and not self._early_restored:
            await self._on_peak_start(None)  # zet _peak_active zelf
        elif in_peak and self._peak_active:
            # Vervroegd einde op PV-overschot: stoken op zonnestroom ipv
            # wachten op de klok. _early_restored voorkomt re-entry.
            solar_entity = self.conf.get(CONF_SOLAR_SENSOR, "")
            if solar_entity:
                solar = self.hass.states.get(solar_entity)
                surplus = (
                    solar is not None
                    and solar.state not in ("unavailable", "unknown")
                    and _float(solar) >= self.conf.get(CONF_SOLAR_MIN_W, 2000)
                )
                if self._since_true("solar_surplus", surplus, SOLAR_SURPLUS_S):
                    _LOGGER.info(
                        "PV-overschot >= %s W — piekblokkade vervroegd beëindigd",
                        self.conf.get(CONF_SOLAR_MIN_W, 2000),
                    )
                    self._early_restored = True
                    await self._on_peak_end(None)
        elif not in_peak:
            self._early_restored = False
            if self._peak_active:
                await self._on_peak_end(None)  # zet _peak_active zelf
            # NB: de oude 'startup restore'-check is vervallen — een
            # vloer-setpoint buiten piek is nu ook de legitieme
            # demand-onderdrukking. Als er warmtevraag is herstelt het
            # onderdrukkings-blok vanzelf; zoniet is laag correct.

        poolex_off = poolex.state == "off"

        # Altijd-aan guard: buiten de piek mag de Poolex nooit 'off' zijn —
        # vorstbeveiliging/telemetrie van de unit vereisen standby (heat).
        # Debounce: de staat moet 60 s aanhouden, zodat een korte
        # off-transitie (tuya-sync, app) geen pingpong geeft.
        # De guard wordt hieronder pas uitgevoerd NA sensor- en remcontrole;
        # een noodstop of onbetrouwbare meting mag nooit auto-aan worden.

        # Alleen climate.jacuzzi_tub_target schrijft het gebruikersdoel.
        # Een externe/stale Poolex-echo verandert NOOIT doel of remstatus.
        live_sp = _attr_float(poolex, "temperature", 99.0)
        setpoint = self._heat_setpoint
        tub_temp = _attr_float(jacuzzi, "current_temperature", float("nan"))
        duty = _float(compressor, float("nan"))
        compressor_valid = (
            self._fresh(compressor) and math.isfinite(duty) and 0 <= duty <= 100
        )
        # Onbekend is NIET uit. Houd circulatie vast totdat uit bevestigd is.
        compressor_on = duty > 0 if compressor_valid else None
        if compressor_on:
            self._comp_last_on = time.monotonic()
            self._compressor_recently_on = True
        self.compressor_on = compressor_on
        pump_on = pump.state == "on"
        problem_active = problem is not None and problem.state == "on"
        # tub_temp kan 0.0 rapporteren tijdens RF-uitval (vessel
        # DISCONNECTED) — dat is geen echte vraag. Ook 'unavailable'
        # telt niet: stale data is geen bewijs van koud water.
        tub_valid = (
            self._fresh(jacuzzi)
            and math.isfinite(tub_temp)
            and (1.0 <= tub_temp <= 45.0)
        )
        # MEETREGEL: Gecko meet de kuip; Poolex-inlaat leest structureel te
        # laag (35 bij kuip 44.5 °C). Ook inlaat+comp en kuip−ΔT zijn geen
        # betrouwbare veiligheidsmetingen. Bij kuipuitval NOOIT daarop
        # doorstoken: remmen, alarm, circulatie voor restwarmte behouden.
        demand_temp, demand_valid, bron = tub_temp, tub_valid, "kuip"
        sensors_ok = (
            tub_valid
            and compressor_valid
            and self._fresh(poolex)
            and poolex.state in ("heat", "off")
            and math.isfinite(live_sp)
            and 4.0 <= live_sp <= MAX_TUB_C
            and self._fresh(pump)
            and pump.state in ("on", "off")
            and self._fresh(problem)
            and problem.state in ("on", "off")
        )
        margin = self._option_float("temp_margin", 0.5)
        overshoot = self._option_float(CONF_OVERSHOOT, DEFAULT_OVERSHOOT_K)
        settings_ok = (
            math.isfinite(margin)
            and 0.1 <= margin <= 5.0
            and (math.isfinite(overshoot) and 0.0 <= overshoot <= 2.0)
        )
        peak_block = self._peak_active or (in_peak and not self._early_restored)
        stop_temp = self.stop_temperature
        guard_temp = min(MAX_TUB_C, stop_temp + OVERHEAT_MARGIN_K)
        # Ook een extreme numerieke kuipwaarde (>45) mag geen fallback
        # activeren: deze waarde blijft een reden voor een onmiddellijke stop.
        overheated = math.isfinite(tub_temp) and tub_temp >= guard_temp
        safety_reason = (
            "oververhitting"
            if overheated
            else "sensoruitval"
            if not sensors_ok
            else "ongeldige instellingen"
            if not settings_ok
            else "hvac-uit"
            if not self.heating_enabled
            else "piekblokkade"
            if peak_block
            else "geen circulatie"
            if not pump_on and compressor_on is True
            else "flow-storing"
            if problem_active or self._flow_lockout
            else "stop niet bevestigd"
            if self._brake_lockout
            else ""
        )
        self.demand_temp = demand_temp if demand_valid else None
        self.demand_bron = (
            "meng-check"
            if self._verify_until and time.monotonic() < self._verify_until
            else bron
        )
        # Echte hysterese: de vraag ontstaat onder doel-marge, maar een
        # lopende (niet-geremde) run stookt door tot doel + overshoot —
        # anders convergeert de kuip op doel-marge i.p.v. doel, en na
        # gebruik/wachttijd is doel zelf al te krap. Gevolg:
        # `not warmtevraag` betekent "doel+overshoot bereikt" zolang wij
        # niet remmen, en "bulk nog te warm om te hervatten" als we dat
        # wel doen — beide zijn precies wat de takken hieronder vragen.
        # Een expliciete run-latch, niet 'rem uit': startup in de dode
        # band is GEEN lopende run. De stopgrens gaat altijd vóór timers.
        self.warmtevraag = not safety_reason and (
            demand_temp < setpoint - margin
            or (self._run_active and demand_temp < stop_temp)
        )
        self._track_cooldown(tub_temp, tub_valid, pump_on, compressor_on is not False)

        # Een hoog Gecko-setpoint kan het eigen elektrische element laten
        # doorstoken terwijl Poolex al geremd is. Bij stop/sensoruitval/uit
        # ook dat doel laag zetten; de gewone lage pack-stand (<=18) laten.
        live_gecko_target = _attr_float(jacuzzi, "temperature", float("nan"))
        gecko_stop = bool(safety_reason) or (tub_valid and tub_temp >= stop_temp)
        if gecko_stop and (not math.isfinite(live_gecko_target) or live_gecko_target > GECKO_MAINT_TARGET_C) and (
            time.monotonic() - self._guard_gecko_at >= ACTUATOR_RETRY_S
        ):
            self._guard_gecko_at = time.monotonic()
            ok = await self._async_call("climate", "set_temperature", {
                "entity_id": self.conf["jacuzzi_climate"], "temperature": self.maintenance_gecko_target(),
            })
            self._notify_once("gecko_heat_stop", not ok, "Jacuzzi: Gecko-warmtevraag niet geremd",
                              "Hoge/onbekende Gecko-vraag kon niet laag worden gezet; controleer het element.")

        # Demand-remming: de Poolex regelt zijn compressor zélf op DP16,
        # maar die leest te laag (zie compensatie hierboven) -> de unit
        # zou doorstoken (waargenomen: kuip 44.5 °C bij doel 38 °C). Wij
        # zijn de thermostaat: vraag weg -> setpoint naar de vloer; de
        # unit blijft normaal op 'heat' met lage actuator-vraag. Bij
        # uitblijvende stopbevestiging handhaven we 15 °C en alarmeren.
        # HERSTART wacht normaal op 30 min rust; startup/reload op 30 s.
        # STOP wacht nooit. Poolex blijft altijd aan.
        # De kuip-stop en sensorcontrole gaan altijd vóór op dwell.
        # Inlaatcompensatie speelt geen rol in toestemming tot verwarmen.
        # De kuip leest de echte bulk (niet retour+ΔT — recorder-data),
        # dus de grens ligt kort boven de normale eindstand
        # (doel + overshoot): +OVERHEAT_MARGIN_K erboven = fout.
        # guard_temp is hierboven absoluut begrensd; retour-offset is
        # alleen diagnostiek en verhoogt NOOIT stop- of noodgrens.

        # Meng-check: na een vraag-stop een paar min circuleren+jets,
        # dan pas de echte (gemengde) bulk evalueren. Einde venster:
        # vraag -> hervatten mag (zie restore-blok), geen vraag ->
        # definitief klaar. Piek/onderhoud/hvac-off breekt af.
        # Onderbroken circulatie of opnieuw draaiende compressor maakt
        # een timer-afloop ongeschikt als bewijs van gemengde bulk.
        if self._verify_until and (not pump_on or compressor_on is not False):
            self._verify_until = time.monotonic() + VERIFY_MIX_S
        if self._verify_until and time.monotonic() >= self._verify_until:
            self._verify_until = 0.0
            # Lerende retour-offset: kuip-las bij de stop minus de nu
            # gemengde bulk = de werkelijke stratificatie/retour-marge
            # van deze run. EMA (0.3) dempt uitschieters; geclamped.
            if self._verify_tub_at_stop is not None and tub_valid and not self._mix_check_failed:
                measured = self._verify_tub_at_stop - tub_temp
                if 0.0 <= measured < 15.0:
                    self._retour_offset = min(
                        max(
                            0.7 * self._retour_offset + 0.3 * measured,
                            RETOUR_GUARD_MIN_K,
                        ),
                        RETOUR_GUARD_MAX_K,
                    )
                    _LOGGER.info(
                        "Retour-offset geleerd: %.1f K (gemeten %.1f K)",
                        self._retour_offset,
                        measured,
                    )
            self._verify_tub_at_stop = None
            if self.warmtevraag:
                if (
                    self._verify_resumes < VERIFY_MAX_RESUMES
                    and not self._mix_check_failed
                ):
                    self._verify_pending = True
                else:
                    _LOGGER.info(
                        "Meng-check: nog vraag (%.1f, bron=%s) maar "
                        "hervat-limiet bereikt — rem blijft tot dwell",
                        demand_temp or 0.0,
                        bron,
                    )
            else:
                _LOGGER.info(
                    "Meng-check: bulk %.1f °C op temperatuur (bron=%s) — sessie klaar",
                    demand_temp or 0.0,
                    bron,
                )
                self._verify_pending = False
                self._verify_resumes = 0
        if (
            peak_block
            or not sensors_ok
            or not self.heating_enabled
            or self.conf.get(CONF_MAINTENANCE)
        ):
            self._verify_until = 0.0
            self._verify_pending = False
            self._verify_tub_at_stop = None

        now = time.monotonic()
        if safety_reason:
            self._verify_until = 0.0
            self._verify_pending = False
            self._verify_tub_at_stop = None
        if not self.warmtevraag:
            # Onmiddellijk remmen op stopgrens, sensoruitval, hvac uit of
            # storing. Er is bewust GEEN minimale looptijd voor een stop.
            if self._run_active and not safety_reason and self._compressor_recently_on:
                self._verify_tub_at_stop = tub_temp
            self._run_active = False
            if not self._demand_suppressed:
                self._suppress_at = now
                self._rest_s = SP_SUPPRESS_REST_S
            self._demand_suppressed = True
            self._suppress_reason = safety_reason or "anti-pendel"
        if poolex_off:
            # Standby herstellen mag pas nadat het lage setpoint bevestigd
            # is; nooit 'heat' inschakelen met een oud hoog setpoint.
            self._demand_suppressed = True
            self._run_active = False
        brake_confirmed = False
        if self._demand_suppressed:
            brake_confirmed = await self._brake()

        # Meng-check begint pas NA bevestigde compressorstop. De timer
        # alleen bewijst geen menging: circulatie moet beschikbaar/aan zijn.
        if (
            self._verify_tub_at_stop is not None
            and not self._verify_until
            and brake_confirmed
            and not safety_reason
            and pump_on
        ):
            self._verify_until = now + VERIFY_MIX_S
            self._mix_check_failed = False
            _LOGGER.info("Run gestopt — %d min circuleren+mengen", VERIFY_MIX_S // 60)
            if self._mix_entity is None and not any(
                self.hass.states.is_state(p, "on") for p in MIX_PUMPS
            ):
                target = MIX_PUMPS[self._mix_next]
                self._mix_next = (self._mix_next + 1) % len(MIX_PUMPS)
                self._mix_entity = target
                self._mix_until = now + float(
                    self.conf.get(CONF_MIX_PULSE_S, DEFAULT_MIX_PULSE_S)
                )
                if not await self._async_call("fan", "turn_on", {"entity_id": target}):
                    self._mix_entity = None
                    self._mix_check_failed = True
                    self._verify_pending = False
                    self._notify_once(
                        "mix_failed",
                        True,
                        "Jacuzzi: meng-check mislukt",
                        "Mengpomp kon niet starten; geen vervroegde hervatting.",
                    )

        can_restore = not poolex_off and (
            not self._demand_suppressed
            or (
                brake_confirmed
                and not self._verify_until
                and (
                    now - self._suppress_at >= self._rest_s
                    or (
                        self._verify_pending
                        and self._verify_resumes < VERIFY_MAX_RESUMES
                    )
                )
            )
        )
        if self.warmtevraag and can_restore and pump_on and not self._brake_lockout:
            # Actuator volgt ons doel, nooit andersom. Ook een doelwijziging
            # tijdens een run moet naar Poolex worden geschreven.
            if (
                abs(live_sp - setpoint) > 0.1
                and now - self._actuator_at >= ACTUATOR_RETRY_S
            ):
                self._actuator_at = now
                if not await self._async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.conf["poolex_climate"],
                        "temperature": self._heat_setpoint,
                    },
                ):
                    self._brake_lockout = True
                    self._demand_suppressed = True
                    self.warmtevraag = False
                    await self._brake()
            if not self._brake_lockout:
                self._run_active = True
                self._rest_s = SP_SUPPRESS_REST_S
                self._demand_suppressed = False
                self._suppress_reason = ""
                self._brake_since = None
                if self._verify_pending:
                    self._verify_resumes += 1
                    self._verify_pending = False

        # Remfout is gelatcht: nooit opnieuw een hoog warmteverzoek na een
        # falende stop. Standby op 15 °C blijft behouden, Poolex nooit off.
        if (
            self.conf.get(CONF_POOLEX_ALWAYS_ON, DEFAULT_POOLEX_ALWAYS_ON)
            and safety_reason in ("", "hvac-uit")
            and not self._brake_lockout
            and self._fresh(poolex)
            and live_sp <= POOLEX_SETPOINT_FLOOR + 0.1
            and compressor_on is False
            and self._since_true("poolex_off_guard", poolex_off, POOLEX_OFF_GUARD_S)
        ):
            await self._async_call(
                "climate",
                "set_hvac_mode",
                {
                    "entity_id": self.conf["poolex_climate"],
                    "hvac_mode": "heat",
                },
            )

        self._notify_once(
            "overheat",
            overheated,
            "Jacuzzi: oververhitting",
            f"Kuip {tub_temp:.1f} °C bereikt noodgrens {guard_temp:.1f} °C — rem gevraagd.",
        )
        self._notify_once(
            "settings_invalid",
            not settings_ok,
            "Jacuzzi: instellingen ongeldig",
            "Vraag-marge/doorstook ongeldig — verwarming geblokkeerd.",
        )
        self._notify_once(
            "sensor_invalid",
            not sensors_ok,
            "Jacuzzi: meting onbetrouwbaar",
            "Verwarming geblokkeerd: kuip/actuator/compressor/pomp/storingsmeting ontbreekt of is verouderd.",
        )

        # Flow-fault lockout: pomp aan + fault-bit lang aanhoudend = er
        # komt echt geen water door (lek tussen pomp en flowmeter, of
        # een lege kuip). Doordraaien loost de kuip leeg of laat de
        # pomp drooglopen. Eenmalig ingrijpen: de Gecko zet de pomp
        # zelf terug (eigen priming/filter-logica, 'non-user initiators')
        # — daarom esaleren we naar watercare 'Away': dan stopt de pack
        # ook met eigen cycli. Onbekende fault-status geeft niets vrij.
        # De noodstop vereist een bewuste herstelactie; reset niet op pump_on.
        if not self._flow_lockout and self._since_true(
            "flow_fault", pump_on and problem_active, FLOW_FAULT_OFF_S
        ):
            _LOGGER.warning(
                "Flow-fault >%d s bij draaiende pomp — pomp uit + watercare Away",
                FLOW_FAULT_OFF_S,
            )
            # Eerst 15 °C blijven vragen; nooit Poolex uitschrijven.
            # De pomp-stop is hier een droogloop/lek-uitzondering, niet
            # het normale temperatuur-stopbeleid met nakoeling.
            self._run_active = False
            self.warmtevraag = False
            self._demand_suppressed = True
            await self._brake()
            await self._async_call(
                "fan", "turn_off", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = False
            sel = self._state("watercare_select")
            if sel is not None and sel.state not in (
                "unavailable",
                "unknown",
                "Away",
            ):
                self._flow_saved_watercare = sel.state
                await self._set_watercare_opt("Away")
            self._flow_lockout = True
            self._brake_lockout = True
            self._suppress_reason = "flow-storing"
            self._notify_once(
                "flow_fault",
                True,
                "Jacuzzi: flow-fault",
                "De Poolex meldt al 5 min een flow-fout terwijl de "
                "circulatiepomp draait — pomp uit + watercare op Away "
                "(lek of lege kuip?). Auto-aanzet blijft geblokkeerd; "
                "controleer de oorzaak vóór een bewuste herstelactie.",
            )
        elif self._fresh(problem) and problem.state == "off":
            if self._flow_lockout and self._flow_saved_watercare is not None:
                _LOGGER.info(
                    "Flow-fault weg — watercare terug naar %s",
                    self._flow_saved_watercare,
                )
                await self._set_watercare_opt(self._flow_saved_watercare)
            self._flow_saved_watercare = None
            self._flow_lockout = False
            self._notify_once("flow_fault", False, "", "")

        # Onthoud dat de compressor echt gedraaid heeft — de nadraai-timer
        # (comp_idle) mag alleen tellen ná een echte run, anders is
        # 'compressor al 3 min uit' permanent waar en slaat de pomp direct af.
        if compressor_on:
            self._compressor_recently_on = True
            self._since.pop("comp_idle", None)

        # Flow blijft nodig bij compressorbedrijf, nakoeling én mengen.
        # Gecko kan de pomp zelf uitzetten: op een bevestigde 'off' meteen
        # herstellen, zonder 15 s wachttijd of gewone 30 s debounce.
        # Onbekende pompstatus is geen bewijs dat de pomp fysiek uit staat.
        compressor_cooled = self._since_true(
            "compressor_stopped", compressor_on is False, PUMP_RUNON_S
        )
        outlet_st = self.hass.states.get(POOLEX_OUTLET_SENSOR)
        outlet_value = _float(outlet_st, float("nan"))
        known_outlet_hot = self._fresh(outlet_st) and math.isfinite(outlet_value) and outlet_value > POST_HEAT_OUTLET_C
        outlet_hot = not self._fresh(outlet_st) or not math.isfinite(outlet_value) or known_outlet_hot
        cooling_needed = (self._compressor_recently_on and not compressor_cooled) or known_outlet_hot
        mixing_needed = (bool(self._verify_until) or (
            self._verify_tub_at_stop is not None and brake_confirmed
        )) and not safety_reason
        circulation_needed = compressor_on is True or cooling_needed or mixing_needed
        if (
            circulation_needed
            and pump.state == "off"
            and not self._flow_lockout
            and self._pump_cmd_ready(urgent=True)
        ):
            _LOGGER.warning("Circulatie nodig maar Gecko-pomp staat uit — pomp herstellen")
            restored = await self._async_call(
                "fan", "turn_on", {"entity_id": self.conf["pump_fan"]}
            )
            if not restored:
                # Ook als de compressor inmiddels idle meldt, een oud hoog
                # actuator-doel niet laten staan bij mislukte flow-aanzet.
                self.warmtevraag = False
                self._run_active = False
                self._demand_suppressed = True
                self._suppress_reason = "circulatieherstel mislukt"
                await self._brake()
                self._notify_once("pump_restore_failed", True, "Jacuzzi: circulatieherstel mislukt",
                                  "Benodigde circulatie kon niet worden hersteld; warmteverzoek blijft geremd.")
            else:
                self.pump_by_us = True
                self._notify_once("pump_restore_failed", False, "", "")
            # De Gecko-pack dropt de pomp af en toe zelf (blijkt normaal
            # gedrag: hij zet 'm aan zonder dat wij het zien, en uit).
            # Een enkele dip = ruis; pas bij herhaling melden.
            now = time.monotonic()
            self._failsafe_hits = [
                t for t in self._failsafe_hits if now - t < FAILSAFE_NOTIFY_WINDOW_S
            ]
            if restored:
                self._failsafe_hits.append(now)
            if len(self._failsafe_hits) >= FAILSAFE_NOTIFY_MIN:
                self._notify_once(
                    "failsafe",
                    True,
                    "Jacuzzi: pomp dipjes",
                    f"De circulatiepomp ging {len(self._failsafe_hits)}x uit "
                    f"terwijl circulatie nodig was (laatste "
                    f"{FAILSAFE_NOTIFY_WINDOW_S // 60} min) — aan gevraagd. "
                    "De pack lijkt de pomp te toggelen; houd de flow in de gaten.",
                )
            else:
                _LOGGER.info(
                    "Pomp-dip %d/%d binnen %d min — herstelpoging, geen herhaalde-dipmelding",
                    len(self._failsafe_hits),
                    FAILSAFE_NOTIFY_MIN,
                    FAILSAFE_NOTIFY_WINDOW_S // 60,
                )
        else:
            self._notify_once("failsafe", False, "", "")

        # Pomp AAN bij warmtevraag (onmiddellijk, zoals template-trigger).
        # Niet tijdens piek: de compressor mag dan toch niet, circulatie
        # heeft geen functie en zou anders de hele blokperiode draaien.
        if (
            (
                (self.warmtevraag and can_restore)
                or (bool(self._verify_until) and not safety_reason)
            )
            and not pump_on
            and not peak_block
            and not self._flow_lockout
            and self._pump_cmd_ready(urgent=True)
        ):
            _LOGGER.info(
                "Warmtevraag/meng-check (%.1f, doel %.1f, bron=%s) — pomp aan",
                demand_temp,
                setpoint,
                bron,
            )
            if await self._async_call(
                "fan", "turn_on", {"entity_id": self.conf["pump_fan"]}
            ):
                self.pump_by_us = True

        # Zelfs als de kuip al langer op temperatuur is, minstens 3 min
        # circuleren NA bevestigde compressorstop. Een oude temp-timer
        # mag deze nakoeling niet overslaan. compressor_cooled wordt
        # hierboven continu bijgehouden, ook bij weggevallen circulatie.
        # Pomp UIT: een van de drie herkansingspaden is lang genoeg waar.
        # comp_idle telt alleen als de compressor echt heeft gedraaid —
        # nadraaien na een run, geen 'al eeuwen idle'.
        off_due = (
            self._since_true(
                "temp_reached",
                demand_valid and demand_temp >= stop_temp,
                PUMP_RUNON_S,
            )
            or self._since_true("poolex_off", poolex_off, PUMP_RUNON_S)
            or self._since_true(
                "comp_idle",
                compressor_on is False and self._compressor_recently_on,
                PUMP_RUNON_S,
            )
            # Piekblokkade: geen vraag-circulatie — de compressor mag
            # toch niet. Een net gestopte run blijft via outlet_hot
            # nakoelen; handmatige circulatie laten we met rust
            # (off vereist pump_by_us).
            or self._since_true(
                "peak_block", in_peak and not compressor_on, PUMP_RUNON_S
            )
        )
        # Nakoeling: compressor uit maar de wisselaar is nog heet ->
        # pomp door laten draaien tot de uitlaat weer koel is. Zonder
        # flow stagneert die restwarmte (waargenomen: uitlaat-spike
        # naar 52 °C vlak na compressor-stop) en gaat de warmte
        # verloren in de behuizing i.p.v. de kuip.
        # outlet_hot is hierboven berekend; een onbekende uitlaat is
        # geen bewijs dat de wisselaar koel is.
        if outlet_hot and pump_on:
            self._afterheat_hold = True
        elif not pump_on:
            self._afterheat_hold = False
        verify_active = bool(self._verify_until) and (
            time.monotonic() < self._verify_until
        )
        if (
            off_due
            and pump_on
            and self.pump_by_us
            and compressor_on is False
            and compressor_cooled
            and not self.warmtevraag
            and not outlet_hot
            and not verify_active
            and self._pump_cmd_ready()
        ):
            _LOGGER.info(
                "Setpoint bereikt / geen vraag — pomp uit%s",
                " (piekblokkade)"
                if in_peak
                else " (na nakoeling uitlaat)"
                if self._afterheat_hold
                else "",
            )
            await self._async_call(
                "fan", "turn_off", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = False
            self._compressor_recently_on = False
            self._afterheat_hold = False

        # Meng-puls: tijdens het stoken afwisselend een massagepomp
        # kort aanzetten — roert de gestratificeerde lagen door elkaar
        # zodat kuip- en inlaat-sensor de echte bulk-temp zien.
        # Alleen als de compressor draait én er circulatie is.
        now_mono = time.monotonic()
        mix_enabled = self.conf.get(CONF_MIX_ENABLED, DEFAULT_MIX_ENABLED)
        mix_interval = (
            float(self.conf.get(CONF_MIX_INTERVAL_MIN, DEFAULT_MIX_INTERVAL_MIN)) * 60
        )
        mix_pulse = float(self.conf.get(CONF_MIX_PULSE_S, DEFAULT_MIX_PULSE_S))
        if self._mix_entity is not None:
            if not mix_enabled or now_mono >= self._mix_until:
                entity = self._mix_entity
                _LOGGER.info("Meng-puls klaar — %s uit", entity)
                if await self._async_call("fan", "turn_off", {"entity_id": entity}):
                    self._mix_entity = None
                else:
                    # Eigenaarschap niet verliezen bij een gefaalde stop.
                    self._mix_until = now_mono + ACTUATOR_RETRY_S
        elif any(self.hass.states.is_state(p, "on") for p in MIX_PUMPS):
            # een massagepomp draait (gebruiker/Gecko-filter) — er wordt
            # al gemengd; het interval telt vanaf het einde daarvan
            self._mix_last = now_mono
        elif (
            mix_enabled
            and compressor_on
            and pump_on
            and self._run_active
            and not safety_reason
            and now_mono - self._mix_last >= mix_interval
        ):
            target = MIX_PUMPS[self._mix_next]
            # Eerst markeren, dán pas de call — _async_evaluate kan tijdens
            # de await opnieuw starten (state-change per tick) en zou de
            # puls anders meerdere keren vuren.
            self._mix_entity = target
            self._mix_until = now_mono + mix_pulse
            self._mix_next = (self._mix_next + 1) % len(MIX_PUMPS)
            self._mix_last = now_mono
            if await self._async_call("fan", "turn_on", {"entity_id": target}):
                _LOGGER.info(
                    "Meng-puls: %s %d s aan (compressor actief)",
                    target,
                    mix_pulse,
                )
            else:
                _LOGGER.warning("Meng-puls mislukt voor %s", target)
                self._mix_entity = None

        # Monitor 0b: water te koud maar hvac_mode staat op 'off' — de
        # warmtevraag wordt dan bewust onderdrukt en er gebeurt zichtbaar
        # niets. Meld dat ipv stil niets te doen.
        self._notify_once(
            "poolex_off_cold",
            self._since_true(
                "poolex_off_cold",
                poolex_off
                and tub_valid
                and tub_temp < setpoint - margin
                and not self._peak_active,
                NO_COMPRESSOR_S,
            ),
            "Jacuzzi: Poolex staat uit",
            f"Het water is {tub_temp} °C maar de warmtepomp staat op off — "
            "zet hem op heat (switch Poolex aan/uit) om weer te stoken.",
        )

        # Monitor 1: echte fault-bit van de unit. Alleen melden als de
        # circulatiepomp aan staat: pomp uit + flow-fault is verwacht
        # (er stroomt dan toch niets); een vertraagde d1 na het stoppen
        # wordt zo ook automatisch onderdrukt. De fault blijft wel
        # zichtbaar op de sensor/entities-kaart.
        self._notify_once(
            "problem",
            self._since_true("problem", problem_active and pump_on, PROBLEM_DELAY_S),
            "Jacuzzi: Poolex fault",
            "De Poolex rapporteert een probleem (problem-sensor aan). "
            "Check de unit — bij d1: te weinig doorstroom, bypass verder dichtknijpen.",
        )

        # Monitor 2: warmtevraag zonder compressor = flow-error proxy.
        # Niet tijdens demand-onderdrukking, piekblokkade of
        # flow-lockout: dan is de compressor bewust stil.
        self._notify_once(
            "no_compressor",
            self._since_true(
                "no_compressor",
                self.warmtevraag
                and not compressor_on
                and not self._demand_suppressed
                and not in_peak
                and not self._peak_active
                and not self._flow_lockout,
                NO_COMPRESSOR_S,
            ),
            "Jacuzzi: Poolex mogelijk in storing",
            f"De warmtepomp staat aan en het water is te koud ({tub_temp} °C), "
            "maar de compressor draait niet — waarschijnlijk een flow-error. "
            "Draait de circulatiepomp? Check de unit.",
        )

        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
