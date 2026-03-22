"""Tests for sensor.py — the GreenButtonSensor entity and statistics import.

These tests use the HA ``recorder_mock`` fixture so that
``async_import_statistics`` and ``get_last_statistics`` work against a real
(in-memory) recorder database rather than mocks.

Important fixture ordering: ``recorder_mock`` must come before
``enable_custom_integrations`` — see pytest-homeassistant-custom-component #132.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.green_button_energy.const import (
    DOMAIN,
    ELECTRIC_SENSOR_KEY,
    ELECTRIC_TIME_KEY,
    GAS_SENSOR_KEY,
    GAS_TIME_KEY,
    SENSOR_ELECTRIC_UID,
    SENSOR_GAS_UID,
    UNIT_ELECTRIC,
    UNIT_GAS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Add the mock entry and set up the integration."""
    mock_config_entry.add_to_hass(hass)
    # Patch panel registration so we don't need the frontend component loaded.
    with (
        patch(
            "custom_components.green_button_energy._async_register_panel",
            return_value=None,
        ),
        patch(
            "custom_components.green_button_energy.websocket_api.async_register_command"
        ),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()


def _get_sensor(hass: HomeAssistant, service_type: str):
    """Return the GreenButtonSensor instance for service_type."""
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if isinstance(entry_data, dict):
            sensor = entry_data.get(service_type)
            if sensor is not None:
                return sensor
    raise KeyError(f"Sensor for service_type='{service_type}' not found in hass.data")


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestSensorSetup:
    """Tests for sensor entity registration."""

    async def test_both_sensors_registered(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Both electric and gas sensors must appear in hass.data after setup."""
        await _setup_integration(hass, mock_config_entry)
        electric = _get_sensor(hass, "electric")
        gas = _get_sensor(hass, "gas")
        assert electric is not None
        assert gas is not None

    async def test_electric_sensor_attributes(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")
        assert sensor.native_unit_of_measurement == UNIT_ELECTRIC
        assert sensor.unique_id == SENSOR_ELECTRIC_UID

    async def test_gas_sensor_attributes(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "gas")
        assert sensor.native_unit_of_measurement == UNIT_GAS
        assert sensor.unique_id == SENSOR_GAS_UID

    async def test_native_value_is_none_at_startup(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """native_value must stay None to prevent recorder boundary stat poisoning."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")
        assert sensor.native_value is None

    async def test_unload_entry_cleans_up(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        # Entry-specific data must be removed; the domain key may still exist.
        domain_data = hass.data.get(DOMAIN, {})
        assert mock_config_entry.entry_id not in domain_data


# ---------------------------------------------------------------------------
# async_process_file — CSV
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestProcessFileCsv:
    """Tests for GreenButtonSensor.async_process_file with CSV input."""

    async def test_csv_electric_import_writes_stats(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()

        assert sensor.last_result is not None
        assert sensor.last_result.success
        assert sensor.last_rows_written == 3

    async def test_csv_gas_import_writes_stats(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_gas_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "gas")

        await sensor.async_process_file(str(csv_gas_file))
        await hass.async_block_till_done()

        assert sensor.last_rows_written == 2

    async def test_reimport_same_file_writes_zero_rows(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        """Re-importing a fully-covered file must write 0 rows."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 3

        # Second import of the exact same file
        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 0

    async def test_running_total_accumulates_correctly(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()

        expected_total = 1.234 + 0.987 + 1.100
        stored_total = sensor._data.get(ELECTRIC_SENSOR_KEY, 0.0)
        assert abs(stored_total - expected_total) < 1e-4

    async def test_last_time_updated_after_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()

        last_time = sensor._data.get(ELECTRIC_TIME_KEY, "")
        assert last_time != ""
        # The three CSV rows are at 05:00, 06:00, 07:00 UTC; newest = 08:00 UTC
        assert last_time == "2026-01-01 08:00:00+00:00"

    async def test_native_value_stays_none_after_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
    ) -> None:
        """native_value must NEVER be set — doing so poisons the recorder chain."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()

        assert sensor.native_value is None

    async def test_parse_error_sets_last_result_with_errors(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        tmp_path: Path,
    ) -> None:
        """A file that fails to parse must populate last_result.errors."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        empty = tmp_path / "empty.csv"
        empty.write_text("")

        await sensor.async_process_file(str(empty))
        await hass.async_block_till_done()

        assert sensor.last_result is not None
        assert not sensor.last_result.success
        assert sensor.last_rows_written == 0

    async def test_last_result_and_rows_written_reset_before_each_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_electric_file: Path,
        tmp_path: Path,
    ) -> None:
        """Stale last_result / last_rows_written from prior import must be cleared."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        # Good import
        await sensor.async_process_file(str(csv_electric_file))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 3

        # Bad import immediately after
        empty = tmp_path / "empty.csv"
        empty.write_text("")
        await sensor.async_process_file(str(empty))
        await hass.async_block_till_done()

        # Must reflect the new (failed) import, not the previous good one
        assert sensor.last_rows_written == 0
        assert sensor.last_result is not None
        assert not sensor.last_result.success

    async def test_zero_and_negative_rows_not_written(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        csv_corrections_file: Path,
    ) -> None:
        """Correction rows with zero/negative usage must be excluded from DB writes."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(csv_corrections_file))
        await hass.async_block_till_done()

        # Only 2 of 4 rows are valid (1.0 and 2.0 kWh)
        assert sensor.last_rows_written == 2
        assert abs(sensor._data.get(ELECTRIC_SENSOR_KEY, 0.0) - 3.0) < 1e-4


# ---------------------------------------------------------------------------
# async_process_file — XML
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestProcessFileXml:
    """Tests for GreenButtonSensor.async_process_file with XML input."""

    async def test_xml_electric_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        xml_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(xml_electric_file))
        await hass.async_block_till_done()

        assert sensor.last_rows_written == 2
        # 0.938 + 1.056 = 1.994 kWh
        assert abs(sensor._data.get(ELECTRIC_SENSOR_KEY, 0.0) - 1.994) < 1e-3

    async def test_xml_gas_import(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        xml_gas_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "gas")

        await sensor.async_process_file(str(xml_gas_file))
        await hass.async_block_till_done()

        assert sensor.last_rows_written == 2
        # 0.702 + 0.450 = 1.152 therms
        assert abs(sensor._data.get(GAS_SENSOR_KEY, 0.0) - 1.152) < 1e-3

    async def test_xml_reimport_writes_zero_rows(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        xml_electric_file: Path,
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        await sensor.async_process_file(str(xml_electric_file))
        await hass.async_block_till_done()

        await sensor.async_process_file(str(xml_electric_file))
        await hass.async_block_till_done()

        assert sensor.last_rows_written == 0


# ---------------------------------------------------------------------------
# Cumulative sum baseline continuity
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestCumulativeSumContinuity:
    """Verify that sequential imports produce a monotonically increasing sum chain."""

    async def test_sequential_imports_sum_is_monotonically_increasing(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        tmp_path: Path,
    ) -> None:
        """Import file A then file B; every row's cumulative sum must be ≥ the previous."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        # File A: rows at hour 0 and 1
        file_a = tmp_path / "a.csv"
        file_a.write_text(
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 00:00:00-05:00,2026-02-01 01:00:00-05:00,1.0,kWh,$0.12,45\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 01:00:00-05:00,2026-02-01 02:00:00-05:00,2.0,kWh,$0.24,45\n"
        )
        await sensor.async_process_file(str(file_a))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 2

        # File B: rows at hour 2 and 3 (non-overlapping)
        file_b = tmp_path / "b.csv"
        file_b.write_text(
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 02:00:00-05:00,2026-02-01 03:00:00-05:00,3.0,kWh,$0.36,45\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 03:00:00-05:00,2026-02-01 04:00:00-05:00,4.0,kWh,$0.48,45\n"
        )
        await sensor.async_process_file(str(file_b))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 2

        # Total must be 1+2+3+4 = 10 kWh
        assert abs(sensor._data.get(ELECTRIC_SENSOR_KEY, 0.0) - 10.0) < 1e-4

    async def test_overlapping_file_does_not_double_count(
        self,
        hass: HomeAssistant,
        mock_config_entry: MockConfigEntry,
        tmp_path: Path,
    ) -> None:
        """A second file that overlaps hours already imported must not double-count."""
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_sensor(hass, "electric")

        base = (
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 00:00:00-05:00,2026-02-01 01:00:00-05:00,1.0,kWh,$0.12,45\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 01:00:00-05:00,2026-02-01 02:00:00-05:00,2.0,kWh,$0.24,45\n"
        )
        file_a = tmp_path / "base.csv"
        file_a.write_text(base)
        await sensor.async_process_file(str(file_a))
        await hass.async_block_till_done()

        # Overlapping file: includes hours 0, 1 (already imported) + hour 2 (new)
        overlap = (
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 00:00:00-05:00,2026-02-01 01:00:00-05:00,1.0,kWh,$0.12,45\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 01:00:00-05:00,2026-02-01 02:00:00-05:00,2.0,kWh,$0.24,45\n"
            "U,A,1,E,electric,2026-02-01,2026-02-01 02:00:00-05:00,2026-02-01 03:00:00-05:00,3.0,kWh,$0.36,45\n"
        )
        file_b = tmp_path / "overlap.csv"
        file_b.write_text(overlap)
        await sensor.async_process_file(str(file_b))
        await hass.async_block_till_done()

        # Only the new hour (3.0 kWh) must be added; total = 1+2+3 = 6
        assert abs(sensor._data.get(ELECTRIC_SENSOR_KEY, 0.0) - 6.0) < 1e-4
