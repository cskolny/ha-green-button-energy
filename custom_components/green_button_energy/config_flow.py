"""Config flow for the Green Button Energy Import integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GreenButtonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for Green Button Energy Import."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """One-click setup — no fields required."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Green Button Energy Import",
            data={},
        )