"""Tests for storage.py — the persistent-storage helper."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.green_button_energy.const import (
    DOMAIN,
    ELECTRIC_SENSOR_KEY,
    ELECTRIC_TIME_KEY,
    GAS_SENSOR_KEY,
    GAS_TIME_KEY,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.green_button_energy.storage import load_store


@pytest.mark.usefixtures("enable_custom_integrations")
class TestLoadStore:
    """Tests for the load_store coroutine."""

    async def test_returns_empty_dict_on_first_run(
        self, hass: HomeAssistant
    ) -> None:
        """On first run (no existing storage file) data must be an empty dict."""
        _store, data = await load_store(hass)
        assert isinstance(data, dict)
        assert data == {}

    async def test_returns_store_instance(self, hass: HomeAssistant) -> None:
        from homeassistant.helpers.storage import Store

        store, _ = await load_store(hass)
        assert isinstance(store, Store)

    async def test_persists_and_reloads_data(self, hass: HomeAssistant) -> None:
        """Data saved via the returned store must be readable on next load."""
        store, data = await load_store(hass)
        data[ELECTRIC_SENSOR_KEY] = 1234.5
        data[ELECTRIC_TIME_KEY] = "2026-03-01 05:00:00+00:00"
        await store.async_save(data)

        # Reload from the same storage key
        _store2, data2 = await load_store(hass)
        assert data2[ELECTRIC_SENSOR_KEY] == 1234.5
        assert data2[ELECTRIC_TIME_KEY] == "2026-03-01 05:00:00+00:00"

    async def test_storage_key_and_version(self, hass: HomeAssistant) -> None:
        """The Store must use the constants from const.py."""
        from homeassistant.helpers.storage import Store

        store, _ = await load_store(hass)
        # Verify the store was created with the right key/version by inspecting
        # the internal attribute that HA's Store exposes.
        assert store.key == STORAGE_KEY
        assert store.version == STORAGE_VERSION

    async def test_missing_keys_default_gracefully(
        self, hass: HomeAssistant
    ) -> None:
        """Callers using data.get(key, default) must not raise on a fresh store."""
        _, data = await load_store(hass)
        assert data.get(ELECTRIC_SENSOR_KEY, 0.0) == 0.0
        assert data.get(GAS_SENSOR_KEY, 0.0) == 0.0
        assert data.get(ELECTRIC_TIME_KEY, "") == ""
        assert data.get(GAS_TIME_KEY, "") == ""
