# Entity-ID's (status: 2026-10-05, tuya-local live)

## Poolex — via make-all/tuya-local (LOKAAL, protocol 3.5)

Device-config: `poolex_qline_heatpump` (auto-gematcht). De cloud-Tuya-
integratie is hiermee overbodig — onderstaande IDs komen van tuya-local.

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Aan/uit + setpoint | `climate.pool_heat_pump` | hvac_modes off/heat/cool; `temperature` = setpoint, `current_temperature` = inlaat-water |
| **Fault-indicator** | `binary_sensor.pool_heat_pump_problem` | nieuw! zat niet in het cloud-schema — monitor triggert hierop |
| Compressor actief | `sensor.pool_heat_pump_compressor_duty_cycle` | >0 = aan het verwarmen (was compressor_strength) |
| Fan | `sensor.pool_heat_pump_fan_speed` | rpm |
| Uitgaand water | `sensor.pool_heat_pump_outflow_temperature` | was flow_temperature |
| Buitentemp | `sensor.pool_heat_pump_temperature_2` | 21,9 °C = ambient |
| Verdamping/coil | `sensor.pool_heat_pump_coil_temperature` + `return_air_temperature` | |
| Heetgas | `sensor.pool_heat_pump_vent_temperature` | ≈ DP 24 |
| EEV's | `sensor.pool_heat_pump_main_eev`, `aux_eev` | pulses |
| Defrost | `switch.pool_heat_pump_defrost` + `binary_sensor..._defrost` | schrijfbaar + state |
| C4 bijstook | `select.pool_heat_pump_auxiliary_heating` | off/auto/manual — nu direct instelbaar! |
| C8 pomp | `select.pool_heat_pump_circulation_pump` | off/auto/manual — laten op off (geen relais) |
| Dry contact | `select.pool_heat_pump_dry_contact_function` | "In Grid" |
| C9 interval | `number.pool_heat_pump_sampling_interval` | 60 min |
| Hysteresis ×4 | `number.pool_heat_pump_..._hysteresis` | heating/cooling stop+restart |
| Diversen | `lock..._child_lock`, `switch..._power_down_memory`, `binary_sensor..._jet_valve`, `select..._temperature_unit` | |

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

## Lokaal via localtuya (geen cloud)

**Let op: het device spreekt ALLEEN protocol 3.5** (getest 2026-10-05:
3.3/3.4 → TCP-connectie OK maar géén DPs; 3.5 → 33 DPs). Gebruik dus
NIET de originele rospogrigio/localtuya (geen 3.5 in dropdown, repo
vrijwel onbeheerd) maar:

- ~~`xZetsubou/hass-localtuya`~~ — actieve fork mét 3.5
- **`make-all/tuya-local`** — GEKOZEN: protocol "auto" detecteerde 3.5,
  device-config `poolex_qline_heatpump` matchte direct (zie tabel boven)

Geen cloud-API-account/username nodig — device handmatig toevoegen met:

- Device ID: `bf20da734a1d2c4846onfr`
- Local key: uit `devices.json` (gitignored)
- IP: `192.168.30.111` — **vast maken met DHCP-reservering in de router**
- Protocol: **3.5**

Let op: Tuya laat maar **één** lokale verbinding toe. Smart Life-app
dicht tijdens gebruik van localtuya (app werkt dan via cloud, dat mag
wel). De `tools/query_dps.py`-queries zijn kort — geen probleem.

### Entities aanmaken (DP-mapping voor de config flow)

| Platform | Naam | DP | Instellingen |
|---|---|---|---|
| switch | Pool Heat Pump | 1 | |
| number | Poolex setpoint | 4 | min 4, max 40, step 1, scaling 0.01 |
| sensor | Water temp (inlaat) | 16 | °C, scaling 0.01 |
| sensor | Flow temp (uitgaand) | 25 | °C, scaling 0.01 |
| sensor | Outside temp | 26 | °C, scaling 0.01 |
| sensor | Compressor strength | 20 | 0–1500, geen scaling |
| switch | Defrost (handmatig) | 7 | |
| binary_sensor | Defrost state | 33 | |
| sensor | Verdampingstemp | 23 | °C, scaling 0.01 |
| sensor | Heetgas/condensortemp | 24 | °C, scaling 0.01 |
| sensor | Stroom | 104 | A, scaling 0.1 (650→6,5A) |
| sensor | Druk/frequentie | 108 | onbekend — eerst ruw loggen |

Daarna: hernoem de lokale entities naar dezelfde IDs als de
cloud-entities (`switch.pool_heat_pump_switch` e.d.) zodat de
automations ongewijzigd blijven werken, en verwijder daarna pas de
Tuya-cloud-integratie. Test eerst of `switch` en `number` lokaal
schrijven werken.

## Helpers (aanmaken in HA)

| Rol | Naam | Type |
|---|---|---|
| Pomp gestart door automation | `input_boolean.jacuzzi_pomp_door_ha` | input_boolean |
