"""Green Button Energy Import — Home Assistant Custom Integration.

Imports hourly smart-meter usage data from Avangrid utility Green Button
CSV/XML exports into the Home Assistant Energy Dashboard via a drag-and-drop
sidebar panel.

Supported utilities: RG&E, NYSEG, Central Maine Power, United Illuminating,
Connecticut Natural Gas, Southern Connecticut Gas, Berkshire Gas.

No ``configuration.yaml`` changes are needed.  The sidebar panel is registered
automatically when the integration is added via **Settings → Devices & Services**.
"""

from __future__ import annotations

import logging
import os
import pathlib
import tempfile
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.websocket_api import (
    ActiveConnection,
    async_register_command,
    async_response,
    websocket_command,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

if TYPE_CHECKING:
    from .sensor import GreenButtonSensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

_FRONTEND_DIR = pathlib.Path(__file__).parent / "frontend"
_PANEL_JS = "green-button-energy-panel.js"
# URL prefix under which HA's HTTP server serves the frontend/ directory.
_PANEL_URL = f"/{DOMAIN}_frontend"

# Maximum file-content size accepted over WebSocket (10 MB as UTF-8 text).
# Prevents memory pressure on resource-constrained hosts from oversized or
# malformed uploads.  Must stay in sync with ``_MAX_FILE_BYTES`` in the JS.
_MAX_FILE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Integration lifecycle
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the WebSocket command once at HA startup.

    Args:
        hass: The Home Assistant instance.
        config: Full ``configuration.yaml`` contents (unused by this integration).

    Returns:
        Always ``True``.
    """
    hass.data.setdefault(DOMAIN, {})
    async_register_command(hass, ws_handle_import_file)
    _LOGGER.info("[%s] Setup complete — WebSocket command registered.", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up sensors and register the sidebar panel from a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being loaded.

    Returns:
        ``True`` on success.
    """
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Guard against duplicate panel registration when the entry is reloaded.
    if not hass.data[DOMAIN].get("panel_registered"):
        await _async_register_panel(hass)
        hass.data[DOMAIN]["panel_registered"] = True

    _LOGGER.info("[%s] Config entry loaded: %s", DOMAIN, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and clean up associated data.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        ``True`` when all platforms unloaded successfully.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("[%s] Config entry unloaded: %s", DOMAIN, entry.entry_id)
    return unload_ok


# ---------------------------------------------------------------------------
# Sidebar panel registration
# ---------------------------------------------------------------------------


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel and serve the frontend JS via HA's HTTP server.

    Serves the ``frontend/`` directory directly from the integration package —
    no copying to ``config/www/`` is required or performed.

    Args:
        hass: The Home Assistant instance.
    """
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path=_PANEL_URL,
                path=str(_FRONTEND_DIR),
                # Disable caching so the browser always fetches the latest JS
                # after an integration update without requiring a hard refresh.
                cache_headers=False,
            )
        ],
    )

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="green-button-energy-panel",  # matches customElements.define()
        frontend_url_path=DOMAIN,                        # sidebar URL: /green_button_energy
        sidebar_title="Energy Import",
        sidebar_icon="mdi:lightning-bolt-circle",
        module_url=f"{_PANEL_URL}/{_PANEL_JS}",
        embed_iframe=False,
        require_admin=False,
    )

    _LOGGER.info("[%s] Sidebar panel registered at /%s", DOMAIN, DOMAIN)


# ---------------------------------------------------------------------------
# WebSocket command handler
# ---------------------------------------------------------------------------


@websocket_command(
    {
        vol.Required("type"): "green_button_energy/import_file",
        vol.Required("filename"): str,
        vol.Required("content"): str,
        vol.Required("service_type"): vol.In(["electric", "gas"]),
    },
)
@async_response
async def ws_handle_import_file(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle an ``import_file`` WebSocket message from the sidebar panel.

    Validates the payload, writes a temporary file, delegates parsing and
    statistics import to the appropriate sensor, and returns a structured
    result or error to the frontend.

    Args:
        hass: The Home Assistant instance.
        connection: The active WebSocket connection.
        msg: The validated message dict from the frontend.
    """
    msg_id = msg["id"]
    # Strip directory components to prevent path-traversal attacks.
    filename: str = pathlib.Path(msg["filename"]).name
    content: str = msg["content"]
    service_type: str = msg["service_type"]

    # Reject oversized payloads before doing any further work.
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > _MAX_FILE_BYTES:
        _LOGGER.warning(
            "[%s] Rejected '%s': %d bytes exceeds %d-byte limit.",
            DOMAIN,
            filename,
            content_bytes,
            _MAX_FILE_BYTES,
        )
        connection.send_error(
            msg_id,
            "file_too_large",
            f"File '{filename}' is too large ({content_bytes // 1024} KB). "
            f"Maximum allowed size is {_MAX_FILE_BYTES // (1024 * 1024)} MB. "
            "Please split the export into smaller date ranges.",
        )
        return

    _LOGGER.info(
        "[%s] Import request: file='%s', type='%s', size=%d bytes",
        DOMAIN,
        filename,
        service_type,
        content_bytes,
    )

    ext = pathlib.Path(filename).suffix.lower()
    if ext not in {".csv", ".xml"}:
        connection.send_error(
            msg_id,
            "invalid_format",
            f"Unsupported file type '{ext}'. Please use .csv or .xml.",
        )
        return

    sensor = _find_sensor(hass, service_type)
    if sensor is None:
        connection.send_error(
            msg_id,
            "sensor_not_found",
            f"No Green Button sensor found for service_type='{service_type}'. "
            "Is the integration configured under Settings → Devices & Services?",
        )
        return

    def _write_temp() -> str:
        """Write file content to a named temporary file and return its path."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=ext,
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(content)
            return tmp.name

    tmp_path = await hass.async_add_executor_job(_write_temp)

    try:
        await sensor.async_process_file(tmp_path)
        result = sensor.last_result
        rows_written = sensor.last_rows_written

        if result is None:
            connection.send_result(
                msg_id,
                {
                    "success": True,
                    "rows_imported": 0,
                    "rows_written": 0,
                    "new_usage": 0.0,
                    "unit": sensor.native_unit_of_measurement,
                    "newest_time": "",
                },
            )
        elif result.errors:
            connection.send_result(
                msg_id,
                {
                    "success": False,
                    "error": "; ".join(result.errors),
                    "rows_imported": result.rows_imported,
                    "rows_written": 0,
                    "rows_skipped": result.rows_skipped,
                    "unit": sensor.native_unit_of_measurement,
                },
            )
        elif rows_written == 0:
            # Parser found rows but all were already present in the DB.
            connection.send_result(
                msg_id,
                {
                    "success": True,
                    "rows_imported": result.rows_imported,
                    "rows_written": 0,
                    "new_usage": 0.0,
                    "newest_time": result.newest_time,
                    "unit": sensor.native_unit_of_measurement,
                },
            )
        else:
            connection.send_result(
                msg_id,
                {
                    "success": True,
                    "rows_imported": result.rows_imported,
                    "rows_written": rows_written,
                    "rows_skipped": result.rows_skipped,
                    "new_usage": round(result.new_usage, 4),
                    "newest_time": result.newest_time,
                    "unit": sensor.native_unit_of_measurement,
                },
            )
    finally:
        await hass.async_add_executor_job(os.unlink, tmp_path)


def _find_sensor(hass: HomeAssistant, service_type: str) -> "GreenButtonSensor | None":
    """Locate the :class:`~.sensor.GreenButtonSensor` for *service_type*.

    Searches ``hass.data[DOMAIN]`` for a config-entry dict that contains a
    sensor registered under the given service key.

    Args:
        hass: The Home Assistant instance.
        service_type: ``"electric"`` or ``"gas"``.

    Returns:
        The matching sensor instance, or ``None`` if not found.
    """
    domain_data: dict[str, Any] = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if isinstance(entry_data, dict):
            sensor: GreenButtonSensor | None = entry_data.get(service_type.lower())
            if sensor is not None:
                return sensor
    return None
