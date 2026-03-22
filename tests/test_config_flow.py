"""Integration tests for the Green Button Energy config flow.

Uses the ``hass`` fixture from pytest-homeassistant-custom-component plus
HA's FlowResultType to assert on the outcome of each step.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.green_button_energy.const import DOMAIN


@pytest.mark.usefixtures("enable_custom_integrations")
class TestConfigFlow:
    """Tests for GreenButtonConfigFlow."""

    async def test_user_step_creates_entry(self, hass: HomeAssistant) -> None:
        """Submitting the user step with no input must create a config entry."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Green Button Energy Import"
        assert result["data"] == {}

    async def test_already_configured_aborts(self, hass: HomeAssistant) -> None:
        """A second setup attempt must be aborted with already_configured."""
        # First setup
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        # Second attempt
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"

    async def test_entry_has_correct_unique_id(self, hass: HomeAssistant) -> None:
        """The config entry must carry the domain as its unique ID."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        assert entries[0].unique_id == DOMAIN
