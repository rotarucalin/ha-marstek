"""Constants for the Marstek Battery System integration."""

DOMAIN = "marstek"

# Config flow
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAX_PASSIVE_POWER = "max_passive_power"

# Default values
DEFAULT_PORT = 30000
DEFAULT_NAME = "Marstek Battery System"

# Device models
DEVICE_VENUS_C = "VenusC"
DEVICE_VENUS_E = "VenusE"
DEVICE_VENUS_D = "VenusD"

# Operating modes
MODE_AUTO = "Auto"
MODE_AI = "AI"
MODE_MANUAL = "Manual"
MODE_PASSIVE = "Passive"

OPERATING_MODES = [MODE_AUTO, MODE_AI, MODE_MANUAL, MODE_PASSIVE]

# Passive power control states
PASSIVE_STATE_UNKNOWN = "unknown"
PASSIVE_STATE_SENT = "sent"
PASSIVE_STATE_ACKNOWLEDGED = "acknowledged"
PASSIVE_STATE_RETRYING = "retrying"

PASSIVE_POWER_STATES = [
    PASSIVE_STATE_UNKNOWN,
    PASSIVE_STATE_SENT,
    PASSIVE_STATE_ACKNOWLEDGED,
    PASSIVE_STATE_RETRYING,
]

# Adaptive passive power compensation.
# The requested power is the desired real output; the value sent to the device
# is learned per direction and per 20 W bucket of desired output.
PASSIVE_BUCKET_WIDTH = 20
PASSIVE_DEADBAND_W = 10
PASSIVE_EMA_ALPHA = 0.15
PASSIVE_MAX_STEP_W = 150
PASSIVE_COMMAND_MIN = -3000
PASSIVE_COMMAND_MAX = 3000
PASSIVE_RESEND_THRESHOLD_W = 5

# The command range defaults to PASSIVE_COMMAND_MAX, but is configurable per
# entry (Config -> Options) for devices whose real limit is lower.
DEFAULT_MAX_PASSIVE_POWER = PASSIVE_COMMAND_MAX
MAX_PASSIVE_POWER_LIMIT = 30000

# Sample validity. One learning step needs a settled command plus several
# consecutive stable measurements, so it spans multiple 30s polls by design.
PASSIVE_SETTLE_SECONDS = 15
PASSIVE_STABILITY_SAMPLES = 3
PASSIVE_STABILITY_TOLERANCE_W = 25
PASSIVE_MIN_LEARN_POWER_W = 20
PASSIVE_SOC_LEARN_MIN = 5
PASSIVE_SOC_LEARN_MAX = 97

# Persisted calibration.
PASSIVE_SAVE_DELAY_SECONDS = 60
CALIBRATION_STORAGE_VERSION = 1
CALIBRATION_STORAGE_KEY_FMT = "marstek.{entry_id}.passive_calibration"

# Provenance of a command power value.
SOURCE_DIRECT = "direct"
SOURCE_CALIBRATION = "calibration"
SOURCE_INTERPOLATION = "interpolation"
SOURCE_EXTRAPOLATION = "extrapolation"
SOURCE_COMPENSATION = "compensation"
SOURCE_SATURATED = "saturated"

# Attributes
ATTR_DEVICE_MODEL = "device_model"
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_BLE_MAC = "ble_mac"
ATTR_WIFI_MAC = "wifi_mac"
