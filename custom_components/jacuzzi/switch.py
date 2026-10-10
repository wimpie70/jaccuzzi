"""Switch platform: warmte toestaan/remmen; Poolex zelf NOOIT uitschakelen."""

from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import (
    CONF_COMPRESSOR_SENSOR,
    CONF_JACUZZI_CLIMATE,
    CONF_MAINT_SAVED,
    CONF_MAINTENANCE,
    CONF_MIX_ENABLED,
    CONF_NOTIFY_SERVICE,
    CONF_POOLEX_ALWAYS_ON,
    CONF_POOLEX_CLIMATE,
    CONF_PUMP_FAN,
    CONF_WATERCARE_PEAK,
    CONF_WATERCARE_SELECT,
    DEFAULT_MIX_ENABLED,
    DEFAULT_POOLEX_ALWAYS_ON,
    DOMAIN,
    MIX_PUMPS,
    POOLEX_SETPOINT_FLOOR,
    GECKO_MAINT_TARGET_C,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JacuzziConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Poolex on/off and always-on-guard switches."""
    async_add_entities(
        [
            JacuzziPoolexSwitch(entry),
            JacuzziAlwaysOnSwitch(entry),
            JacuzziMixSwitch(entry),
            JacuzziMaintenanceSwitch(entry),
        ]
    )


class JacuzziPoolexSwitch(SwitchEntity):
    """Warmtevraag toestaan/blokkeren; schakelt de Poolex-unit nooit uit."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_power"
    _attr_icon = "mdi:heat-pump"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poolex_power"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    def _poolex_entity(self) -> str:
        conf = {**self._entry.data, **self._entry.options}
        return conf[CONF_POOLEX_CLIMATE]

    @property
    def is_on(self) -> bool | None:
        """Warmtevraag toegestaan; de Poolex-unit zelf blijft altijd aan."""
        controller = self._entry.runtime_data
        return controller.heating_enabled and not controller.conf.get(CONF_MAINTENANCE, False)

    async def async_turn_on(self, **kwargs) -> None:
        """Sta verwarming toe; controller bewaakt sensoren en rusttijd."""
        self._entry.runtime_data.async_set_heating_enabled(True)
        await self._entry.runtime_data._async_evaluate()

    async def async_turn_off(self, **kwargs) -> None:
        """Zet alleen warmtevraag uit: rem 15 °C, NOOIT Poolex off."""
        self._entry.runtime_data.async_set_heating_enabled(False)
        await self._entry.runtime_data._async_evaluate()

    async def async_added_to_hass(self) -> None:
        """Subscribe op controller-updates én op de echte climate-state.

        De climate kan ook buiten de controller om veranderen (onderhoud-
        modus, tuya-app) — dan is SIGNAL_UPDATE niet genoeg.
        """
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._async_update)
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._poolex_entity(), self._async_state_change
            )
        )

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _async_state_change(self, event) -> None:
        self.async_write_ha_state()


