"""Constants for the Jacuzzi Poolex Control integration."""

DOMAIN = "jacuzzi"

CONF_POOLEX_CLIMATE = "poolex_climate"
CONF_JACUZZI_CLIMATE = "jacuzzi_climate"
CONF_PUMP_FAN = "pump_fan"
CONF_COMPRESSOR_SENSOR = "compressor_sensor"
CONF_PROBLEM_SENSOR = "problem_sensor"
CONF_WATERCARE_SELECT = "watercare_select"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_PEAK_START = "peak_start"
CONF_PEAK_END = "peak_end"
CONF_WATERCARE_PEAK = "watercare_peak"
CONF_WATERCARE_NORMAL = "watercare_normal"
CONF_TEMP_MARGIN = "temp_margin"
CONF_OVERSHOOT = "overshoot"
CONF_PEAK_MODE = "peak_mode"
CONF_PEAK_SETPOINT = "peak_setpoint"
CONF_NORMAL_SETPOINT = "normal_setpoint"
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_SOLAR_MIN_W = "solar_min_watts"
CONF_POOLEX_POWER_SENSOR = "poolex_power_sensor"
CONF_POOLEX_MAX_W = "poolex_max_watts"
CONF_JACUZZI_POWER_SENSOR = "jacuzzi_power_sensor"
CONF_POOLEX_ALWAYS_ON = "poolex_always_on"
CONF_MIX_ENABLED = "mix_enabled"
CONF_MIX_INTERVAL_MIN = "mix_interval_min"
CONF_MIX_PULSE_S = "mix_pulse_s"
CONF_MAINTENANCE = "maintenance"
CONF_MAINT_SAVED = "maintenance_saved"
CONF_INLET_COMPENSATION = "inlet_compensation_k"

DEFAULT_POOLEX_CLIMATE = "climate.pool_heat_pump"
DEFAULT_JACUZZI_CLIMATE = "climate.jaccuzzi_thermostat_1"
DEFAULT_PUMP_FAN = "fan.jaccuzzi_waterfall"
DEFAULT_COMPRESSOR_SENSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
DEFAULT_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
DEFAULT_WATERCARE_SELECT = "select.jaccuzzi_watercare_mode"
DEFAULT_NOTIFY_SERVICE = "notify"
DEFAULT_PEAK_START = "16:30"
DEFAULT_PEAK_END = "20:00"
# multi-word opties ('Super Savings') breken in de geckoal-integratie:
# geckolib accepteert alleen UPPER_SNAKE intern. Single-word modes
# (Away/Savings/Weekender) werken wel.
DEFAULT_WATERCARE_PEAK = "Away"            # moet exact matchen met select-options
DEFAULT_WATERCARE_NORMAL = "Savings"
DEFAULT_TEMP_MARGIN = 0.5        # vraag ontstaat onder doel-marge
# Een lopende run stookt door tot doel + overshoot: de kuip koelt
# daarna zelf af (gebruik/wachttijd), dus eindigen op doel-marge is
# structureel te koud. 1 K boven doel ~ gezien veilig; de bewaker
# (doel + 1 + retour-marge) blijft de vangrail daarboven.
DEFAULT_OVERSHOOT_K = 1.0
PEAK_MODE_SETPOINT = "setpoint"   # piek = laag setpoint, unit blijft aan
PEAK_MODE_OFF = "off"             # piek = hvac_mode off
DEFAULT_PEAK_MODE = PEAK_MODE_SETPOINT
DEFAULT_PEAK_SETPOINT = 4.0       # °C — laagste setpoint, unit idle
DEFAULT_NORMAL_SETPOINT = 38.0    # °C — fallback als geen opgeslagen setpoint
DEFAULT_SOLAR_MIN_W = 2000        # W — PV-overschot voor vervroegd piek-einde
DEFAULT_POOLEX_ALWAYS_ON = True   # buiten piek nooit hvac 'off' toestaan
DEFAULT_POOLEX_MAX_W = 1600       # W bij duty 100% — kalibreer met meter
SOLAR_SURPLUS_S = 600             # overschot moet 10 min aanhouden

# Inlaat-compensatie: DP16 zit thermisch gekoppeld aan de buitenlucht
# (stilstaand water zakt hij naar ambient — waargenomen 18 °C bij 30 °C
# water). De meetfout is dus geen vaste offset maar ~k × (water-buiten);
# gemeten uit recorder-data: k ≈ 0.14. demand_temp = inlaat + k ×
# (inlaat - buiten). Kalibreerbaar via een number-entity.
DEFAULT_INLET_COMPENSATION_K = 0.14

# Demand-remming via het Poolex-setpoint: de unit regelt zijn
# compressor zelf op DP16, die te laag leest -> hij zou doorstoken
# (gezien: kuip 44.5 °C bij doel 38). Wij zijn de echte thermostaat:
# vraag weg -> setpoint naar de vloer; vraag terug -> herstellen.
# Dwell-tijden voorkomen kort-cyclen van de compressor.
POOLEX_SETPOINT_FLOOR = 15.0   # tuya minimum in heat-mode
SP_SUPPRESS_REST_S = 1800      # setpoint laag >=30 min (compressor-rust)
SP_SUPPRESS_RUN_S = 900        # setpoint hoog >=15 min (min. stookrun)
OVERHEAT_MARGIN_K = 1.0        # kuip > doel + dit -> meteen remmen
SP_WRITE_GRACE_S = 90          # na onze eigen setpoint-write: live attr
                               # kan nog de oude waarde (tuya-echo) of
                               # een afgeronde waarde tonen -> niet
                               # adopteren als gebruikersdoel

