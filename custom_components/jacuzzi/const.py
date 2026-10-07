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
DEFAULT_TEMP_MARGIN = 1.0
PEAK_MODE_SETPOINT = "setpoint"   # piek = laag setpoint, unit blijft aan
PEAK_MODE_OFF = "off"             # piek = hvac_mode off
DEFAULT_PEAK_MODE = PEAK_MODE_SETPOINT
DEFAULT_PEAK_SETPOINT = 4.0       # °C — laagste setpoint, unit idle
DEFAULT_NORMAL_SETPOINT = 38.0    # °C — fallback als geen opgeslagen setpoint
DEFAULT_SOLAR_MIN_W = 2000        # W — PV-overschot voor vervroegd piek-einde
DEFAULT_POOLEX_ALWAYS_ON = True   # buiten piek nooit hvac 'off' toestaan
DEFAULT_POOLEX_MAX_W = 1600       # W bij duty 100% — kalibreer met meter
SOLAR_SURPLUS_S = 600             # overschot moet 10 min aanhouden

# Massagepompen voor de meng-puls (vaste Gecko-ids; staan niet in config)
MIX_PUMPS = ("fan.jaccuzzi_pump_1", "fan.jaccuzzi_pump_2")
DEFAULT_MIX_ENABLED = True      # meng-puls tijdens het stoken
DEFAULT_MIX_INTERVAL_MIN = 30   # minuten tussen pulsen
DEFAULT_MIX_PULSE_S = 60        # pulsduur — genoeg om lagen te mengen

# Timers (seconds)
PUMP_RUNON_S = 180       # nadraaitijd pomp
FAILSAFE_DELAY_S = 15    # compressor aan + pomp uit -> pomp aan
MAINT_PUMP_GRACE_S = 90  # Gecko check-cyclus (~40 s) niet tegenwerken
MAINT_PUMP_RETRY_S = 300  # pack weigert turn_off tijdens eigen cyclus
PROBLEM_DELAY_S = 60     # fault-bit aan voordat we melden
NO_COMPRESSOR_S = 600    # warmtevraag zonder compressor -> verdacht
EVAL_INTERVAL_S = 30     # periodieke her-evaluatie van timers
UNREACHABLE_S = 900      # Poolex offline (PRCD-stekker/stroomstoring)
POOLEX_OFF_GUARD_S = 60  # hvac 'off' moet zo lang aanhouden voor de guard
RELOAD_COOLDOWN_S = 900  # max 1x per kwartier de Gecko-entry reloaden
