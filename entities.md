# Entity-ID's (status: ingevuld 2026-xx)

## Tuya — Pool Heat Pump

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Master switch | `switch.pool_heat_pump_switch` | piekblokkade + mee-trigger pomp |
| Setpoint (control) | `number.pool_heat_pump_temperature` | 4–40 °C, nu 38 |
| Watertemperatuur | `sensor.pool_heat_pump_temperature` | 19,6 °C (alleen betrouwbaar mét flow) |
| Flow temperature | `sensor.pool_heat_pump_flow_temperature` | 20,9 °C |
| Outside temperature | `sensor.pool_heat_pump_outside_temperature` | 18 °C — voor eigen bijstook-logica |
| Compressor strength | `sensor.pool_heat_pump_compressor_strength` | 0 = idle; >0 = echt aan het verwarmen |
| Child lock | `switch.pool_heat_pump_child_lock` | |
| Defrost (handmatig, schrijfbaar) | — niet gemapt | DP 7 — via tuya-local ontsluitbaar |
| Defrost state | — niet gemapt | DP 33 — via tuya-local ontsluitbaar |
| Foutcode | **bestaat niet** | geen fault-DP in het schema |

### DP-schema (uit diagnostics, product `trtfk7jrlez4hvxu`, cat. znrb)
Volledige lijst: `1=switch, 3=child_lock, 4=temp_set, 6=temp_unit_convert,
7=defrost, 16=temp_current, 20=compressor_strength, 25=temp_effluent,
26=temp_around, 33=defrost_state`. **Geen fault/foutcode-DP** — foutdetectie
dus via proxy: `compressor_strength` blijft 0 terwijl warmtevraag bestaat
(zie `automations/jacuzzi_poolex_monitor.yaml`).

## Gecko (in.touch 3) — "jaccuzzi"

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Kuip-temperatuur | `climate.jaccuzzi_thermostat_1` → attr `current_temperature` | **hoofdtrigger pomp-automation** |
| Setpoint Gecko | `climate.jaccuzzi_thermostat_1` → attr `temperature` | laag zetten (~34–35 °C) |
| Circulatiepomp | `fan.jaccuzzi_waterfall` (vermoedelijk!) | stond AAN terwijl pump1/2 uit → verifieer |
| Massagepompen | `fan.jaccuzzi_pump_1`, `fan.jaccuzzi_pump_2` | |
| Watercare | `select.jaccuzzi_watercare_mode` | Savings/Standard/... — extra piek-schroef |
| Status | `binary_sensor.jaccuzzi_spa_status` e.a. | connectivity/running |

## Helpers (aanmaken in HA)

| Rol | Naam | Type |
|---|---|---|
| Pomp gestart door automation | `input_boolean.jacuzzi_pomp_door_ha` | input_boolean |
