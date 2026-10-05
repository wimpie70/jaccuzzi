# Jaccuzzi – energiemanagement

Home Assistant-aansturing van de jacuzzi-verwarming:

- **Jacuzzi:** Riptide RSP 230-1 met **Gecko in.ye-3** pack + **in.touch 3** (via Gecko-integratie in HA)
- **Warmtepomp:** **Poolex Spawer IceSpa 7** (via **tuya-local**, protocol 3.5 — geen cloud)

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
- [x] ~~Fault-DP?~~ → cloud-schema had er geen, maar tuya-local levert `binary_sensor.pool_heat_pump_problem` + `compressor_duty_cycle` als proxy-fallback.
- [ ] Zorgt "pomp aan via HA" niet voor conflict met Gecko-filtercycli? (input_boolean-vlag gebruiken)

## Installatie (HACS)

`custom_components/jacuzzi` is een echte integratie — alle
automation-logica in Python, instelbaar via de UI (entities, piektijden,
Watercare-modes, notify-service).

1. HACS → ⋯ → **Custom repositories** → `https://github.com/wimpie70/jaccuzzi`,
   type **Integration**
2. Installeer "Jacuzzi Poolex Control" en herstart HA
3. Settings → Devices & Services → Add integration → **Jacuzzi**
   (de entity-defaults passen direct op deze setup)

Updates gaan daarna via HACS — geen SSH nodig.

## Bestanden

- `custom_components/jacuzzi/` — de integratie (controller + config flow
  + status-binary_sensors `Pomp door HA` en `Warmtevraag`)
- `entities.md` — entity-ID's + volledige DP-tabel (lokaal, protocol 3.5)
- `packages/jacuzzi.yaml` — standalone YAML-variant van dezelfde logica
  (fallback, niet meer nodig als de integratie draait)
- `tools/query_dps.py` — ruwe DP-dump via tinytuya (credentials in
  gitignored `devices.json`)
- `tools/deploy.sh` — deploy package-variant naar prod-HA via SSH
- `lovelace/jacuzzi_card.yaml` — kant-en-klare dashboard-card (alle
  data uit de 3 integraties): plak via Kaart toevoegen → "Manual"