class JacuzziAlwaysOnSwitch(SwitchEntity):
    """'Poolex altijd aan'-guard: buiten piek nooit hvac 'off' toestaan.

    Aan = controller zet de unit terug op heat als iets/iemand hem uit
    zet (vorstbeveiliging blijft dan werken). Schrijft naar options.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "poolex_always_on"
    _attr_icon = "mdi:shield-check"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{CONF_POOLEX_ALWAYS_ON}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def is_on(self) -> bool:
        """Guard aan? (options > data > default)."""
        return bool(
            self._entry.options.get(
                CONF_POOLEX_ALWAYS_ON,
                self._entry.data.get(
                    CONF_POOLEX_ALWAYS_ON, DEFAULT_POOLEX_ALWAYS_ON
                ),
            )
        )

    async def _set_option(self, value: bool) -> None:
        """Sla op in options; de update-listener reloadt."""
        options = dict(self._entry.options)
        options[CONF_POOLEX_ALWAYS_ON] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_option(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_option(False)


class JacuzziMixSwitch(SwitchEntity):
    """Meng-puls aan/uit: periodiek een massagepomp roeren tijdens stoken.

    Aan = controller zet elke N min kort een massagepomp aan zolang de
    compressor draait — mengt de gestratificeerde lagen zodat kuip- en
    inlaat-sensor de echte bulk-temperatuur zien. Schrijft naar options.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "mix_pulse_enabled"
    _attr_icon = "mdi:waves"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{CONF_MIX_ENABLED}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def is_on(self) -> bool:
        """Meng-puls aan? (options > data > default)."""
        return bool(
            self._entry.options.get(
                CONF_MIX_ENABLED,
                self._entry.data.get(CONF_MIX_ENABLED, DEFAULT_MIX_ENABLED),
            )
        )

    async def _set_option(self, value: bool) -> None:
        """Sla op in options; de update-listener reloadt."""
        options = dict(self._entry.options)
        options[CONF_MIX_ENABLED] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_option(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_option(False)


class JacuzziMaintenanceSwitch(SwitchEntity):
    """Onderhoudsmodus: warmte geblokkeerd, Poolex aan op 15 °C.

    Aan = bewaar de huidige standen (Poolex hvac-mode/setpoint, watercare)
    in options en zet alles in onderhoud: Poolex 15 °C, watercare op de
    standby/away-stand (of piek-stand als geen van beide bestaat),
    circulatie- en
    massagepompen uit. De controller
    handhaaft de 15 °C-rem maar start geen verwarming of menging.
    Uit = opgeslagen standen terugzetten; de controller hervat.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "maintenance"
    _attr_icon = "mdi:wrench-cog"

    def __init__(self, entry: JacuzziConfigEntry) -> None:
        """Bind aan de config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{CONF_MAINTENANCE}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Jacuzzi",
        }

    @property
    def is_on(self) -> bool:
        """Onderhoud actief? (options > data > default=False)."""
        return bool(
            self._entry.options.get(
                CONF_MAINTENANCE,
                self._entry.data.get(CONF_MAINTENANCE, False),
            )
        )

    def _conf(self) -> dict:
        return {**self._entry.data, **self._entry.options}

    async def _call(self, domain: str, service: str, data: dict) -> None:
        if domain == "climate" and data.get("hvac_mode") == "off":
            raise ValueError("Poolex mag niet op off; gebruik rem-setpoint 15 °C")
        await asyncio.wait_for(
            self.hass.services.async_call(domain, service, data, blocking=True), 10,
        )

    async def _try_call(self, domain: str, service: str, data: dict) -> bool:
        """Een falend onderhoudscommando mag de overige stopacties niet overslaan."""
        try:
            await self._call(domain, service, data)
            return True
        except Exception as err:
            _LOGGER.warning("Onderhoudscommando %s.%s mislukt: %s", domain, service, err)
            return False

    async def async_turn_on(self, **kwargs) -> None:
        """Bewaar de huidige standen en zet alles in onderhoud."""
        if self.is_on:
            return  # herhaald 'aan' mag het oorspronkelijke snapshot niet overschrijven
        conf = self._conf()
        poolex = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
        watercare = self.hass.states.get(conf[CONF_WATERCARE_SELECT])
        gecko = self.hass.states.get(conf[CONF_JACUZZI_CLIMATE])
        gecko_target = None if gecko is None else gecko.attributes.get("temperature")
        try:
            gecko_target = float(gecko_target)
            if not math.isfinite(gecko_target):
                gecko_target = None
        except (TypeError, ValueError):
            gecko_target = None
        # Watercare naar standby-achtige stand: zoek in de opties van de
        # select op voorkeursvolgorde ('standby' schort alles op, 'away'
        # houdt alleen vorstbewaking aan), anders de piek-stand.
        options_list = (
            [] if watercare is None else watercare.attributes.get("options", [])
        )
        standby = next(
            (
                o
                for pref in ("standby", "away")
                for o in options_list
                if pref in o.lower()
            ),
            conf[CONF_WATERCARE_PEAK],
        )
        options = dict(self._entry.options)
        options[CONF_MAINT_SAVED] = {
            "poolex_mode": None if poolex is None else poolex.state,
            "poolex_setpoint": (
                None
                if poolex is None
                else poolex.attributes.get("temperature")
            ),
            "watercare": None if watercare is None else watercare.state,
            "gecko_setpoint": gecko_target,
            # de gekozen standby-stand: handhaven tijdens onderhoud
            "standby_mode": standby,
        }
        options[CONF_MAINTENANCE] = True
        # Blokkeer ook de OUDE controller onmiddellijk; options-reload
        # is asynchroon en mag niet tussendoor de compressor herstellen.
        controller = self._entry.runtime_data
        controller.conf[CONF_MAINTENANCE] = True
        # Snapshot/vlag vóór de eerste await opslaan: ook gelijktijdig
        # opnieuw 'aan' moet het oorspronkelijke herstelpunt behouden.
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await controller.async_shutdown()
        await self._try_call("climate", "set_temperature", {
            "entity_id": conf[CONF_POOLEX_CLIMATE], "temperature": POOLEX_SETPOINT_FLOOR,
        })
        # Warmtevragen van BEIDE actuators laag; het eigen kuip-doel bewaren.
        await self._try_call("climate", "set_temperature", {
            "entity_id": conf[CONF_JACUZZI_CLIMATE],
            "temperature": self._entry.runtime_data.maintenance_gecko_target(),
        })
        await self._try_call(
            "select",
            "select_option",
            {
                "entity_id": conf[CONF_WATERCARE_SELECT],
                "option": standby,
            },
        )
        # '15 °C' service-succes bewijst geen compressorstop. Tot duty=0
        # bevestigd is behouden we flow; de onderhoudscontroller rondt af.
        compressor = self.hass.states.get(conf[CONF_COMPRESSOR_SENSOR])
        try:
            duty = float(compressor.state) if compressor is not None else float("nan")
        except (ValueError, TypeError):
            duty = float("nan")
        compressor_stopped = self._entry.runtime_data._fresh(compressor) and math.isfinite(duty) and duty == 0
        for fan in (conf[CONF_PUMP_FAN], *MIX_PUMPS):
            # Alleen de koelende circulatie wacht op compressor-uit;
            # massagepompen mogen onmiddellijk uit bij onderhoud.
            if fan == conf[CONF_PUMP_FAN] and not compressor_stopped:
                continue
            await self._try_call("fan", "turn_off", {"entity_id": fan})
        # Waarschuwing: de Gecko-pack doet zelf in.flo-flow-checks en
        # check-cycli (pomp droog bij lege kuip!) — software kan die niet
        # blokkeren, dus de groep moet er echt uit.
        if conf.get(CONF_NOTIFY_SERVICE):
            await self._call(
                "notify",
                conf[CONF_NOTIFY_SERVICE],
                {
                    "title": "Jacuzzi: onderhoudsmodus aan",
                    "message": "Onderhoud gevraagd: Poolex 15 °C, Gecko-doel minimum, "
                    "pompen uit zodra compressor-uit bevestigd is. Controleer de standen; "
                    "de Gecko-pack kan zelf pompen starten "
                    "(flow-checks). Bij een lege kuip: schakel de groep "
                    "uit in de meterkast!",
                },
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Herstel de bewaarde standen; de controller hervat."""
        if not self.is_on:
            return  # geen oude onderhoudssnapshot opnieuw toepassen tijdens een run
        conf = self._conf()
        saved = self._entry.options.get(CONF_MAINT_SAVED) or {}
        options = dict(self._entry.options)
        options[CONF_MAINTENANCE] = False
        # Eerst laag, dán eventueel standby herstellen, pas als laatste
        # onderhoud vrijgeven. Een gefaalde call laat onderhoud actief.
        await self._call("climate", "set_temperature", {
            "entity_id": conf[CONF_POOLEX_CLIMATE], "temperature": POOLEX_SETPOINT_FLOOR,
        })
        # Oude bewaarde hvac_mode='off' NOOIT herstellen. De unit blijft
        # in standby; de controller herstelt alleen ons warmteverzoek.
        # Bewaard hoog actuator-setpoint is GEEN gebruikersdoel en wordt
        # niet hersteld; alleen de controller mag weer stoken.
        try:
            gecko_target = float(saved.get("gecko_setpoint"))
        except (TypeError, ValueError):
            gecko_target = float("nan")
        # De normale lage Gecko-stand (bv. 18 °C) wél terugzetten. Geen
        # oude hoge pack-vraag blind herstellen naast onze kuip-regelaar.
        floor = self._entry.runtime_data.maintenance_gecko_target()
        if math.isfinite(gecko_target) and floor <= gecko_target <= GECKO_MAINT_TARGET_C:
            await self._call("climate", "set_temperature", {
                "entity_id": conf[CONF_JACUZZI_CLIMATE], "temperature": gecko_target,
            })
        elif math.isfinite(gecko_target):
            _LOGGER.warning("Hoge/ongeldige oude Gecko-vraag %.1f niet automatisch hersteld", gecko_target)
        if saved.get("watercare") and saved["watercare"] not in (
            "unavailable",
            "unknown",
        ):
            await self._call(
                "select",
                "select_option",
                {
                    "entity_id": conf[CONF_WATERCARE_SELECT],
                    "option": saved["watercare"],
                },
            )
        self.hass.config_entries.async_update_entry(self._entry, options=options)
