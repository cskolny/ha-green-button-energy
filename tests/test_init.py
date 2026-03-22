"""Tests for __init__.py — integration setup, teardown, and WebSocket handler.

The WebSocket handler (``ws_handle_import_file``) is tested by calling it
directly with a mock ``ActiveConnection`` rather than spinning up a full
WebSocket server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.green_button_energy.const import DOMAIN


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Set up the integration properly for testing.

    Uses async_setup_component which correctly transitions the config entry
    through NOT_LOADED -> LOADING -> LOADED states (required for
    async_forward_entry_setups to succeed).

    Patches:
    - ``homeassistant.setup.async_process_deps_reqs`` — skips dependency
      resolution so ``frontend``/``panel_custom`` (unavailable in CI) do not
      cause a DependencyError.
    - ``_async_register_panel`` — skips sidebar panel registration.
    - ``websocket_api.async_register_command`` — skips WS command registration.
    """
    from homeassistant.setup import async_setup_component

    mock_config_entry.add_to_hass(hass)
    with (
        patch("homeassistant.setup.async_process_deps_reqs"),
        patch(
            "custom_components.green_button_energy._async_register_panel",
            return_value=None,
        ),
        patch(
            "custom_components.green_button_energy.async_register_command"
        ),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()


def _mock_connection() -> MagicMock:
    """Return a MagicMock that mimics websocket_api.ActiveConnection."""
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# Integration setup / unload
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestIntegrationLifecycle:
    async def test_setup_entry_succeeds(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        assert mock_config_entry.entry_id in hass.data.get(DOMAIN, {})

    async def test_unload_entry_succeeds(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        result = await hass.config_entries.async_unload(mock_config_entry.entry_id)
        assert result is True

    async def test_panel_registered_flag_set(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """_async_register_panel must only be called once even on reload."""
        from homeassistant.setup import async_setup_component

        call_count = 0

        async def _fake_register(h: HomeAssistant) -> None:
            nonlocal call_count
            call_count += 1

        mock_config_entry.add_to_hass(hass)
        with (
            patch("homeassistant.setup.async_process_deps_reqs"),
            patch(
                "custom_components.green_button_energy._async_register_panel",
                side_effect=_fake_register,
            ),
            patch(
                "custom_components.green_button_energy.async_register_command"
            ),
        ):
            await async_setup_component(hass, DOMAIN, {})
            await hass.async_block_till_done()

        assert call_count == 1
        assert hass.data[DOMAIN].get("panel_registered") is True


# ---------------------------------------------------------------------------
# WebSocket handler — _find_sensor
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestFindSensor:
    async def test_find_electric_sensor(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        from custom_components.green_button_energy import _find_sensor

        await _setup_integration(hass, mock_config_entry)
        sensor = _find_sensor(hass, "electric")
        assert sensor is not None
        assert sensor.unique_id == "green_button_energy_electric_total"

    async def test_find_gas_sensor(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        from custom_components.green_button_energy import _find_sensor

        await _setup_integration(hass, mock_config_entry)
        sensor = _find_sensor(hass, "gas")
        assert sensor is not None
        assert sensor.unique_id == "green_button_energy_gas_total"

    async def test_find_sensor_unknown_type_returns_none(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        from custom_components.green_button_energy import _find_sensor

        await _setup_integration(hass, mock_config_entry)
        assert _find_sensor(hass, "steam") is None


# ---------------------------------------------------------------------------
# WebSocket handler — ws_handle_import_file
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestWsHandleImportFile:
    """Tests for the ws_handle_import_file WebSocket handler."""

    async def _call_handler(
        self,
        hass: HomeAssistant,
        msg: dict,
    ) -> MagicMock:
        """Call the unwrapped handler directly and return the mock connection.

        ``ws_handle_import_file`` is decorated with
        ``@websocket_api.async_response``, which wraps the coroutine and
        schedules it via ``hass.async_create_task``, returning ``None``
        rather than a coroutine.  Awaiting ``None`` raises ``TypeError``.
        ``.__wrapped__`` gives us the original coroutine function so we can
        await it directly in tests.
        """
        from custom_components.green_button_energy import ws_handle_import_file

        conn = _mock_connection()
        await ws_handle_import_file.__wrapped__(hass, conn, msg)
        await hass.async_block_till_done()
        return conn

    async def test_successful_csv_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)

        content = csv_electric_file.read_text(encoding="utf-8")
        conn = await self._call_handler(
            hass,
            {
                "id": 1,
                "type": "green_button_energy/import_file",
                "filename": "electric.csv",
                "content": content,
                "service_type": "electric",
            },
        )

        conn.send_result.assert_called_once()
        result_payload = conn.send_result.call_args[0][1]
        assert result_payload["success"] is True
        assert result_payload["rows_written"] == 3
        conn.send_error.assert_not_called()

    async def test_file_too_large_sends_error(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)

        oversized = "x" * (11 * 1024 * 1024)  # 11 MB
        conn = await self._call_handler(
            hass,
            {
                "id": 2,
                "type": "green_button_energy/import_file",
                "filename": "big.csv",
                "content": oversized,
                "service_type": "electric",
            },
        )

        conn.send_error.assert_called_once()
        error_code = conn.send_error.call_args[0][1]
        assert error_code == "file_too_large"
        conn.send_result.assert_not_called()

    async def test_unsupported_extension_sends_error(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)

        conn = await self._call_handler(
            hass,
            {
                "id": 3,
                "type": "green_button_energy/import_file",
                "filename": "data.txt",
                "content": "some content",
                "service_type": "electric",
            },
        )

        conn.send_error.assert_called_once()
        error_code = conn.send_error.call_args[0][1]
        assert error_code == "invalid_format"

    async def test_sensor_not_found_sends_error(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """If _find_sensor returns None the handler must send sensor_not_found."""
        await _setup_integration(hass, mock_config_entry)

        with patch(
            "custom_components.green_button_energy._find_sensor",
            return_value=None,
        ):
            conn = await self._call_handler(
                hass,
                {
                    "id": 4,
                    "type": "green_button_energy/import_file",
                    "filename": "electric.csv",
                    "content": "Name,Start Time,Usage\n",
                    "service_type": "electric",
                },
            )

        conn.send_error.assert_called_once()
        assert conn.send_error.call_args[0][1] == "sensor_not_found"

    async def test_path_traversal_in_filename_is_stripped(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        """Directory components in 'filename' must be stripped before use."""
        await _setup_integration(hass, mock_config_entry)

        content = csv_electric_file.read_text(encoding="utf-8")
        conn = await self._call_handler(
            hass,
            {
                "id": 5,
                "type": "green_button_energy/import_file",
                "filename": "../../etc/passwd",  # traversal attempt
                "content": content,
                "service_type": "electric",
            },
        )

        # The handler should proceed (basename is 'passwd' which has no .csv/.xml)
        # and return invalid_format, not crash or access /etc/passwd
        conn.send_error.assert_called_once()
        assert conn.send_error.call_args[0][1] == "invalid_format"

    async def test_already_imported_file_returns_rows_written_zero(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        """A fully-covered re-import must return success=True, rows_written=0."""
        await _setup_integration(hass, mock_config_entry)

        content = csv_electric_file.read_text(encoding="utf-8")
        msg = {
            "id": 6,
            "type": "green_button_energy/import_file",
            "filename": "electric.csv",
            "content": content,
            "service_type": "electric",
        }

        # First import
        await self._call_handler(hass, {**msg, "id": 6})

        # Second import of the same file
        conn = await self._call_handler(hass, {**msg, "id": 7})

        conn.send_result.assert_called_once()
        result_payload = conn.send_result.call_args[0][1]
        assert result_payload["success"] is True
        assert result_payload["rows_written"] == 0

    async def test_temp_file_deleted_after_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        """The temporary file written by the handler must be cleaned up."""
        await _setup_integration(hass, mock_config_entry)

        written_paths: list[str] = []
        original_unlink = __import__("os").unlink

        def _tracking_unlink(path: str) -> None:
            written_paths.append(path)
            original_unlink(path)

        content = csv_electric_file.read_text(encoding="utf-8")
        with patch("custom_components.green_button_energy.os.unlink", side_effect=_tracking_unlink):
            await self._call_handler(
                hass,
                {
                    "id": 8,
                    "type": "green_button_energy/import_file",
                    "filename": "electric.csv",
                    "content": content,
                    "service_type": "electric",
                },
            )

        assert len(written_paths) == 1
        assert not Path(written_paths[0]).exists()
