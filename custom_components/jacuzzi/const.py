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

DEFAULT_POOLEX_CLIMATE = "climate.pool_heat_pump"
DEFAULT_JACUZZI_CLIMATE = "climate.jaccuzzi_thermostat_1"
DEFAULT_PUMP_FAN = "fan.jaccuzzi_waterfall"
DEFAULT_COMPRESSOR_SENSOR = "sensor.pool_heat_pump_compressor_duty_cycle"
DEFAULT_PROBLEM_SENSOR = "binary_sensor.pool_heat_pump_problem"
DEFAULT_WATERCARE_SELECT = "select.jaccuzzi_watercare_mode"
DEFAULT_NOTIFY_SERVICE = "notify"
DEFAULT_PEAK_START = "16:30"
DEFAULT_PEAK_END = "20:00"
DEFAULT_WATERCARE_PEAK = "Super Savings"
DEFAULT_WATERCARE_NORMAL = "Savings"
DEFAULT_TEMP_MARGIN = 1.0

# Timers (seconds)
PUMP_RUNON_S = 180       # nadraaitijd pomp
FAILSAFE_DELAY_S = 15    # compressor aan + pomp uit -> pomp aan
PROBLEM_DELAY_S = 60     # fault-bit aan voordat we melden
NO_COMPRESSOR_S = 600    # warmtevraag zonder compressor -> verdacht
EVAL_INTERVAL_S = 30     # periodieke her-evaluatie van timers
