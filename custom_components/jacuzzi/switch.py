"""Switch platform: Poolex aan/uit (stelt de climate hvac_mode)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JacuzziConfigEntry
from .controller import SIGNAL_UPDATE
from .const import (
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
)


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
    """Aan/uit voor de warmtepomp — stelt hvac_mode van de climate."""

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
        """Aan als de Poolex-climate niet op 'off' staat."""
        state = self.hass.states.get(self._poolex_entity())
        if state is None:
            return None
        return state.state != "off"

    async def async_turn_on(self, **kwargs) -> None:
        """Zet de Poolex op heat."""
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._poolex_entity(), "hvac_mode": "heat"},
            blocking=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Zet de Poolex uit."""
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._poolex_entity(), "hvac_mode": "off"},
            blocking=True,
        )

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
    """Onderhoudsmodus: alle automatiseringen uit, kuip standby, Poolex off.

    Aan = bewaar de huidige standen (Poolex hvac-mode/setpoint, watercare)
    in options en zet alles in onderhoud: Poolex off, watercare op de
    standby/away-stand (of piek-stand als geen van beide bestaat),
    circulatie- en
    massagepompen uit. De controller
    slaat dan álle acties over (incl. failsafe en altijd-aan guard).
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
        await self.hass.services.async_call(domain, service, data, blocking=True)

    async def async_turn_on(self, **kwargs) -> None:
        """Bewaar de huidige standen en zet alles in onderhoud."""
        conf = self._conf()
        poolex = self.hass.states.get(conf[CONF_POOLEX_CLIMATE])
        watercare = self.hass.states.get(conf[CONF_WATERCARE_SELECT])
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
            # de gekozen standby-stand: handhaven tijdens onderhoud
            "standby_mode": standby,
        }
        options[CONF_MAINTENANCE] = True
        # eerst de vlag: de herladende controller mag niets terugvechten
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self._call(
            "climate",
            "set_hvac_mode",
            {"entity_id": conf[CONF_POOLEX_CLIMATE], "hvac_mode": "off"},
        )
        await self._call(
            "select",
            "select_option",
            {
                "entity_id": conf[CONF_WATERCARE_SELECT],
                "option": standby,
            },
        )
        for fan in (conf[CONF_PUMP_FAN], *MIX_PUMPS):
            await self._call("fan", "turn_off", {"entity_id": fan})
        # Waarschuwing: de Gecko-pack doet zelf in.flo-flow-checks en
        # check-cycli (pomp droog bij lege kuip!) — software kan die niet
        # blokkeren, dus de groep moet er echt uit.
        if conf.get(CONF_NOTIFY_SERVICE):
            await self._call(
                "notify",
                conf[CONF_NOTIFY_SERVICE],
                {
                    "title": "Jacuzzi: onderhoudsmodus aan",
                    "message": "Poolex staat op off en de automatiseringen "
                    "zijn uit — maar de Gecko-pack kan zelf pompen starten "
                    "(flow-checks). Bij een lege kuip: schakel de groep "
                    "uit in de meterkast!",
                },
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Herstel de bewaarde standen; de controller hervat."""
        conf = self._conf()
        saved = self._entry.options.get(CONF_MAINT_SAVED) or {}
        options = dict(self._entry.options)
        options[CONF_MAINTENANCE] = False
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        mode = saved.get("poolex_mode")
        if mode and mode not in ("unavailable", "unknown"):
            await self._call(
                "climate",
                "set_hvac_mode",
                {"entity_id": conf[CONF_POOLEX_CLIMATE], "hvac_mode": mode},
            )
        if saved.get("poolex_setpoint") is not None:
            await self._call(
                "climate",
                "set_temperature",
                {
                    "entity_id": conf[CONF_POOLEX_CLIMATE],
                    "temperature": saved["poolex_setpoint"],
                },
            )
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
