# Jacuzzi integration safety and verification

- The custom integration owns control and registers its dashboard. Production
  has no additional Jacuzzi YAML automations/cards; `packages/jacuzzi.yaml`
  is legacy, not a required deployment step.
- Gecko tub temperature is the regulation measurement. Poolex inlet readings
  systematically under-read and must never authorize heating, including as a
  fallback. Do not subtract the measured outlet-minus-inlet delta from Gecko.
- Temperature/sensor stops always precede timers. Anti-cycling limits restart,
  not stopping. User target is independent of Poolex actuator echoes.
- NEVER set Poolex HVAC mode to off (including faults, maintenance, switches,
  or legacy peak settings). Brake with setpoint 15 °C, retain telemetry,
  keep appropriate circulation, retry and alarm if the stop is not confirmed.
- Startup/reload rest is 30 seconds, distinct from normal 30-minute restart
  dwell after a heating run. Stop confirmation remains mandatory.
  A low actuator target alone is not anti-cycling: show startup wait separately,
  true restart dwell only when demand is waiting, and idle when no heat is needed.
- Maintenance keeps both actuator targets low (Poolex 15 °C, Gecko minimum),
  stops massage pumps and stops circulation after confirmed compressor idle.
  On exit restore saved Watercare and a valid low Gecko setting (<=18 °C),
  not a stale high actuator target; the tub target/heating mode stays separate.
  Learned return offset is restored through climate attributes and is solely
  diagnostic, never part of a start/stop/overheat threshold.
  Do not confuse software standby with physical isolation of an empty tub.
- Restore confirmed-off circulation when needed for heating, mixing or cooling,
  except during maintenance/flow lockout. Urgent recovery uses a 5-second
  command retry limit rather than the ordinary 30-second debounce.
  Retain pump ownership after failed/unconfirmed stop requests; release it
  only when pump-off is reported and circulation is no longer needed.
- The generic Poolex problem bit can briefly indicate d1 during circulation
  startup. Allow a single 120-second window only for an otherwise valid new
  start; pump-on must be confirmed before raising the actuator target. Retries
  must not renew that window. Persistent/mid-run faults and all sensor,
  temperature, peak, maintenance and lockout protections still take priority.
- Notification delivery must handle missing/failed notify services, falling
  back to a registered persistent_notification service without unhandled tasks.
- Run offline safety regressions with:
  `python3 -m unittest discover -s tools -p 'test_*.py'`
  These execute controller/lifecycle code against HA/service doubles; they do
  not prove hardware behaviour or Home Assistant platform compatibility.
- Lint with `ruff check --select F custom_components/jacuzzi tools/test_safety.py`
  and check whitespace with `git diff --check`.
- Do not test actuator commands against production, publish releases, or push
  changes unless the user explicitly requests it.
