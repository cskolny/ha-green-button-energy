"""Config flow for the Green Button Energy Import integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GreenButtonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One-click setup flow — no user input required.

    The integration has no configuration options; a single submit creates the
    entry and registers the sidebar panel.  A unique-ID guard prevents the
    user from adding the integration twice.
    """

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the initial setup step.

        Args:
            user_input: Always ``None`` on first presentation; any non-None
                value triggers entry creation immediately (no fields to
                validate).

        Returns:
            A :func:`~homeassistant.config_entries.ConfigFlow.async_create_entry`
            result, or an abort result when already configured.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Green Button Energy Import",
            data={},
        )