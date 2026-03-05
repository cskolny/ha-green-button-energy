"""Persistent storage helpers for Green Button Energy Import."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


async def load_store(hass: HomeAssistant) -> tuple[Store, dict[str, Any]]:
    """Load or initialise the integration's persistent storage."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    _LOGGER.debug("[green_button_energy] Storage loaded: %s", data)
    return store, data