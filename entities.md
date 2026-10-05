# Entity-ID's (invullen zodra Poolex + Gecko beide online zijn)

## Tuya — Pool Heat Pump

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Master switch | `switch.xxx` | piekblokkade + pomp-trigger |
| Setpoint (control) | `number.xxx` / `climate.xxx` | streeftemperatuur instellen |
| Watertemperatuur (sensor) | `sensor.xxx` | kuip-water temp? |
| Flow temperature | `sensor.xxx` | alleen betrouwbaar mét flow |
| Outside temperature | `sensor.xxx` | buitentemp |
| Compressor strength | `sensor.xxx` / `select.xxx` | draait hij echt? |
| Child lock | `switch.xxx` / `lock.xxx` | |
| Foutcode / status (zoeken!) | `sensor.xxx` | check attributes + diagnostics |

## Gecko (in.touch 3)

| Rol | Entity-ID | Opmerking |
|---|---|---|
| Watertemperatuur kuip | `sensor.xxx` | **hoofdtrigger pomp-automation** |
| Circulatiepomp | `switch.xxx` / `fan.xxx` | schakelbaar? |
| Setpoint Gecko | `climate.xxx` / `number.xxx` | laag zetten (~34–35 °C) |
| Heater status / warmtevraag | `sensor.xxx` | optioneel |
| Pump 1 / Pump 2 | `switch.xxx` | massage |
| Error / status | `sensor.xxx` | optioneel |

## Helpers (aanmaken in HA)

| Rol | Naam | Type |
|---|---|---|
| Pomp gestart door automation | `input_boolean.jacuzzi_pomp_door_ha` | input_boolean |
