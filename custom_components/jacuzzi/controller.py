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
    CONF_PEAK_END,
    CONF_PEAK_MODE,
    CONF_PEAK_SETPOINT,
    CONF_PEAK_START,
    CONF_POOLEX_ALWAYS_ON,
    CONF_SOLAR_MIN_W,
    CONF_SOLAR_SENSOR,
    DEFAULT_MIX_ENABLED,
    DEFAULT_MIX_INTERVAL_MIN,
    DEFAULT_MIX_PULSE_S,
    DEFAULT_POOLEX_ALWAYS_ON,
    DOMAIN,
    EVAL_INTERVAL_S,
    FAILSAFE_DELAY_S,
    FLOW_FAULT_OFF_S,
    MAINT_PUMP_GRACE_S,
    MAINT_PUMP_RETRY_S,
    MIX_PUMPS,
    NO_COMPRESSOR_S,
    PEAK_MODE_OFF,
    POOLEX_OFF_GUARD_S,
    PROBLEM_DELAY_S,
    PUMP_RUNON_S,
    RELOAD_COOLDOWN_S,
    SOLAR_SURPLUS_S,
    UNREACHABLE_S,
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
        self.pump_by_us = False          # wij hebben de pomp aangezet
        self.warmtevraag = False          # warmte gevraagd (voor sensor)
        self._compressor_recently_on = False  # compressor heeft gedraaid (nadraai)
        self._peak_saved_setpoint: float | None = None  # setpoint vóór de piek
        self._since: dict[str, datetime] = {}
        self._notified: set[str] = set()
        self._last_gecko_reload = 0.0
        self._mix_last = 0.0             # monotonic ts laatste meng-puls
        self._mix_until = 0.0            # monotonic ts einde lopende puls
        self._mix_next = 0               # index in MIX_PUMPS (wisselend)
        self._mix_entity: str | None = None  # pomp die nu pulseert
        self._peak_active = False        # piek-blok is toegepast
        self._early_restored = False     # PV-einde al gedaan dit venster
        self._startup_restore_checked = False  # eenmalig: achtergebleven
        #                                      piek-setpoint herstellen
        self._flow_lockout = False       # flow-fault: geen auto-aanzet pomp
        self._maint_off_retry: dict[str, float] = {}  # backoff per pomp
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

    async def _async_call(self, domain: str, service: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
            return True
        except Exception as err:  # noqa: BLE001 - log en ga door
            _LOGGER.error("%s.%s faalde: %s", domain, service, err)
            return False

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
                self.hass, self._on_peak_start, hour=hh, minute=mm, second=0
            )
        )
        hh, mm = _parse_hhmm(self.conf[CONF_PEAK_END])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_peak_end, hour=hh, minute=mm, second=0
            )
        )
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

        'setpoint'-mode (default): setpoint naar het dieptepunt — de unit
        blijft aan (telemetrie + vorstbeveiliging blijven werken) maar
        de compressor krijgt nooit vraag. 'off'-mode: hvac_mode uit.
        """
        _LOGGER.info("Piekblokkade AAN (%s)", self.conf[CONF_PEAK_START])
        poolex = self._state("poolex_climate")
        if poolex is None or poolex.state in ("unavailable", "unknown"):
            # _peak_active blijft False -> evaluate probeert het opnieuw
            _LOGGER.warning("Piekblokkade AAN uitgesteld — Poolex unavailable")
            return
        ok = True
        if self.conf.get(CONF_PEAK_MODE) == PEAK_MODE_OFF:
            ok = await self._async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.conf["poolex_climate"], "hvac_mode": "off"},
            )
        else:
            # clamp: de unit accepteert in heat-mode min. ~15 °C
            # (tuya-local validatie). min_temp-attr rapporteert het
            # DP-minimum (~4 °C) — lager dan de service-floor. Daarom
            # harde floor op 15 °C, ongeacht de attr.
            peak = max(
                self.conf.get(CONF_PEAK_SETPOINT, 4.0),
                _attr_float(poolex, "min_temp", 15.0),
                15.0,
            )
            current = _attr_float(poolex, "temperature", 38.0)
            # niet overschrijven als we midden in de piek (her)starten:
            # vergelijk met het GECLAMPTE piek-niveau, niet de config
            if current > peak + 0.5:
                self._peak_saved_setpoint = current
            ok = await self._async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.conf["poolex_climate"],
                    "temperature": peak,
                },
            )
        ok = await self._set_watercare("watercare_peak") and ok
        # False -> evaluate roept _on_peak_start de volgende tick opnieuw
        self._peak_active = ok

    async def _on_peak_end(self, _now) -> None:
        """Piekblokkade UIT: setpoint/mode herstellen + Watercare normaal."""
        _LOGGER.info("Piekblokkade UIT (%s)", self.conf[CONF_PEAK_END])
        poolex = self._state("poolex_climate")
        if poolex is None or poolex.state in ("unavailable", "unknown"):
            # _peak_active blijft True -> evaluate probeert het opnieuw
            _LOGGER.warning("Piekblokkade UIT uitgesteld — Poolex unavailable")
            return
        ok = True
        if self.conf.get(CONF_PEAK_MODE) == PEAK_MODE_OFF:
            ok = await self._async_call(
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
            ok = await self._async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.conf["poolex_climate"],
                    "temperature": restore,
                },
            )
            if ok:
                self._peak_saved_setpoint = None
        ok = await self._set_watercare("watercare_normal") and ok
        # False -> evaluate roept _on_peak_end de volgende tick opnieuw
        self._peak_active = not ok

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

        # Onderhoudsmodus: alles handmatig — de controller doet niets.
        # Niet vechten met failsafe/altijd-aan/piek/menging; de gebruiker
        # bepaalt (bv. water verversen). Monitors hierboven lopen door.
        if self.conf.get(CONF_MAINTENANCE):
            self.warmtevraag = False
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
                if (st := self.hass.states.get(ent)) is not None
                and st.state == "on"
            ]
            self._notify_once(
                "maintenance_pump",
                bool(pumps_on),
                "Jacuzzi: pomp aan tijdens onderhoud",
                "De Gecko startte een circulatie-cyclus. Korte check-runs "
                "laten we; > 90 s wordt hij teruggezet. Bij een lege kuip: "
                "groep uitschakelen in de meterkast.",
            )
            for ent in pumps_on:
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
                and self._since_true(
                    "maint_wc_grace", True, MAINT_PUMP_GRACE_S
                )
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
            await self._on_peak_start(None)   # zet _peak_active zelf
        elif in_peak and self._peak_active:
            # Vervroegd einde op PV-overschot: stoken op zonnestroom ipv
            # wachten op de klok. _early_restored voorkomt re-entry.
            solar_entity = self.conf.get(CONF_SOLAR_SENSOR, "")
            if solar_entity:
                solar = self.hass.states.get(solar_entity)
                surplus = (
                    solar is not None
                    and solar.state not in ("unavailable", "unknown")
                    and _float(solar)
                    >= self.conf.get(CONF_SOLAR_MIN_W, 2000)
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
            elif not self._startup_restore_checked:
                # eenmalig: als de piek-einde-trigger ooit gemist is (HA
                # down) kan de Poolex op zijn piek-setpoint blijven staan
                # -> herstel
                self._startup_restore_checked = True
                cur = _attr_float(poolex, "temperature", 99.0)
                peak_sp = max(
                    self.conf.get(CONF_PEAK_SETPOINT, 4.0),
                    _attr_float(poolex, "min_temp", 15.0),
                    15.0,
                )
                if cur <= peak_sp + 0.5:
                    _LOGGER.warning(
                        "Poolex-setpoint %.1f = piek-niveau buiten piekvenster "
                        "(gemist piek-einde?) — herstellen naar normaal",
                        cur,
                    )
                    await self._on_peak_end(None)

        poolex_off = poolex.state == "off"

        # Altijd-aan guard: buiten de piek mag de Poolex nooit 'off' zijn —
        # vorstbeveiliging/telemetrie van de unit vereisen standby (heat).
        # Debounce: de staat moet 60 s aanhouden, zodat een korte
        # off-transitie (tuya-sync, app) geen pingpong geeft.
        if self.conf.get(
            CONF_POOLEX_ALWAYS_ON, DEFAULT_POOLEX_ALWAYS_ON
        ) and self._since_true(
            "poolex_off_guard",
            poolex_off and not in_peak and not self._peak_active,
            POOLEX_OFF_GUARD_S,
        ):
            _LOGGER.warning("Poolex stond op off — terug naar heat (always-on)")
            if await self._async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.conf["poolex_climate"], "hvac_mode": "heat"},
            ):
                self._notify_once(
                    "poolex_autoon",
                    True,
                    "Jacuzzi: Poolex stond uit",
                    "De warmtepomp stond op off — teruggezet naar heat "
                    "(Poolex altijd aan staat aan).",
                )
        else:
            self._notify_once("poolex_autoon", False, "", "")

        setpoint = _attr_float(poolex, "temperature", 38.0)
        tub_temp = _attr_float(jacuzzi, "current_temperature")
        compressor_on = _float(compressor) > 0
        pump_on = pump.state == "on"
        problem_active = problem is not None and problem.state == "on"
        self.warmtevraag = not poolex_off and tub_temp < setpoint - self.conf["temp_margin"]

        # Flow-fault lockout: pomp aan + fault-bit lang aanhoudend = er
        # komt echt geen water door (lek tussen pomp en flowmeter, of
        # een lege kuip). Doordraaien loost de kuip leeg of laat de
        # pomp drooglopen -> na FLOW_FAULT_OFF_S zetten we de pomp uit
        # en blokkeren we auto-aanzet tot de fault weg is, of tot
        # iemand de pomp bewust weer aanzet (pump_on -> reset).
        if pump_on:
            self._flow_lockout = False
        if self._since_true(
            "flow_fault", pump_on and problem_active, FLOW_FAULT_OFF_S
        ):
            _LOGGER.warning(
                "Flow-fault >%d s bij draaiende pomp — pomp uit (drooglopen/lek?)",
                FLOW_FAULT_OFF_S,
            )
            await self._async_call(
                "fan", "turn_off", {"entity_id": self.conf["pump_fan"]}
            )
            self.pump_by_us = False
            self._flow_lockout = True
            self._notify_once(
                "flow_fault", True, "Jacuzzi: flow-fault",
                "De Poolex meldt al 5 min een flow-fout terwijl de "
                "circulatiepomp draait — pomp uitgezet (lek of lege kuip?). "
                "Auto-aanzet staat uit tot de storing weg is.",
            )
        elif not problem_active:
            self._notify_once("flow_fault", False, "", "")

        # Onthoud dat de compressor echt gedraaid heeft — de nadraai-timer
        # (comp_idle) mag alleen tellen ná een echte run, anders is
        # 'compressor al 3 min uit' permanent waar en slaat de pomp direct af.
        if compressor_on:
            self._compressor_recently_on = True
            self._since.pop("comp_idle", None)

        # FAILSAFE: compressor draait maar pomp staat uit -> restwarmte
        # kan niet weg -> d1. Pomp direct weer aan. Alleen ingrijpen als
        # de pomp echt 'off' is — unavailable/unknown (restart, cloud-
        # hapering) is geen bewijs dat er geen flow is.
        compressor_running = self._since_true(
            "comp_running", compressor_on, FAILSAFE_DELAY_S
        )
        if compressor_running and pump.state == "off" and not self._flow_lockout:
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
        if self.warmtevraag and not pump_on and not self._flow_lockout:
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

        # Meng-puls: tijdens het stoken afwisselend een massagepomp
        # kort aanzetten — roert de gestratificeerde lagen door elkaar
        # zodat kuip- en inlaat-sensor de echte bulk-temp zien.
        # Alleen als de compressor draait én er circulatie is.
        now_mono = time.monotonic()
        mix_enabled = self.conf.get(CONF_MIX_ENABLED, DEFAULT_MIX_ENABLED)
        mix_interval = float(
            self.conf.get(CONF_MIX_INTERVAL_MIN, DEFAULT_MIX_INTERVAL_MIN)
        ) * 60
        mix_pulse = float(self.conf.get(CONF_MIX_PULSE_S, DEFAULT_MIX_PULSE_S))
        if self._mix_entity is not None:
            if not mix_enabled or now_mono >= self._mix_until:
                entity = self._mix_entity
                self._mix_entity = None
                _LOGGER.info("Meng-puls klaar — %s uit", entity)
                await self._async_call(
                    "fan", "turn_off", {"entity_id": entity}
                )
        elif any(
            self.hass.states.is_state(p, "on") for p in MIX_PUMPS
        ):
            # een massagepomp draait (gebruiker/Gecko-filter) — er wordt
            # al gemengd; het interval telt vanaf het einde daarvan
            self._mix_last = now_mono
        elif (
            mix_enabled
            and compressor_on
            and pump_on
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
            if await self._async_call(
                "fan", "turn_on", {"entity_id": target}
            ):
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
                and tub_temp < setpoint - self.conf["temp_margin"]
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
            self._since_true(
                "problem", problem_active and pump_on, PROBLEM_DELAY_S
            ),
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
