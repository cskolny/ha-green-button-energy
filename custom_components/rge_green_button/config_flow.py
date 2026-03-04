"""
Config flow for the RG&E Green Button integration.

Minimal setup — no folder paths needed. The integration is fully
self-contained: the sidebar panel and WebSocket handler are registered
automatically, and no configuration.yaml changes are required.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class RGEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for RG&E Green Button."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """One-click setup — no fields required."""
        if user_input is not None or True:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="RG&E Green Button",
                data={},
            )

        return self.async_show_form(step_id="user")
