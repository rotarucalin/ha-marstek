"""Constants for the Marstek Battery System integration."""

DOMAIN = "marstek"

# Config flow
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAX_PASSIVE_POWER = "max_passive_power"

# Default values
DEFAULT_PORT = 30000
DEFAULT_NAME = "Marstek Battery System"

# Device models, as named in chapter 4 of the Marstek Device Open API (Rev 3.1).
# Firmware reports these with varying spelling ("VenusC", "Venus C", and the
# "VenusC-123456789012" form used by `src`), so never compare against them
# directly; resolve capabilities through .capabilities instead.
DEVICE_VENUS_A = "Venus A"
DEVICE_VENUS_C = "Venus C"
DEVICE_VENUS_D = "Venus D"
DEVICE_VENUS_E = "Venus E"
DEVICE_VENUS_E_MINI = "Venus E mini"

# Operating modes
MODE_AUTO = "Auto"
MODE_AI = "AI"
MODE_MANUAL = "Manual"
MODE_PASSIVE = "Passive"
# The API also documents "Ups". It is deliberately not offered yet.
# The list of modes the integration exposes lives in capabilities.ALL_ES_MODES,
# so a model's supported modes have exactly one source of truth.

# Manual mode addresses one schedule slot per command through `time_num`.
# Venus A/C/D/E support 0-9; Venus E mini supports 0-5.
MANUAL_SLOTS_DEFAULT = 10
MANUAL_SLOTS_E_MINI = 6
# Selecting Manual from the mode select entity writes this single slot.
MANUAL_DEFAULT_SLOT = 0

# `manual_set` is an extra manual_cfg field accepted only by the Venus E mini.
MANUAL_SET_DISABLE = 0
MANUAL_SET_CHARGE = 1
MANUAL_SET_DISCHARGE = 2
MANUAL_SET_AUTO = 3

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
