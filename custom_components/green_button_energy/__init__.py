"""
Green Button Energy Import — Home Assistant Custom Integration.

Imports hourly smart meter usage data from Avangrid utility Green Button
CSV/XML exports into the Home Assistant Energy Dashboard via a drag-and-drop
sidebar panel.

Supported utilities: RG&E, NYSEG, Central Maine Power, United Illuminating,
Connecticut Natural Gas, Southern Connecticut Gas, Berkshire Gas.

No configuration.yaml changes are needed. The sidebar panel is registered
automatically when the integration is added via Settings → Devices & Services.
"""

from __future__ import annotations

import logging
import os
import pathlib
import tempfile

import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

_FRONTEND_DIR = pathlib.Path(__file__).parent / "frontend"
_PANEL_JS      = "green-button-energy-panel.js"
_PANEL_URL     = f"/{DOMAIN}_frontend"   # Static path served by HA's HTTP server

# Maximum file content size accepted over WebSocket (10 MB as UTF-8 text).
# Prevents memory pressure on the Pi from oversized or malformed uploads.
_MAX_FILE_BYTES = 10 * 1024 * 1024


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the WebSocket command (runs once at HA startup)."""
    hass.data.setdefault(DOMAIN, {})
    websocket_api.async_register_command(hass, ws_handle_import_file)
    _LOGGER.info("[%s] Setup complete — WebSocket command registered.", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up sensors and register the sidebar panel from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the sidebar panel programmatically — no configuration.yaml needed.
    # Guard against duplicate registration if the entry is reloaded.
    if not hass.data[DOMAIN].get("panel_registered"):
        await _async_register_panel(hass)
        hass.data[DOMAIN]["panel_registered"] = True

    _LOGGER.info("[%s] Config entry loaded: %s", DOMAIN, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("[%s] Config entry unloaded: %s", DOMAIN, entry.entry_id)
    return unload_ok


async def _async_register_panel(hass: HomeAssistant) -> None:
    """
    Register the sidebar panel and serve the frontend JS via HA's HTTP server.

    This replaces the panel_custom configuration.yaml entry entirely.
    The JS file is served directly from the integration's frontend/ directory —
    no copying to config/www/ is needed or performed.
    """
    # Register the frontend/ directory as a static path served by HA's HTTP server.
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            url_path=_PANEL_URL,
            path=str(_FRONTEND_DIR),
            cache_headers=False,   # Disable caching so updates appear immediately
        )
    ])

    # Register the panel — programmatic equivalent of panel_custom in YAML.
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="green-button-energy-panel",  # Matches customElements.define()
        frontend_url_path=DOMAIN,                       # Sidebar URL: /green_button_energy
        sidebar_title="Energy Import",
        sidebar_icon="mdi:lightning-bolt-circle",
        module_url=f"{_PANEL_URL}/{_PANEL_JS}",
        embed_iframe=False,
        require_admin=False,
    )

    _LOGGER.info("[%s] Sidebar panel registered at /%s", DOMAIN, DOMAIN)


# ---------------------------------------------------------------------------
# WebSocket command
# ---------------------------------------------------------------------------

@websocket_api.websocket_command(
    {
        vol.Required("type"): "green_button_energy/import_file",
        vol.Required("filename"): str,
        vol.Required("content"): str,
        vol.Required("service_type"): vol.In(["electric", "gas"]),
    }
)
@websocket_api.async_response
async def ws_handle_import_file(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Handle an import_file WebSocket command from the frontend panel."""
    msg_id       = msg["id"]
    # Strip any directory components from the filename — defensive measure
    # to prevent path traversal if this field is ever used in a file path.
    filename     = pathlib.Path(msg["filename"]).name
    content      = msg["content"]
    service_type = msg["service_type"]

    # Reject files that exceed the size limit before doing any further work.
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > _MAX_FILE_BYTES:
        _LOGGER.warning(
            "[%s] Rejected '%s': content size %d bytes exceeds %d byte limit.",
            DOMAIN, filename, content_bytes, _MAX_FILE_BYTES,
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
        DOMAIN, filename, service_type, content_bytes,
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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=ext, encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(content)
            return tmp.name

    tmp_path = await hass.async_add_executor_job(_write_temp)

    try:
        await sensor.async_process_file(tmp_path)
        result = sensor.last_result

        if result is None:
            connection.send_result(msg_id, {
                "success": True,
                "rows_imported": 0,
                "new_usage": 0.0,
                "unit": sensor.native_unit_of_measurement,
                "newest_time": "",
            })
        elif result.errors:
            connection.send_result(msg_id, {
                "success": False,
                "error": "; ".join(result.errors),
                "rows_imported": result.rows_imported,
                "rows_skipped": result.rows_skipped,
                "unit": sensor.native_unit_of_measurement,
            })
        else:
            connection.send_result(msg_id, {
                "success": True,
                "rows_imported": result.rows_imported,
                "rows_skipped": result.rows_skipped,
                "new_usage": round(result.new_usage, 4),
                "newest_time": result.newest_time,
                "unit": sensor.native_unit_of_measurement,
            })
    finally:
        await hass.async_add_executor_job(os.unlink, tmp_path)


def _find_sensor(hass: HomeAssistant, service_type: str):
    """Find the GreenButtonSensor instance for the given service_type."""
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if isinstance(entry_data, dict):
            sensor = entry_data.get(service_type.lower())
            if sensor is not None:
                return sensor
    return None