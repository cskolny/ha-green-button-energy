"""
Persistent storage for RG&E Green Button integration.

Uses Home Assistant's built-in Store helper so data survives restarts
and is written atomically to .storage/rge_green_button_data.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    ELECTRIC_SENSOR_KEY,
    ELECTRIC_TIME_KEY,
    GAS_SENSOR_KEY,
    GAS_TIME_KEY,
    LAST_FILE_KEY,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_DATA: dict[str, Any] = {
    ELECTRIC_SENSOR_KEY: 0.0,
    GAS_SENSOR_KEY: 0.0,
    ELECTRIC_TIME_KEY: "",
    GAS_TIME_KEY: "",
    LAST_FILE_KEY: "",
}


async def load_store(hass: HomeAssistant) -> tuple[Store, dict[str, Any]]:
    """
    Load persisted data from HA storage.

    Returns a (Store, data) tuple. The data dict always contains all
    expected keys — missing keys are backfilled from DEFAULT_DATA so
    the integration degrades gracefully after an upgrade.
    """
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    raw: dict[str, Any] | None = await store.async_load()

    if raw is None:
        _LOGGER.debug("[%s] No persisted data found; starting fresh.", DOMAIN)
        data = dict(DEFAULT_DATA)
    else:
        # Backfill any keys added in newer versions
        data = {**DEFAULT_DATA, **raw}
        if data != raw:
            _LOGGER.debug("[%s] Backfilled missing storage keys after version upgrade.", DOMAIN)

    return store, data
