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
| Foutcode | — (nog niet als entity) | zie hieronder |

### Fault-DP achterhalen
- **Download diagnostics** op het device (⋮ → Download diagnostics) → alle DP's zichtbaar, incl. eventueel `fault` (DP 21 bitfield; 16 = P03 waterflow-fout bij vergelijkbare Poolex).
- Alternatief: **tuya-local** of **ha-silverline** (beide HACS) maken fault-bits beschikbaar als binary_sensors.

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
