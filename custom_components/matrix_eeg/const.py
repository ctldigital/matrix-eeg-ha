"""Constants of the MATRIX EEG integration."""

# Hard upper bound for any setpoint the integration writes. The local limit option may only lower it.
MAX_TARGET_C = 60

DOMAIN = "matrix_eeg"
DEFAULT_URL = "mqtts://mqtt.matrix.ctl.digital:8883"

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DHW_TARGET = "dhw_target"
CONF_DHW_TEMP = "dhw_temp"
CONF_POWER = "power"
CONF_HEATER_ROD = "heater_rod"
CONF_DHW_MAX = "dhw_max"
CONF_LIMIT_C = "limit_c"