# Stop-meng-meet: bij einde warmtevraag tijdens een run schrijven we de
# compressor-rem op basis van de (gecorrigeerde) inlaat — maar die
# leest systematisch te laag (pocket + ambient, ~5-9 K fout waargenomen
# 08/10; echte stratificatie blijkt klein: ~0.5 K gemeten 10/10). Eerst
# een paar minuten circuleren + jets mengen, dan pas de ECHTE bulk
# evalueren: nog vraag -> run hervatten (geen pendel: bulk klopt dan
# echt niet), geen vraag -> definitief klaar.
VERIFY_MIX_S = 360        # circulatie+meng-duur na een vraag-stop —
                          # ook >= de ~3-5 min compressor-egaliseertijd
VERIFY_MAX_RESUMES = 2    # max hervattingen via meng-check per sessie
# Lerende retour-offset: hoeveel heter de kuip-sensor las dan de
# gemengde bulk bleek te zijn -> vervangt de vaste RETOUR_GUARD_K
# als oververhittings-marge tijdens stoken. EMA-geleerd, geclamped:
# te laag = vals alarm, te hoog = iets anders kapot.
RETOUR_GUARD_MIN_K = 2.0
RETOUR_GUARD_MAX_K = 9.0

# Fase-afhankelijke vraag-bron. In rust (compressor al een poos uit)
# is de Gecko-kuipmeting leidend: de inlaat (DP16) drijft dan naar
# ambient door zijn slecht-gekoppelde pocket (>10 min convergentie,
# 's nachts volledig richting buitentemp). Tijdens/kort na stoken is
# de kuipmeting juist NIET bruikbaar als vraag-bron: de Gecko-buis
# leest retour-water = bulk + per-pass ΔT (~5 K bij duty ~70).
COMP_IDLE_TRUST_S = 180        # compressor uit zo lang -> kuip leidend
RETOUR_GUARD_K = 5.0           # extra oververhittings-marge tijdens
                               # stoken: retour ≈ bulk + per-pass ΔT;
                               # zonder deze ruimte tripte de bewaker
                               # constant op gezond retour-water

# Massagepompen voor de meng-puls (vaste Gecko-ids; staan niet in config)
MIX_PUMPS = ("fan.jaccuzzi_pump_1", "fan.jaccuzzi_pump_2")
# tuya-local entity van de Poolex-uitlaat (vaste id in deze setup)
POOLEX_OUTLET_SENSOR = "sensor.pool_heat_pump_outflow_temperature"
POST_HEAT_OUTLET_C = 45.0  # uitlaat heter dan dit na compressor-stop =
                           # restwarmte in de wisselaar -> pomp door laten
                           # draaien om af te koelen (waargenomen spike
                           # naar 52 °C direct na compressor-stop 08/10)
DEFAULT_MIX_ENABLED = True      # meng-puls tijdens het stoken
DEFAULT_MIX_INTERVAL_MIN = 30   # minuten tussen pulsen
DEFAULT_MIX_PULSE_S = 60        # pulsduur — genoeg om lagen te mengen

# Timers (seconds)
PUMP_RUNON_S = 180       # nadraaitijd pomp
FAILSAFE_DELAY_S = 15    # compressor aan + pomp uit -> pomp aan
MAINT_PUMP_GRACE_S = 90  # Gecko check-cyclus (~40 s) niet tegenwerken
MAINT_PUMP_RETRY_S = 300  # pack weigert turn_off tijdens eigen cyclus
PROBLEM_DELAY_S = 120    # fault-bit aan voordat we melden — d1 mag
                         # eerst de kans krijgen bij hervatte flow te clearen
FLOW_FAULT_OFF_S = 300   # pomp aan + fault zo lang -> pomp uit (drooglopen/lek)
PUMP_CMD_DEBOUNCE_S = 30 # min. tijd tussen pomp-commando's (RF-flap -> geen storm)
FAILSAFE_NOTIFY_MIN = 3      # pas melden bij de zoveelste pomp-dip
FAILSAFE_NOTIFY_WINDOW_S = 3600  # ...binnen dit venster (Gecko toggelt vanzelf)
NO_COMPRESSOR_S = 600    # warmtevraag zonder compressor -> verdacht
EVAL_INTERVAL_S = 30     # periodieke her-evaluatie van timers
UNREACHABLE_S = 900      # Poolex offline (PRCD-stekker/stroomstoring)
POOLEX_OFF_GUARD_S = 60  # hvac 'off' moet zo lang aanhouden voor de guard
RELOAD_COOLDOWN_S = 900  # max 1x per kwartier de Gecko-entry reloaden

# Warmteverlies-meting: tijdens stille periodes (alle pompen + compressor
# uit) daalt de kuip-temp. W/K-coëfficiënt = afkoeling genormaliseerd op
# (kuip - buiten). Dek open/dicht geeft spreiding tussen metingen.
DEFAULT_AMBIENT_SENSOR = "sensor.pool_heat_pump_temperature_2"  # Poolex DP21
TUB_WATER_KG = 1500        # watermassa voor energie/verlies-rekeningen
COOLDOWN_MIN_H = 3.0       # minimale meetduur voor een geldig venster
COOLDOWN_MIN_DROP_K = 0.5  # minimale daling om als meting te tellen
HEAT_LOSS_SAMPLES = 10     # bewaar de laatste N metingen
