"""Constants for the Green Button Energy Import integration."""

# ── Integration identity ───────────────────────────────────────────────────
DOMAIN = "green_button_energy"

# ── Persistent storage ─────────────────────────────────────────────────────
STORAGE_KEY = f"{DOMAIN}_data"
STORAGE_VERSION = 1

# Storage data keys — must match the JSON schema documented in storage.py.
ELECTRIC_SENSOR_KEY = "electric_total"
GAS_SENSOR_KEY = "gas_total"
ELECTRIC_TIME_KEY = "last_electric_time"
GAS_TIME_KEY = "last_gas_time"
LAST_FILE_KEY = "last_processed_file"

# ── Sensor display names and unique identifiers ────────────────────────────
SENSOR_ELECTRIC_NAME = "Avangrid Electric Total"
SENSOR_GAS_NAME = "Avangrid Gas Total"
SENSOR_ELECTRIC_UID = f"{DOMAIN}_electric_total"
SENSOR_GAS_UID = f"{DOMAIN}_gas_total"

# ── Units of measurement ───────────────────────────────────────────────────
UNIT_ELECTRIC = "kWh"
# Therms ≈ CCF; HA's gas device_class requires a volume unit, so we store as CCF.
UNIT_GAS = "CCF"

# ── Accepted file extensions ───────────────────────────────────────────────
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".xml"})

# ── Persistent notification IDs ────────────────────────────────────────────
NOTIF_SUCCESS = f"{DOMAIN}_import_success"
NOTIF_ERROR = f"{DOMAIN}_import_error"