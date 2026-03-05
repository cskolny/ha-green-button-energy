"""Constants for the Green Button Energy Import integration."""

DOMAIN = "green_button_energy"

# Storage
STORAGE_KEY = f"{DOMAIN}_data"
STORAGE_VERSION = 1

# Storage data keys
ELECTRIC_SENSOR_KEY = "electric_total"
GAS_SENSOR_KEY      = "gas_total"
ELECTRIC_TIME_KEY   = "last_electric_time"
GAS_TIME_KEY        = "last_gas_time"
LAST_FILE_KEY       = "last_processed_file"

# Sensor names and unique IDs
SENSOR_ELECTRIC_NAME = "Avangrid Electric Total"
SENSOR_GAS_NAME      = "Avangrid Gas Total"
SENSOR_ELECTRIC_UID  = f"{DOMAIN}_electric_total"
SENSOR_GAS_UID       = f"{DOMAIN}_gas_total"

# Units
UNIT_ELECTRIC = "kWh"
UNIT_GAS      = "CCF"   # Therms ≈ CCF; HA gas device class requires a volume unit

# Supported file extensions
SUPPORTED_EXTENSIONS = {".csv", ".xml"}

# Notification IDs
NOTIF_SUCCESS = f"{DOMAIN}_import_success"
NOTIF_ERROR   = f"{DOMAIN}_import_error"

# Config entry keys
CONF_ELECTRIC_KEYWORD = "electric_keyword"
CONF_GAS_KEYWORD      = "gas_keyword"