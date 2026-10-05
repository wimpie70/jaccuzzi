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

- **Statische parameters** (onveranderd tijdens test): 105=500→5.0°C,
  106=500→5.0°C, 107=200→2.0°C, 110=60 min — passen op C5/C6/C7/C9.
  109='0' zou C8 kunnen zijn.
- **Telemetrie** (live gemeten tijdens compressorrun + d1-fault):
  - DP 16: inlaat-watertemp (x100)
  - DP 23: verdampingstemp — zakte naar 5,7°C tijdens run
  - DP 24: heetgas-/condensortemp — piekte 41,7°C
  - DP 25: uitgaand water — schoot naar **39°C** bij te weinig flow
  - DP 104: waarschijnlijk stroom (650 = 6,5A ≈ 1,5 kW)
  - DP 108: druk of frequentie (730→1660; 16,6 bar zou hoge-druk-fault
    verklaren)
  - DP 126: dynamisch (300→480)
- **Fault-codes zitten NIET in status-DP's** — d1-fault actief terwijl
  102/103/127 allemaal 0 bleven. App toont ze via historisch fault-logboek
  (push/alarm-kanaal). Live vangen = `receive()`-listener nodig.
- **d1 = waarschijnlijk hoge-druk/overhittingsbeveiliging** door te weinig
  doorstroom (bypass-kraan verder dichtknijpen!).
- **DP 101/115** = booleans (status/relays?). **117/118** = 3500/3200
  (vermogens/grenzen?).

## Gecko (in.touch 3) — "jaccuzzi"

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Kuip-temperatuur | `climate.jaccuzzi_thermostat_1` → attr `current_temperature` | **hoofdtrigger pomp-automation** |
| Setpoint Gecko | `climate.jaccuzzi_thermostat_1` → attr `temperature` | laag zetten (~34–35 °C) |
| Circulatiepomp | `fan.jaccuzzi_waterfall` | **bevestigd** = circulatiepomp |
| Massagepompen | `fan.jaccuzzi_pump_1`, `fan.jaccuzzi_pump_2` | |
| Watercare | `select.jaccuzzi_watercare_mode` | Savings/Standard/... — extra piek-schroef |
| Status | `binary_sensor.jaccuzzi_spa_status` e.a. | connectivity/running |

## Helpers (aanmaken in HA)

| Rol | Naam | Type |
|---|---|---|
| Pomp gestart door automation | `input_boolean.jacuzzi_pomp_door_ha` | input_boolean |
