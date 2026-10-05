# Jaccuzzi – energiemanagement

Home Assistant-aansturing van de jacuzzi-verwarming:

- **Jacuzzi:** Riptide RSP 230-1 met **Gecko in.ye-3** pack + **in.touch 3** (via Gecko-integratie in HA)
- **Warmtepomp:** **Poolex Spawer IceSpa 7** (via Tuya/Smart Life-integratie in HA)

## Architectuur (relay-less)

Geen hardware-relais tussen Poolex en Gecko. Alles loopt via Home Assistant:

| Functie | Oplossing |
|---|---|
| Primaire verwarming | Poolex (eigen thermostaat, setpoint 38 °C) |
| Back-up verwarming | Gecko-element op **laag** setpoint (~34–35 °C) — autonome fabrieks-fallback, werkt ook als HA/Poolex uitvalt |
| Circulatiepomp | HA-automation zet de pomp aan zodra de Poolex wil verwarmen |
| Kookpiek 16:30–20:00 | HA-automation zet de Poolex uit + Watercare-schema op de Gecko blokkeert de bijstook |

### Fallback bij HA/wifi-uitval

1. Poolex krijgt geen flow → flow-error, compressor start niet.
2. Water koelt af onder het lage Gecko-setpoint.
3. Gecko start **zelf** circulatiepomp + 3 kW-element → kuip blijft op ~35 °C.
4. HA terug → normale werking hervat.

### Testpunten

- [x] `fan.jaccuzzi_waterfall` = circulatiepomp — **bevestigd**
- [x] Herstelt de Poolex **zelf** uit een flow-error zodra de pomp weer draait? → **Ja** (d1 ruimde zichzelf op zodra flow herstelde; compressor startte opnieuw)
- [x] ~~Fault-DP?~~ → **bestaat niet** in dit product-schema. Proxy-detectie via `compressor_strength` (`jacuzzi_poolex_monitor.yaml`). Optioneel: `defrost`/`defrost_state` via tuya-local.
- [ ] Zorgt "pomp aan via HA" niet voor conflict met Gecko-filtercycli? (input_boolean-vlag gebruiken)

## Bestanden

- `entities.md` — entity-ID's van beide integraties (invullen zodra alles online is)
- `automations/` — de HA-automations (YAML)
