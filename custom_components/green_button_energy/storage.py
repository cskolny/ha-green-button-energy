"""Persistent storage helpers for the Green Button Energy Import integration.

Storage schema — version 1
---------------------------
The integration stores a single JSON object under ``STORAGE_KEY`` with the
following optional keys.  Missing keys are treated as their default values,
so the store degrades gracefully on first run.

``electric_total`` (:class:`float`)
    Cumulative kWh written to the recorder DB since the integration was first
    configured.  Used to populate the sensor's ``native_value`` at startup.

``gas_total`` (:class:`float`)
    Cumulative CCF written to the recorder DB since first setup.

``last_electric_time`` (:class:`str`)
    UTC timestamp (``"YYYY-MM-DD HH:MM:SS+00:00"``) of the most recently
    *written* electric statistic.  Any row at or before this timestamp is
    skipped on the next import to prevent duplicate entries.

``last_gas_time`` (:class:`str`)
    Same as ``last_electric_time``, but for the gas sensor.

``last_processed_file`` (:class:`str`)
    Basename of the most recently processed import file.  Informational only
    — not used in deduplication logic.

Example stored value::

    {
      "electric_total": 4821.337,
      "gas_total": 312.04,
      "last_electric_time": "2026-03-07 23:00:00+00:00",
      "last_gas_time": "2026-03-07 23:00:00+00:00",
      "last_processed_file": "avangrid-em-rge_electric_60_Minute_03-01-2026.xml"
    }

Adding a new storage version
-----------------------------
When a future change requires a new key or restructured data:

1. Increment ``STORAGE_VERSION`` in ``const.py`` (e.g. 1 → 2).
2. Add a migration function below::

       def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
           if "old_key" in data:
               data["new_key"] = data.pop("old_key")
           return data

3. Call it in :func:`load_store` before returning::

       if store.version < 2:
           data = _migrate_v1_to_v2(data)
           await store.async_save(data)

HA's :class:`~homeassistant.helpers.storage.Store` saves ``STORAGE_VERSION``
alongside the data and exposes it as ``store.version`` so migration code can
branch on it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


async def load_store(hass: HomeAssistant) -> tuple[Store[dict[str, Any]], dict[str, Any]]:
    """Load or initialise the integration's persistent storage.

    Creates the :class:`~homeassistant.helpers.storage.Store` and reads the
    current data from disk.  Returns an empty dict on first run so all callers
    can use ``data.get(key, default)`` safely without null-checking.

    Args:
        hass: The Home Assistant instance.

    Returns:
        A ``(store, data)`` tuple where *store* is the open
        :class:`~homeassistant.helpers.storage.Store` and *data* is the
        deserialized JSON object (never ``None``).
    """
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data: dict[str, Any] = await store.async_load() or {}
    _LOGGER.debug("[green_button_energy] Storage loaded: %s", data)
    return store, data
