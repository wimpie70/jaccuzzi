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

## Lokale DPs (tinytuya, protocol 3.5 — `tools/query_dps.py`)

De lokale query toont **33 DPs** — veel meer dan de cloud-schema. Dump
(gemeten, unit uit): `1=F,2='4',3=T,4=3800,6='c',7=F,16=2050,20=0,23=1920,
24=2000,25=2120,26=2070,33=F,101=T,102=0,103='0',104=0,105=500,106=500,
107=200,108=2000,109='0',110=60,111-114=200,115=T,117=3500,118=3200,
126=350,127=0,128=F`.

Sterke vermoedens (waarden passen op fabrieksdefaults):

- **DP 104–110 = de C4–C9-parameters**: 104=0 (C4 heater-relais uit — klopt,
  nog geen relais), 105=500→5.0°C (C5), 106=500→5.0°C (C6), 107=200→2.0°C
  (C7), 109='0' (C8 pomp-relais uit), 110=60 min (C9). Als dit klopt zijn de
  parameters **lokaal schrijfbaar** via tuya-local.
- **Fault-kandidaten** (nu allemaal 0 = gezond): DP **102**, **103** ('0'),
  **127**. Verifiëren: flow-error provoceren en kijken welke verandert.
- **DP 23/24** = extra temps (19.2 / 20.0°C — andere sensoren).
- **DP 101/115** = booleans (status/relays?). **117/118** = 3500/3200
  (vermogens/grenzen?).

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
