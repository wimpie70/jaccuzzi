# Jaccuzzi – energiemanagement

Home Assistant-aansturing van de jacuzzi-verwarming:

- **Jacuzzi:** Riptide RSP 230-1 met **Gecko in.ye-3** pack + **in.touch 3** (via Gecko-integratie in HA)
- **Warmtepomp:** **Poolex Spawer IceSpa 7** (via **tuya-local**, protocol 3.5 — geen cloud)

## Architectuur (relay-less)

Geen hardware-relais tussen Poolex en Gecko. Alles loopt via Home Assistant:

| Functie | Oplossing |
|---|---|
| Primaire verwarming | Poolex (eigen thermostaat, setpoint ~37–38 °C) |
| Back-up verwarming | Gecko-element op **laag** setpoint — autonome fabrieks-fallback, werkt ook als HA/Poolex uitvalt |
| Circulatiepomp | Integratie zet `fan.jaccuzzi_waterfall` aan zodra de Poolex warmte vraagt |
| Menging | Massagepomp 1/2 afwisselend kort aan tijdens het stoken (interval instelbaar) — de inlaatsensor meet dan de echte bulk-temperatuur |
| Piekblokkade | Poolex-setpoint → 15 °C (mode `setpoint`, unit blijft standby) of `hvac off`; watercare → `Away`; PV-overschot kan de blokkade vervroegd eindigen |

### Fallback bij HA/wifi-uitval

1. Poolex krijgt geen flow → flow-error, compressor start niet.
2. Water koelt af onder het lage Gecko-setpoint.
3. Gecko start **zelf** circulatiepomp + element → kuip blijft warm.
4. HA terug → normale werking hervat.

## Veiligheid (wat de controller bewaakt)

| Situatie | Actie |
|---|---|
| Compressor draait, pomp uit | Pomp direct aan + melding (restwarmte moet weg) |
| Pomp aan + fault-bit > 5 min | Pomp uit + watercare → `Away` + lockout (geen auto-aanzet tot de fault weg is) — dekt lek pomp→flowmeter en lege kuip |
| Fault-bit aan, pomp uit | Geen melding — verwacht gedrag (vertraagde d1 na een stop is normaal); fault blijft wel zichtbaar op `binary_sensor.pool_heat_pump_problem` |
| Poolex staat op `off` | Terug naar `heat` na 60 s debounce (vorstbeveiliging/telemetrie vereisen standby) |
| Poolex/Gecko > 15 min unavailable | Melding + bij Gecko desnoods config-entry reload |
| Onderhoud aan | Poolex uit, watercare standby, alle pompen bewaakt (incl. massagepompen); korte Gecko-check-runs (< 90 s) worden getolereerd, langere teruggezet met retry-backoff |

Let op: watercare multi-word opties zoals `Super Savings` falen **stilletjes**
in de gecko-integratie (Title-Case options, maar geckolib accepteert alleen
UPPER_SNAKE intern). Gebruik single-word modes: `Away`, `Savings`, `Weekender`.

## Meting & rendement

| Sensor | Betekenis |
|---|---|
| `sensor.jacuzzi_tub_temperature` | Kuip-temperatuur (Gecko) |
| `sensor.jacuzzi_poolex_inlet_temperature` | Poolex-inlaat = beste bulk-schatting tijdens circulatie |
| `sensor.jacuzzi_poolex_delta_t` | Uitlaat − inlaat over de wisselaar |
| `sensor.jacuzzi_heating_power` | Thermisch vermogen uit de inlaat-helling |
| `sensor.jacuzzi_poolex_power_estimate` | Elektrisch vermogen, geschat uit compressor-duty × `number.jacuzzi_poolex_max_watts` (kalibreer met P1 of een echte meter) |
| `sensor.jacuzzi_cop_estimate` | COP per stook-cyclus: eerste/laatste **post-mix** bulk-temp + geïntegreerde Wh → ΔT × 1500 kg × 4.19 / kWh. Attributen: `delta_t_k`, `kwh_thermisch`, `kwh_elektrisch` |
| `sensor.jacuzzi_poolex_energy` | kWh-integratie op de geconfigureerde W-sensor (bruikbaar voor Energy Dashboard) |

COP is een **schat ÷ schat** tot er een echte vermogensmeter op de Poolex zit.

## Installatie (HACS)

1. HACS → ⋯ → **Custom repositories** → `https://github.com/wimpie70/jaccuzzi`,
   type **Integration**
2. Installeer "Jacuzzi Poolex Control" en herstart HA
3. Settings → Devices & Services → Add integration → **Jacuzzi**
   (de entity-defaults passen direct op deze setup)

Updates gaan daarna via HACS — geen SSH nodig.

Het dashboard staat op `/jacuzzi` (native sections-view: 3 kolommen op
desktop, klapt terug naar 1 kolom op mobiel).

## Bestanden

- `custom_components/jacuzzi/` — de integratie (controller + config flow
  + sensors + dashboard)
- `entities.md` — entity-ID's + volledige DP-tabel (lokaal, protocol 3.5)
- `packages/jacuzzi.yaml` — standalone YAML-variant van dezelfde logica
  (fallback, niet meer nodig als de integratie draait)
- `tools/query_dps.py` — ruwe DP-dump via tinytuya (credentials in
  gitignored `devices.json`)
- `tools/deploy.sh` — deploy package-variant naar prod-HA via SSH
- `lovelace/jacuzzi_card.yaml` — kant-en-klare dashboard-card (alle
  data uit de 3 integraties): plak via Kaart toevoegen → "Manual"
