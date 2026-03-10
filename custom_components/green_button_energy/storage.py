"""Persistent storage helpers for Green Button Energy Import."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage schema — version 1
# ---------------------------------------------------------------------------
#
# The integration stores a single JSON object at STORAGE_KEY with the
# following keys. All keys are optional; missing keys are treated as their
# default values so the file degrades gracefully on first run.
#
#   electric_total       float   Cumulative kWh written to the DB since the
#                                integration was first set up. Used as the
#                                sensor's native_value on startup.
#
#   gas_total            float   Cumulative CCF written to the DB since the
#                                integration was first set up.
#
#   last_electric_time   str     UTC timestamp of the most recently *written*
#                                electric stat, in the format:
#                                "YYYY-MM-DD HH:MM:SS+00:00"
#                                Rows at or before this time are skipped on
#                                the next import to prevent duplicates.
#
#   last_gas_time        str     Same as last_electric_time but for gas.
#
#   last_processed_file  str     Filename (basename only) of the most recently
#                                processed import file. Informational only —
#                                not used for deduplication logic.
#
# Example stored value:
#   {
#     "electric_total": 4821.337,
#     "gas_total": 312.04,
#     "last_electric_time": "2026-03-07 23:00:00+00:00",
#     "last_gas_time": "2026-03-07 23:00:00+00:00",
#     "last_processed_file": "avangrid-em-rge_electric_60_Minute_03-01-2026.xml"
#   }
#
# ---------------------------------------------------------------------------
# Adding a new storage version (STORAGE_VERSION bump)
# ---------------------------------------------------------------------------
#
# If a future change requires a new key or restructured data:
#
#   1. Increment STORAGE_VERSION in const.py (e.g. 1 → 2).
#   2. Add a migration function below:
#
#       def _migrate_v1_to_v2(data: dict) -> dict:
#           """Example: rename a key from v1 to v2 schema."""
#           if "old_key" in data:
#               data["new_key"] = data.pop("old_key")
#           return data
#
#   3. Call it in load_store() before returning:
#
#       if store.version < 2:
#           data = _migrate_v1_to_v2(data)
#           await store.async_save(data)
#
#   HA's Store class handles the version field automatically — it saves
#   STORAGE_VERSION alongside the data and exposes it as store.version
#   so migration code can key off it.
#
# ---------------------------------------------------------------------------


async def load_store(hass: HomeAssistant) -> tuple[Store, dict[str, Any]]:
    """Load or initialise the integration's persistent storage."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    _LOGGER.debug("[green_button_energy] Storage loaded: %s", data)
    return store, data