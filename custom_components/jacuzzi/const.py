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
CONF_JACUZZI_POWER_SENSOR = "jacuzzi_power_sensor"
CONF_POOLEX_ALWAYS_ON = "poolex_always_on"

DEFAULT_POOLEX_CLIMATE = "climate.pool_heat_pump"
DEFAULT_JACUZZI_CLIMATE = "climate.jaccuzzi_thermostat_1"
DEFAULT_PUMP_FAN = "fan.jaccuzzi_waterfall"
DEFAULT_COMPRESSOR_SENSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
DEFAULT_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
DEFAULT_WATERCARE_SELECT = "select.jaccuzzi_watercare_mode"
DEFAULT_NOTIFY_SERVICE = "notify"
DEFAULT_PEAK_START = "16:30"
DEFAULT_PEAK_END = "20:00"
DEFAULT_WATERCARE_PEAK = "Super Savings"   # moet exact matchen met select-options
DEFAULT_WATERCARE_NORMAL = "Savings"
DEFAULT_TEMP_MARGIN = 1.0
PEAK_MODE_SETPOINT = "setpoint"   # piek = laag setpoint, unit blijft aan
PEAK_MODE_OFF = "off"             # piek = hvac_mode off
DEFAULT_PEAK_MODE = PEAK_MODE_SETPOINT
DEFAULT_PEAK_SETPOINT = 4.0       # °C — laagste setpoint, unit idle
DEFAULT_NORMAL_SETPOINT = 38.0    # °C — fallback als geen opgeslagen setpoint
DEFAULT_SOLAR_MIN_W = 2000        # W — PV-overschot voor vervroegd piek-einde
DEFAULT_POOLEX_ALWAYS_ON = True   # buiten piek nooit hvac 'off' toestaan
SOLAR_SURPLUS_S = 600             # overschot moet 10 min aanhouden

# Timers (seconds)
PUMP_RUNON_S = 180       # nadraaitijd pomp
FAILSAFE_DELAY_S = 15    # compressor aan + pomp uit -> pomp aan
PROBLEM_DELAY_S = 60     # fault-bit aan voordat we melden
NO_COMPRESSOR_S = 600    # warmtevraag zonder compressor -> verdacht
EVAL_INTERVAL_S = 30     # periodieke her-evaluatie van timers
UNREACHABLE_S = 900      # Poolex offline (PRCD-stekker/stroomstoring)
POOLEX_OFF_GUARD_S = 60  # hvac 'off' moet zo lang aanhouden voor de guard
RELOAD_COOLDOWN_S = 900  # max 1x per kwartier de Gecko-entry reloaden
