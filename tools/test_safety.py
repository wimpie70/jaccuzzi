"""Offline safety regressions: python3 -m unittest discover -s tools -p 'test_*.py'.

Execute the actual controller with an in-memory HA/service double. This does
not contact Home Assistant or hardware; device acknowledgement is explicit.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "jacuzzi"


def load_controller():
    namespace = {
        "__name__": "safety_test",
        "asyncio": asyncio,
        "math": math,
        "datetime": datetime,
        "timedelta": timedelta,
        "callback": lambda fn: fn,
        "dt_util": SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
        "async_dispatcher_send": lambda *args: None,
        "SIGNAL_UPDATE": "update",
        "_LOGGER": logging.getLogger("safety_test"),
    }
    exec(compile((ROOT / "const.py").read_text(), str(ROOT / "const.py"), "exec"), namespace)
    tree = ast.parse((ROOT / "controller.py").read_text())
    tree.body = [
        node for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        or isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    exec(compile(tree, str(ROOT / "controller.py"), "exec"), namespace)
    return namespace


class State:
    def __init__(self, entity_id, state, attributes=None, *, age=0):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_reported = datetime.now(timezone.utc) - timedelta(seconds=age)
        self.last_updated = self.last_reported


class States(dict):
    def is_state(self, entity_id, state):
        return entity_id in self and self[entity_id].state == state


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_controller()
        self.clock = SimpleNamespace(value=10000.0)
        self.ns["time"] = SimpleNamespace(monotonic=lambda: self.clock.value)
        self.ns["State"] = State
        self.calls = []
        self.ack = True
        self.fail_writes = False
        self.active_calls = 0
        self.max_active_calls = 0
        self.tasks = []
        self.states = States({
            "poolex": State("poolex", "heat", {"temperature": 37, "current_temperature": 30}),
            "gecko": State("gecko", "heat", {"current_temperature": 36, "temperature": 18}),
            "pump": State("pump", "on"),
            "compressor": State("compressor", "40"),
            "problem": State("problem", "off"),
            "watercare": State("watercare", "Savings", {"options": ["Savings", "Away"]}),
            self.ns["DEFAULT_AMBIENT_SENSOR"]: State("ambient", "13"),
            self.ns["POOLEX_OUTLET_SENSOR"]: State("outlet", "36"),
        })
        hass = SimpleNamespace(
            states=self.states,
            services=SimpleNamespace(async_call=self.service),
            async_create_task=self.create_task,
        )
        conf = {
            "poolex_climate": "poolex", "jacuzzi_climate": "gecko",
            "pump_fan": "pump", "compressor_sensor": "compressor",
            "problem_sensor": "problem", "watercare_select": "watercare",
            "watercare_peak": "Away", "watercare_normal": "Savings",
            "peak_start": "16:30", "peak_end": "20:00",
            "temp_margin": 0.5, "overshoot": 1, "normal_setpoint": 37,
            "mix_enabled": False, "poolex_always_on": True,
            "notify_service": "notify",
        }
        self.c = self.ns["JacuzziController"](hass, conf)
        self.c._in_peak_window = lambda: False
        self.c._track_cooldown = lambda *args: None
        self.c._notify_once = lambda *args: None
        self.c._started = True
        self.c._demand_suppressed = False
        self.c._suppress_at = self.clock.value - 2000

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def asyncTearDown(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)
        # Globale invariant voor ALLE scenario's: nooit Poolex hvac off.
        self.assertFalse(any(domain == "climate" and data.get("hvac_mode") == "off"
                             for domain, service, data in self.calls))

    async def service(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, dict(data)))
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0)
            if self.fail_writes and service == "set_temperature":
                raise RuntimeError("simulated actuator failure")
            if self.ack:
                state = self.states.get(data.get("entity_id"))
                if state is not None:
                    if service == "set_temperature":
                        state.attributes["temperature"] = data["temperature"]
                    elif service == "set_hvac_mode":
                        state.state = data["hvac_mode"]
        finally:
            self.active_calls -= 1

    def temperatures(self):
        return [data["temperature"] for _, service, data in self.calls if service == "set_temperature"]

    def tub(self, value, *, age=0, state="heat"):
        self.states["gecko"] = State("gecko", state, {"current_temperature": value, "temperature": 18}, age=age)

    async def test_stop_at_limit_ignores_minimum_run(self):
        self.tub(38)
        self.c._suppress_at = self.clock.value - 120
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertFalse(self.c.warmtevraag)

    async def test_overheat_does_not_crash(self):
        self.tub(39.5)
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())

    async def test_sensor_failure_never_uses_inlet(self):
        for value, state in [(36, "unavailable"), (0, "heat"), (46, "heat"), (float("nan"), "heat")]:
            with self.subTest(value=value, state=state):
                self.setUp()
                self.tub(value, state=state)
                await self.c._async_evaluate()
                self.assertFalse(self.c.warmtevraag)
                self.assertIn(15, self.temperatures())
                self.assertNotIn("inlaat", self.c.demand_bron)

    async def test_stale_tub_stops(self):
        self.tub(36, age=600)
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)
        self.assertIn(15, self.temperatures())

    async def test_missing_entity_stops(self):
        del self.states["gecko"]
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())

    async def test_unknown_compressor_does_not_stop_pump(self):
        self.states["compressor"].state = "unavailable"
        self.c.pump_by_us = True
        self.c._compressor_recently_on = True
        self.c._since["comp_idle"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        await self.c._async_evaluate()
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.assertIsNone(self.c.compressor_on)
        self.assertIn(15, self.temperatures())

    async def test_failed_brake_retries_floor_without_switching_off(self):
        self.tub(38)
        self.fail_writes = True
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertTrue(self.c.demand_suppressed)
        self.assertFalse(any(s == "set_hvac_mode" and d["hvac_mode"] == "off" for _, s, d in self.calls))
        self.clock.value += 120
        await self.c._async_evaluate()
        self.assertFalse(any(s == "set_hvac_mode" and d["hvac_mode"] == "heat" for _, s, d in self.calls))

    async def test_unacknowledged_brake_escalates(self):
        self.tub(38)
        self.ack = False
        await self.c._async_evaluate()
        self.clock.value += 120
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertTrue(self.c.demand_suppressed)
        self.assertFalse(any(s == "set_hvac_mode" and d["hvac_mode"] == "off" for _, s, d in self.calls))

    async def test_floor_ack_but_running_compressor_escalates(self):
        self.tub(38)
        await self.c._async_evaluate()
        self.clock.value += 120
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertTrue(self.c.demand_suppressed)
        self.assertFalse(any(s == "set_hvac_mode" and d["hvac_mode"] == "off" for _, s, d in self.calls))

    async def test_external_setpoint_does_not_release_brake(self):
        self.tub(37.5)
        self.c._demand_suppressed = True
        self.c._suppress_reason = "anti-pendel"
        self.states["poolex"].attributes["temperature"] = 38
        await self.c._async_evaluate()
        self.assertTrue(self.c.demand_suppressed)
        self.assertFalse(self.c.warmtevraag)
        self.assertEqual(self.c.heat_setpoint, 37)
        self.assertIn(15, self.temperatures())

    async def test_restart_in_deadband_does_not_start_run(self):
        self.tub(37.5)
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)

    async def test_absolute_cap(self):
        self.c.async_set_target(40)
        self.c.conf["overshoot"] = 2
        self.tub(40)
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)
        self.assertIn(15, self.temperatures())

    async def test_invalid_target_rejected(self):
        for value in (100, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.c.async_set_target(value)
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_concurrent_evaluations_are_serialized(self):
        self.tub(38)
        await asyncio.gather(self.c._async_evaluate(), self.c._async_evaluate())
        self.assertEqual(self.max_active_calls, 1)

    async def test_stopped_controller_cannot_write(self):
        self.c.async_stop()
        await self.c._async_evaluate()
        self.assertFalse(self.calls)

    async def test_peak_callbacks_do_nothing_in_maintenance(self):
        self.c.conf["maintenance"] = True
        await self.c._on_peak_start(None)
        await self.c._on_peak_end(None)
        self.assertFalse(self.calls)

    async def test_cold_run_heats_only_with_circulation(self):
        self.states["compressor"].state = "0"
        self.states["pump"].state = "off"
        self.states["poolex"].attributes["temperature"] = 15
        await self.c._async_evaluate()
        self.assertFalse(any(t > 15 for t in self.temperatures()))
        self.assertTrue(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.states["pump"].state = "on"
        await self.c._async_evaluate()
        self.assertIn(37, self.temperatures())

    async def test_running_pump_failure_brakes(self):
        self.states["pump"].state = "off"
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertFalse(self.c.warmtevraag)

    async def test_running_hysteresis_and_target_change(self):
        self.c._run_active = True
        self.tub(37.5)
        await self.c._async_evaluate()
        self.assertTrue(self.c.warmtevraag)
        self.c.async_set_target(36)
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertEqual(self.c.heat_setpoint, 36)

    async def test_stop_sensor_failure_overrides_run_and_rest(self):
        self.c._run_active = True
        self.c._suppress_at = self.clock.value
        self.tub(36, state="unavailable")
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())

    async def test_mixing_starts_after_confirmed_stop_and_resumes_cold(self):
        self.c._run_active = True
        self.c.conf["mix_enabled"] = True
        self.tub(38)
        await self.c._async_evaluate()
        self.assertEqual(self.c._verify_until, 0)
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.assertGreater(self.c._verify_until, self.clock.value)
        self.tub(36)
        self.clock.value += 360
        await self.c._async_evaluate()
        self.assertIn(37, self.temperatures())
        self.assertEqual(self.c._verify_resumes, 1)
        self.assertTrue(self.c._run_active)

    async def test_mixing_does_not_resume_warm_bulk(self):
        self.c._run_active = True
        self.tub(38)
        await self.c._async_evaluate()
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.tub(37.5)
        self.clock.value += 360
        await self.c._async_evaluate()
        self.assertFalse(self.c._run_active)
        self.assertNotIn(37, self.temperatures())

    async def test_interrupted_mixing_cannot_resume_early(self):
        self.c._verify_until = self.clock.value - 1
        self.c._verify_tub_at_stop = 38
        self.c._demand_suppressed = True
        self.states["pump"].state = "off"
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        await self.c._async_evaluate()
        self.assertGreater(self.c._verify_until, self.clock.value)
        self.assertNotIn(37, self.temperatures())

    async def test_peak_end_does_not_enable_heat(self):
        self.c._peak_active = True
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        self.tub(38)
        await self.c._async_evaluate()
        self.assertFalse(any(t > 15 for t in self.temperatures()))

    async def test_peak_blocks_even_if_watercare_fails(self):
        self.c._in_peak_window = lambda: True
        del self.states["watercare"]
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)
        self.assertFalse(any(t > 15 for t in self.temperatures()))

    async def test_sensor_unknown_does_not_clear_flow_lockout(self):
        self.c._flow_lockout = True
        self.states["problem"].state = "unavailable"
        await self.c._async_evaluate()
        self.assertTrue(self.c._flow_lockout)
        self.assertFalse(self.c.warmtevraag)

    async def test_unknown_outlet_retains_circulation(self):
        self.states["compressor"].state = "0"
        self.states[self.ns["POOLEX_OUTLET_SENSOR"]].state = "unavailable"
        self.c.pump_by_us = True
        self.c._compressor_recently_on = True
        self.c._since["comp_idle"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.tub(38)
        await self.c._async_evaluate()
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_startup_rest_is_30_seconds_after_confirmation(self):
        self.c._demand_suppressed = True
        self.c._suppress_at = self.clock.value
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())
        self.clock.value += 29
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())
        self.clock.value += 1
        await self.c._async_evaluate()
        self.assertIn(37, self.temperatures())

    async def test_persistent_high_echo_cannot_override_target(self):
        self.ack = False
        self.states["poolex"].attributes["temperature"] = 38
        self.tub(37.5)
        for _ in range(5):
            await self.c._async_evaluate()
            self.clock.value += 30
        self.assertEqual(self.c.heat_setpoint, 37)
        self.assertFalse(self.c.warmtevraag)

    async def test_shutdown_waits_for_old_evaluation(self):
        self.tub(38)
        await asyncio.gather(self.c._async_evaluate(), self.c.async_shutdown())
        n = len(self.calls)
        await self.c._async_evaluate()
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self.max_active_calls, 1)

    async def test_high_target_rejected_without_rounding_up(self):
        with self.assertRaises(ValueError):
            self.c.async_set_target(42)
        self.c.async_set_target(37.5)
        self.assertEqual(self.c.heat_setpoint, 37.5)

    async def test_invalid_regulation_options_brake_without_crash(self):
        for key, value in [("overshoot", None), ("overshoot", float("nan")), ("temp_margin", "bad")]:
            with self.subTest(key=key, value=value):
                self.setUp()
                self.c.conf[key] = value
                await self.c._async_evaluate()
                self.assertFalse(self.c.warmtevraag)
                self.assertIn(15, self.temperatures())

    async def test_invalid_actuator_setpoint_brakes(self):
        self.states["poolex"].attributes["temperature"] = float("nan")
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)
        self.assertIn(15, self.temperatures())

    async def test_rest_counts_from_confirmed_stop(self):
        self.tub(38)
        await self.c._async_evaluate()
        self.clock.value += 60
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.assertEqual(self.c._suppress_at, self.clock.value)

    async def test_maintenance_retains_pump_until_compressor_stopped(self):
        self.c.conf["maintenance"] = True
        self.c._since["maint_pump_grace_pump"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertTrue(self.c.demand_suppressed)
        self.assertFalse(any(s == "set_hvac_mode" and d["hvac_mode"] == "off" for _, s, d in self.calls))
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_actual_setup_restores_before_first_actuator_write(self):
        tree = ast.parse((ROOT / "__init__.py").read_text())
        tree.body = [n for n in tree.body if (
            isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
            or isinstance(n, ast.ImportFrom) and n.module == "__future__"
        )]
        entry = SimpleNamespace(data=dict(self.c.conf), options={}, runtime_data=None,
                                async_on_unload=lambda fn: None, add_update_listener=lambda fn: None)
        entry.data["normal_setpoint"] = 38
        async def forward(entry, platforms):
            self.assertFalse(self.calls)
            entry.runtime_data.async_set_target(37)
            entry.runtime_data.async_set_heating_enabled(False)
            entry.runtime_data._in_peak_window = lambda: False
            entry.runtime_data._notify_once = lambda *a: None
        async def dashboard(hass):
            pass
        async def integration(*args):
            return SimpleNamespace(version="test")
        ns = dict(self.ns)
        ns.update(PLATFORMS=[], _async_update_listener=lambda *a: None,
                  async_setup_dashboard=dashboard)
        self.ns.update(async_get_integration=integration,
                       async_track_state_change_event=lambda *a: lambda: None,
                       async_track_time_interval=lambda *a: lambda: None,
                       async_track_time_change=lambda *a, **kw: lambda: None)
        exec(compile(tree, "setup_test", "exec"), ns)
        self.c.hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward)
        await ns["async_setup_entry"](self.c.hass, entry)
        self.assertEqual(entry.runtime_data.heat_setpoint, 37)
        self.assertFalse(entry.runtime_data.heating_enabled)
        self.assertFalse(any(t > 15 for t in self.temperatures()))
        await entry.runtime_data.async_shutdown()

    async def test_climate_restore_keeps_off_and_rejects_old_high_target(self):
        class ClimateBase:
            def async_on_remove(self, fn):
                pass

        class RestoreBase:
            async def async_get_last_state(self):
                return self.last

        ns = dict(self.ns)
        ns.update(ClimateEntity=ClimateBase, RestoreEntity=RestoreBase,
                  ClimateEntityFeature=SimpleNamespace(TARGET_TEMPERATURE=1),
                  HVACMode=SimpleNamespace(HEAT="heat", OFF="off"),
                  HVACAction=SimpleNamespace(OFF="off", HEATING="heating", PREHEATING="preheating", IDLE="idle"),
                  UnitOfTemperature=SimpleNamespace(CELSIUS="°C"),
                  async_dispatcher_connect=lambda *a: lambda: None,
                  ATTR_SOURCE="demand_bron", ATTR_SUPPRESSED="rem_actief",
                  ATTR_STATUS="regelstatus", ATTR_RETOUR="retour_marge")
        tree = ast.parse((ROOT / "climate.py").read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.ClassDef)
                     or isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        exec(compile(tree, "climate_test", "exec"), ns)
        self.c._started = False
        climate = ns["JacuzziTubClimate"](SimpleNamespace(runtime_data=self.c, entry_id="test"))
        climate.hass = self.c.hass
        climate.last = State("our_climate", "off", {"temperature": 37, "retour_marge": 0.75})
        await climate.async_added_to_hass()
        self.assertEqual(self.c.heat_setpoint, 37)
        self.assertFalse(self.c.heating_enabled)
        self.assertAlmostEqual(self.c.retour_offset, 0.75)
        climate.last = State("our_climate", "heat", {"temperature": 42})
        await climate.async_added_to_hass()
        self.assertFalse(self.c.heating_enabled)
        self.assertFalse(self.calls)
        self.assertEqual(climate.target_temperature_step, 0.5)

    async def test_failed_mixing_does_not_allow_early_resume(self):
        original = self.c.hass.services.async_call
        async def fail_jet(domain, service, data, **kwargs):
            if domain == "fan" and service == "turn_on" and data["entity_id"] != "pump":
                raise RuntimeError("simulated jet failure")
            return await original(domain, service, data, **kwargs)
        self.c.hass.services.async_call = fail_jet
        self.c._run_active = True
        self.c.conf["mix_enabled"] = True
        self.tub(38)
        await self.c._async_evaluate()
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.tub(36)
        self.clock.value += 360
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())
        self.assertFalse(self.c._verify_pending)

    async def test_warm_command_rechecks_current_temperature(self):
        self.tub(38)
        ok = await self.c._async_call("climate", "set_temperature", {
            "entity_id": "poolex", "temperature": 37,
        })
        self.assertFalse(ok)
        self.assertFalse(self.calls)

    async def test_mixing_resume_limit_is_enforced(self):
        self.c._demand_suppressed = True
        self.c._suppress_at = self.clock.value
        self.c._verify_resumes = 2
        self.c._verify_until = self.clock.value - 1
        self.c._verify_tub_at_stop = 38
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())

    async def test_disabled_demand_allows_only_low_standby(self):
        self.c.heating_enabled = False
        self.states["poolex"].state = "off"
        self.states["poolex"].attributes["temperature"] = 15
        self.states["compressor"].state = "0"
        self.c._since["poolex_off_guard"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        await self.c._async_evaluate()
        self.assertTrue(any(s == "set_hvac_mode" and d["hvac_mode"] == "heat" for _, s, d in self.calls))
        self.assertFalse(any(t > 15 for t in self.temperatures()))
        self.assertFalse(self.c.warmtevraag)

    async def test_standby_when_user_demand_disabled(self):
        self.c.heating_enabled = False
        self.states["poolex"].state = "off"
        self.states["poolex"].attributes["temperature"] = 15
        self.states["compressor"].state = "0"
        self.c._since["poolex_off_guard"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        await self.c._async_evaluate()
        self.assertTrue(any(s == "set_hvac_mode" and d["hvac_mode"] == "heat" for _, s, d in self.calls))
        self.assertFalse(any(t > 15 for t in self.temperatures()))
        self.assertFalse(self.c.warmtevraag)

    async def test_circulation_retained_through_stop_and_mixing(self):
        self.c._run_active = True
        self.c.pump_by_us = True
        self.c.conf["mix_enabled"] = True
        self.tub(38)
        self.c._since["temp_reached"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.assertGreater(self.c._verify_until, self.clock.value)
        self.c._since["compressor_stopped"] = datetime.now(timezone.utc) - timedelta(minutes=4)
        self.clock.value += 180
        await self.c._async_evaluate()
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.clock.value += 180
        await self.c._async_evaluate()
        self.assertTrue(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_old_temperature_timer_does_not_skip_runon(self):
        self.c.pump_by_us = True
        self.tub(38)
        self.c._since["temp_reached"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.assertFalse(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.c._since["compressor_stopped"] = datetime.now(timezone.utc) - timedelta(minutes=4)
        self.clock.value += 180
        await self.c._async_evaluate()
        self.assertTrue(any(s == "turn_off" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_normal_restart_rest_remains_separate_from_startup(self):
        self.tub(38)
        await self.c._async_evaluate()
        self.states["compressor"].state = "0"
        await self.c._async_evaluate()
        self.tub(36)
        self.clock.value += 30
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())
        self.assertEqual(self.c._rest_s, 1800)
        self.clock.value += 1770
        await self.c._async_evaluate()
        self.assertIn(37, self.temperatures())

    async def test_legacy_peak_off_setting_only_writes_floor(self):
        self.c.conf["peak_mode"] = "off"
        self.c._in_peak_window = lambda: True
        await self.c._async_evaluate()
        self.assertIn(15, self.temperatures())
        self.assertFalse(self.c.warmtevraag)

    async def test_off_command_rejected_by_controller(self):
        ok = await self.c._async_call("climate", "set_hvac_mode", {
            "entity_id": "poolex", "hvac_mode": "off",
        })
        self.assertFalse(ok)
        self.assertFalse(self.calls)

    def switch_class(self, name):
        ns = dict(self.ns)
        ns["SwitchEntity"] = type("SwitchEntity", (), {})
        tree = ast.parse((ROOT / "switch.py").read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.ClassDef)
                     or isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        exec(compile(tree, "switch_test", "exec"), ns)
        entry = SimpleNamespace(runtime_data=self.c, entry_id="test", data=dict(self.c.conf), options={})
        switch = ns[name](entry)
        switch.hass = self.c.hass
        return switch

    async def test_poolex_switch_off_only_disables_heat_request(self):
        switch = self.switch_class("JacuzziPoolexSwitch")
        await switch.async_turn_off()
        self.assertIn(15, self.temperatures())
        self.assertFalse(switch.is_on)
        self.assertEqual(self.states["poolex"].state, "heat")

    async def test_maintenance_switch_never_sends_off(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        entry = switch._entry
        def update(entry, *, options):
            entry.options = options
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=update)
        self.states["compressor"].state = "0"
        await switch.async_turn_on()
        self.assertIn(15, self.temperatures())
        self.assertEqual(self.states["poolex"].state, "heat")
        entry.options["maintenance_saved"]["poolex_mode"] = "off"
        await switch.async_turn_off()
        self.assertFalse(entry.options["maintenance"])
        self.assertEqual(self.states["poolex"].state, "heat")

    async def test_no_literal_poolex_off_requests_in_integration(self):
        for path in ROOT.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "hvac_mode":
                            self.assertFalse(isinstance(value, ast.Constant) and value.value == "off", str(path))

    async def test_unconfirmed_stop_alarms_and_keeps_retrying_15(self):
        alarms = []
        self.c._notify_once = lambda *args: alarms.append(args)
        self.tub(38)
        await self.c._async_evaluate()
        self.clock.value += 120
        await self.c._async_evaluate()
        self.assertTrue(any(a[0] == "brake_failed" and a[1] for a in alarms))
        self.assertTrue(self.c._brake_lockout)
        self.clock.value += 30
        await self.c._async_evaluate()
        self.assertGreaterEqual(self.temperatures().count(15), 2)
        self.assertEqual(self.states["poolex"].state, "heat")
        self.assertFalse(self.c.warmtevraag)

    async def test_short_startup_rest_does_not_skip_stop_confirmation(self):
        self.c._demand_suppressed = True
        self.c._suppress_at = self.clock.value
        await self.c._async_evaluate()
        self.clock.value += 30
        await self.c._async_evaluate()
        self.assertNotIn(37, self.temperatures())
        self.assertFalse(self.c._run_active)

    async def test_maintenance_off_request_is_rejected(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        with self.assertRaises(ValueError):
            await switch._call("climate", "set_hvac_mode", {
                "entity_id": "poolex", "hvac_mode": "off",
            })
        self.assertFalse(self.calls)

    async def test_gecko_circulation_drop_restored_without_long_debounce(self):
        self.c._run_active = True
        self.states["pump"].state = "off"
        self.c._pump_cmd_at = self.clock.value - 10
        await self.c._async_evaluate()
        self.assertTrue(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.assertIn(15, self.temperatures())
        self.assertTrue(self.c.pump_by_us)
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_gecko_drop_during_cooling_restored(self):
        self.tub(38)
        self.states["pump"].state = "off"
        self.states["compressor"].state = "0"
        self.states[self.ns["POOLEX_OUTLET_SENSOR"]].state = "52"
        self.c._pump_cmd_at = self.clock.value - 10
        await self.c._async_evaluate()
        self.assertFalse(self.c.warmtevraag)
        self.assertTrue(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_gecko_drop_during_runon_restored_without_hot_outlet(self):
        self.tub(38)
        self.states["pump"].state = "off"
        self.states["compressor"].state = "0"
        self.c._compressor_recently_on = True
        await self.c._async_evaluate()
        self.assertTrue(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_gecko_drop_during_mixing_restored(self):
        self.tub(37.5)
        self.states["pump"].state = "off"
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        self.c._verify_until = self.clock.value + 100
        self.c._verify_tub_at_stop = 38
        self.c._demand_suppressed = True
        self.c._pump_cmd_at = self.clock.value - 10
        await self.c._async_evaluate()
        self.assertTrue(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))
        self.assertGreater(self.c._verify_until, self.clock.value)

    async def test_no_circulation_restore_in_maintenance_or_flow_lockout(self):
        for maintenance, lockout in [(True, False), (False, True)]:
            with self.subTest(maintenance=maintenance, lockout=lockout):
                self.setUp()
                self.states["pump"].state = "off"
                self.c.conf["maintenance"] = maintenance
                self.c._flow_lockout = lockout
                self.states["problem"].state = "on" if lockout else "off"
                await self.c._async_evaluate()
                self.assertFalse(any(s == "turn_on" and d["entity_id"] == "pump" for _, s, d in self.calls))

    async def test_failed_circulation_restore_never_claims_ownership(self):
        original = self.c.hass.services.async_call
        async def fail_pump(domain, service, data, **kwargs):
            if domain == "fan" and service == "turn_on" and data["entity_id"] == "pump":
                raise RuntimeError("simulated pump failure")
            return await original(domain, service, data, **kwargs)
        self.c.hass.services.async_call = fail_pump
        self.states["pump"].state = "off"
        await self.c._async_evaluate()
        self.assertFalse(self.c.pump_by_us)
        self.assertIn(15, self.temperatures())
        self.assertFalse(self.c.warmtevraag)
        n = len([s for _, s, _ in self.calls if s == "set_temperature"])
        self.clock.value += 5
        await self.c._async_evaluate()
        self.assertFalse(self.c.pump_by_us)
        self.assertGreaterEqual(len([s for _, s, _ in self.calls if s == "set_temperature"]), n)

    async def test_maintenance_low_targets_and_all_pumps_off(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        def update(entry, *, options):
            entry.options = options
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=update)
        self.states["compressor"].state = "0"
        self.states["gecko"].attributes.update(temperature=37, min_temp=15)
        for entity in self.ns["MIX_PUMPS"]:
            self.states[entity] = State(entity, "on")
        await switch.async_turn_on()
        targets = {d["entity_id"]: d["temperature"] for _, s, d in self.calls if s == "set_temperature"}
        self.assertEqual(targets["poolex"], 15)
        self.assertEqual(targets["gecko"], 15)
        off = {d["entity_id"] for _, s, d in self.calls if s == "turn_off"}
        self.assertTrue({"pump", *self.ns["MIX_PUMPS"]}.issubset(off))
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_maintenance_stops_jets_but_preserves_unconfirmed_circulation(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=lambda *a, **kw: None)
        await switch.async_turn_on()
        off = {d["entity_id"] for _, s, d in self.calls if s == "turn_off"}
        self.assertNotIn("pump", off)
        self.assertTrue(set(self.ns["MIX_PUMPS"]).issubset(off))

    async def test_maintenance_enforces_gecko_low_target_and_retries_pump_off(self):
        self.c.conf["maintenance"] = True
        self.c.conf["maintenance_saved"] = {"standby_mode": "Away"}
        self.states["compressor"].state = "0"
        self.states["gecko"].attributes.update(temperature=37, min_temp=15)
        for entity in ("pump", *self.ns["MIX_PUMPS"]):
            self.states[entity] = State(entity, "on")
            self.c._since[f"maint_pump_grace_{entity}"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        await self.c._async_evaluate()
        self.assertTrue(any(s == "set_temperature" and d["entity_id"] == "gecko" and d["temperature"] == 15 for _, s, d in self.calls))
        off = {d["entity_id"] for _, s, d in self.calls if s == "turn_off"}
        self.assertTrue({"pump", *self.ns["MIX_PUMPS"]}.issubset(off))
        self.assertFalse(any(s == "turn_on" for _, s, d in self.calls))
        self.assertFalse(self.c.warmtevraag)

    async def test_maintenance_attempts_remaining_stops_if_temperature_write_fails(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=lambda *a, **kw: None)
        self.states["compressor"].state = "0"
        self.fail_writes = True
        with self.assertLogs(self.ns["_LOGGER"], level="WARNING"):
            await switch.async_turn_on()
        off = {d["entity_id"] for _, s, d in self.calls if s == "turn_off"}
        self.assertTrue({"pump", *self.ns["MIX_PUMPS"]}.issubset(off))
        self.assertTrue(self.c.conf["maintenance"])
        self.assertFalse(any(s == "turn_on" for _, s, d in self.calls))

    async def test_return_offset_is_diagnostic_not_a_temperature_correction(self):
        for offset in (0, 0.5, 3.6, 9):
            self.assertTrue(self.c.async_restore_retour_offset(offset))
            self.assertEqual(self.c.stop_temperature, 38)
            self.tub(38)
            await self.c._async_evaluate()
            self.assertFalse(self.c.warmtevraag)
            self.assertEqual(self.c.demand_temp, 38)
            self.assertEqual(self.c.demand_bron, "kuip")
        for bad in (None, "bad", float("nan"), -1, 20):
            self.assertFalse(self.c.async_restore_retour_offset(bad))

    async def test_return_offset_ema_can_learn_less_than_two_and_zero(self):
        self.c.async_restore_retour_offset(1)
        self.c._demand_suppressed = True
        self.c._verify_tub_at_stop = 38
        self.c._verify_until = self.clock.value - 1
        self.states["compressor"].state = "0"
        self.states["poolex"].attributes["temperature"] = 15
        self.tub(37.5)
        await self.c._async_evaluate()
        self.assertAlmostEqual(self.c.retour_offset, 0.85)
        self.c._verify_tub_at_stop = 37.5
        self.c._verify_until = self.clock.value - 1
        await self.c._async_evaluate()
        self.assertAlmostEqual(self.c.retour_offset, 0.595)
        self.assertEqual(self.c.stop_temperature, 38)

    async def test_failed_mixing_cannot_update_learned_offset(self):
        self.c.async_restore_retour_offset(1)
        self.c._mix_check_failed = True
        self.c._verify_tub_at_stop = 38
        self.c._verify_until = self.clock.value - 1
        self.states["compressor"].state = "0"
        self.tub(37.5)
        await self.c._async_evaluate()
        self.assertEqual(self.c.retour_offset, 1)

    async def test_maintenance_restores_low_gecko_and_watercare_preserving_user_target(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        def update(entry, *, options):
            entry.options = options
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=update)
        self.states["compressor"].state = "0"
        self.states["gecko"].attributes.update(temperature=18, min_temp=15)
        await switch.async_turn_on()
        self.assertEqual(self.states["gecko"].attributes["temperature"], 15)
        self.states["watercare"].state = "Away"
        await switch.async_turn_off()
        self.assertEqual(self.states["gecko"].attributes["temperature"], 18)
        self.assertTrue(any(s == "select_option" and d["option"] == "Savings" for _, s, d in self.calls))
        self.assertEqual(self.c.heat_setpoint, 37)
        self.assertTrue(self.c.heating_enabled)
        self.assertEqual(self.states["poolex"].attributes["temperature"], 15)
        self.assertFalse(switch.is_on)

    async def test_maintenance_never_restores_high_gecko_target(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, "options", kw["options"]))
        self.states["compressor"].state = "0"
        self.states["gecko"].attributes.update(temperature=40, min_temp=15)
        await switch.async_turn_on()
        await switch.async_turn_off()
        self.assertEqual(self.states["gecko"].attributes["temperature"], 15)
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_repeated_maintenance_on_preserves_original_snapshot(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        self.c.hass.config_entries = SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, "options", kw["options"]))
        self.states["compressor"].state = "0"
        self.states["gecko"].attributes.update(temperature=18, min_temp=15)
        await switch.async_turn_on()
        n = len(self.calls)
        self.states["watercare"].state = "Away"
        await switch.async_turn_on()
        self.assertEqual(len(self.calls), n)
        self.assertEqual(switch._entry.options["maintenance_saved"]["gecko_setpoint"], 18)
        self.assertEqual(switch._entry.options["maintenance_saved"]["watercare"], "Savings")

    async def test_repeated_maintenance_off_cannot_brake_an_active_run(self):
        switch = self.switch_class("JacuzziMaintenanceSwitch")
        await switch.async_turn_off()
        self.assertFalse(self.calls)
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_high_gecko_element_target_lowered_at_tub_stop(self):
        self.tub(38)
        self.states["gecko"].attributes.update(temperature=40, min_temp=15)
        await self.c._async_evaluate()
        self.assertEqual(self.states["gecko"].attributes["temperature"], 15)
        self.assertEqual(self.states["poolex"].attributes["temperature"], 15)
        self.assertEqual(self.c.heat_setpoint, 37)

    async def test_invalid_default_target_does_not_abort_safety_setup(self):
        for value in (None, "bad", float("nan"), 50):
            conf = {**self.c.conf, "normal_setpoint": value}
            controller = self.ns["JacuzziController"](self.c.hass, conf)
            controller._started = True
            controller._in_peak_window = lambda: False
            controller._notify_once = lambda *args: None
            self.assertFalse(controller.heating_enabled)
            await controller._async_evaluate()
            self.assertFalse(controller.warmtevraag)
        self.assertIn(15, self.temperatures())

    async def test_start_after_restore(self):
        tree = ast.parse((ROOT / "__init__.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry")
        body = ast.unparse(fn)
        self.assertLess(body.index("async_forward_entry_setups"), body.index("controller.async_start"))


if __name__ == "__main__":
    unittest.main()
