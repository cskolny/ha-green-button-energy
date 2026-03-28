"""Tests for billing_parser.py and the billing WebSocket handler in __init__.py.

billing_parser.py was at 17% coverage — these tests exercise the full
parse pipeline: timestamp parsing, intra-file gap fill, inter-import gap
fill, deduplication, error paths, and the hour-enumeration helper.

The billing WebSocket handler (ws_handle_import_billing) and
_find_cost_sensor are tested via the same _call_handler pattern used in
test_init.py for the usage handler.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.green_button_energy.billing_parser import (
    BillingParseResult,
    _enumerate_hours,
    _parse_billing_timestamp,
    parse_billing_file,
)
from custom_components.green_button_energy.const import DOMAIN

# ---------------------------------------------------------------------------
# Sample billing CSV helpers
# ---------------------------------------------------------------------------

_BILLING_HEADER = (
    "Name,Address,Account Number,Service,Type,Date,"
    "Start Time,End Time,Usage,Units,Costs,Weather\n"
)


def _make_billing_csv(*rows: str) -> str:
    """Return a complete billing CSV string with the standard header."""
    return _BILLING_HEADER + "".join(rows)


def _billing_row(
    start: str,
    end: str,
    cost: str,
    service: str = "Electric",
    stype: str = "electric",
) -> str:
    return (
        f"Test User,123 Main St,1234567890,{service},{stype},2026-01-01,"
        f"{start},{end},500,kWh,{cost},45\n"
    )


# ---------------------------------------------------------------------------
# _parse_billing_timestamp
# ---------------------------------------------------------------------------


class TestParseBillingTimestamp:
    def test_naive_datetime_treated_as_eastern(self) -> None:
        # 2026-01-01 00:00 Eastern = 2026-01-01 05:00 UTC (EST = UTC-5)
        dt = _parse_billing_timestamp("2026-01-01 00:00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)

    def test_naive_date_only_treated_as_eastern(self) -> None:
        dt = _parse_billing_timestamp("2026-07-01")
        assert dt is not None
        # 2026-07-01 00:00 EDT = 2026-07-01 04:00 UTC (EDT = UTC-4)
        assert dt == datetime(2026, 7, 1, 4, 0, 0, tzinfo=UTC)

    def test_naive_hhmm_treated_as_eastern(self) -> None:
        dt = _parse_billing_timestamp("2026-01-15 00:00")
        assert dt is not None
        assert dt == datetime(2026, 1, 15, 5, 0, 0, tzinfo=UTC)

    def test_aware_string_honored_directly(self) -> None:
        dt = _parse_billing_timestamp("2026-01-01 05:00:00+00:00")
        assert dt is not None
        assert dt == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)

    def test_garbage_returns_none(self) -> None:
        assert _parse_billing_timestamp("not-a-date") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_billing_timestamp("") is None

    def test_whitespace_stripped(self) -> None:
        dt = _parse_billing_timestamp("  2026-01-01  ")
        assert dt is not None

    def test_result_is_always_aware(self) -> None:
        dt = _parse_billing_timestamp("2026-03-01 00:00:00")
        assert dt is not None
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# BillingParseResult dataclass
# ---------------------------------------------------------------------------


class TestBillingParseResult:
    def test_success_true_when_no_errors(self) -> None:
        r = BillingParseResult()
        assert r.success is True

    def test_success_false_when_errors(self) -> None:
        r = BillingParseResult(errors=["oops"])
        assert r.success is False

    def test_has_new_data_false_at_zero(self) -> None:
        assert BillingParseResult(new_cost=0.0).has_new_data is False

    def test_has_new_data_true_when_positive(self) -> None:
        assert BillingParseResult(new_cost=12.34).has_new_data is True

    def test_defaults_are_empty(self) -> None:
        r = BillingParseResult()
        assert r.hourly_costs == []
        assert r.errors == []
        assert r.newest_time == ""
        assert r.last_effective_end == ""


# ---------------------------------------------------------------------------
# _enumerate_hours
# ---------------------------------------------------------------------------


class TestEnumerateHours:
    def test_returns_correct_count_for_one_day(self) -> None:
        start = datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)  # Eastern midnight
        end = datetime(2026, 1, 2, 5, 0, 0, tzinfo=UTC)    # next Eastern midnight
        hours = _enumerate_hours(start, end)
        assert len(hours) == 24

    def test_all_results_are_hour_aligned(self) -> None:
        start = datetime(2026, 1, 1, 5, 30, 0, tzinfo=UTC)  # non-aligned start
        end = datetime(2026, 1, 2, 5, 0, 0, tzinfo=UTC)
        hours = _enumerate_hours(start, end)
        for h in hours:
            assert h.minute == 0
            assert h.second == 0

    def test_empty_when_end_before_start(self) -> None:
        start = datetime(2026, 1, 2, 5, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)
        assert _enumerate_hours(start, end) == []

    def test_all_results_are_utc_aware(self) -> None:
        start = datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 5, 0, 0, tzinfo=UTC)
        for h in _enumerate_hours(start, end):
            assert h.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# parse_billing_file — error paths
# ---------------------------------------------------------------------------


class TestParseBillingFileErrors:
    def test_non_csv_extension_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.xml"
        f.write_text("<feed/>")
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success
        assert any(".xml" in e for e in result.errors)

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        result = parse_billing_file(str(tmp_path / "nope.csv"), "electric", "")
        assert not result.success

    def test_empty_file_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.csv"
        f.write_text("")
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success

    def test_missing_start_time_column_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.csv"
        f.write_text("Name,End Time,Costs,Type\nFoo,2026-01-31,85.15,electric\n")
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success
        assert any("Start Time" in e for e in result.errors)

    def test_missing_end_time_column_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.csv"
        f.write_text("Name,Start Time,Costs,Type\nFoo,2026-01-01,85.15,electric\n")
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success
        assert any("End Time" in e for e in result.errors)

    def test_missing_costs_column_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.csv"
        f.write_text("Name,Start Time,End Time,Type\nFoo,2026-01-01,2026-01-31,electric\n")
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success
        assert any("Costs" in e for e in result.errors)

    def test_no_rows_for_service_type_returns_error(self, tmp_path: Path) -> None:
        """A file with only gas rows returns an error when parsed as electric."""
        f = tmp_path / "gas_only.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15", "Gas", "gas")))
        result = parse_billing_file(str(f), "electric", "")
        assert not result.success
        assert any("no billing rows" in e for e in result.errors)

    def test_non_numeric_cost_row_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "bad_cost.csv"
        f.write_text(
            _make_billing_csv(
                _billing_row("2026-01-01", "2026-01-31", "N/A"),
                _billing_row("2026-02-01", "2026-02-28", "72.50"),
            )
        )
        result = parse_billing_file(str(f), "electric", "")
        assert result.cycles_imported == 1
        assert result.cycles_skipped == 1

    def test_zero_cost_row_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "zero_cost.csv"
        f.write_text(
            _make_billing_csv(
                _billing_row("2026-01-01", "2026-01-31", "0.00"),
                _billing_row("2026-02-01", "2026-02-28", "72.50"),
            )
        )
        result = parse_billing_file(str(f), "electric", "")
        assert result.cycles_imported == 1
        assert result.cycles_skipped == 1


# ---------------------------------------------------------------------------
# parse_billing_file — happy-path single cycle
# ---------------------------------------------------------------------------


class TestParseBillingFileSingleCycle:
    def test_single_cycle_imports_correctly(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        # Jan 1 – Jan 31 Eastern midnight (31 days = 744 hours)
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        result = parse_billing_file(str(f), "electric", "")
        assert result.success
        assert result.cycles_imported == 1
        assert abs(result.new_cost - 85.15) < 0.01

    def test_hourly_costs_populated(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        result = parse_billing_file(str(f), "electric", "")
        assert len(result.hourly_costs) > 0

    def test_hourly_costs_are_aware_utc(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        result = parse_billing_file(str(f), "electric", "")
        for dt, cost in result.hourly_costs:
            assert dt.tzinfo is not None
            assert cost > 0

    def test_total_cost_equals_sum_of_hourly(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "100.00")))
        result = parse_billing_file(str(f), "electric", "")
        hourly_sum = sum(c for _, c in result.hourly_costs)
        assert abs(hourly_sum - 100.00) < 0.001

    def test_newest_time_set(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        result = parse_billing_file(str(f), "electric", "")
        assert result.newest_time != ""

    def test_last_effective_end_set(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        result = parse_billing_file(str(f), "electric", "")
        assert result.last_effective_end != ""

    def test_dollar_sign_prefix_in_cost_parsed(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "$85.15")))
        result = parse_billing_file(str(f), "electric", "")
        assert result.cycles_imported == 1
        assert abs(result.new_cost - 85.15) < 0.01

    def test_gas_cycle_filters_correctly(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(
            _make_billing_csv(
                _billing_row("2026-01-01", "2026-01-31", "85.15"),           # electric
                _billing_row("2026-01-01", "2026-01-31", "42.00", "Gas", "gas"),  # gas
            )
        )
        result = parse_billing_file(str(f), "gas", "")
        assert result.cycles_imported == 1
        assert abs(result.new_cost - 42.00) < 0.01


# ---------------------------------------------------------------------------
# parse_billing_file — deduplication
# ---------------------------------------------------------------------------


class TestParseBillingDeduplication:
    def test_reimport_same_cycle_writes_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        first = parse_billing_file(str(f), "electric", "")
        assert first.cycles_imported == 1

        second = parse_billing_file(str(f), "electric", first.newest_time)
        assert second.cycles_imported == 0
        assert second.cycles_skipped >= 1

    def test_partial_reimport_skips_old_cycle(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(
            _make_billing_csv(
                _billing_row("2026-01-01", "2026-01-31", "85.15"),
                _billing_row("2026-02-01", "2026-02-28", "72.50"),
            )
        )
        first = parse_billing_file(str(f), "electric", "")
        assert first.cycles_imported == 2

        # Re-import with last_time set to the Jan cycle start — only Feb should come through
        # The Jan cycle start (Eastern midnight Jan 1) in UTC
        jan_start_utc = "2026-01-01 05:00:00+00:00"
        second = parse_billing_file(str(f), "electric", jan_start_utc)
        assert second.cycles_imported == 1
        assert abs(second.new_cost - 72.50) < 0.01


# ---------------------------------------------------------------------------
# parse_billing_file — intra-file gap fill
# ---------------------------------------------------------------------------


class TestParseBillingIntraFileGapFill:
    def test_two_cycles_gap_filled(self, tmp_path: Path) -> None:
        """Cycle 1 ends Jan 31; Cycle 2 starts Feb 2 (one-day gap).
        Cycle 1's effective end should extend to Cycle 2's start (Feb 2)."""
        f = tmp_path / "billing.csv"
        # Cycle 1: Jan 1 – Jan 31; Cycle 2: Feb 2 – Feb 28 (gap: Feb 1)
        f.write_text(
            _make_billing_csv(
                _billing_row("2026-01-01", "2026-01-31", "85.15"),
                _billing_row("2026-02-02", "2026-02-28", "72.50"),
            )
        )
        result = parse_billing_file(str(f), "electric", "")
        assert result.success
        assert result.cycles_imported == 2

        # Cycle 1's hourly_costs should extend through Feb 1 (up to Feb 2 start)
        # so total hours > 31*24 = 744 for cycle 1 alone
        # We verify no gap by checking continuity of hourly_costs timestamps
        sorted_hours = sorted(h for h, _ in result.hourly_costs)
        for i in range(1, len(sorted_hours)):
            diff_seconds = (sorted_hours[i] - sorted_hours[i - 1]).total_seconds()
            assert diff_seconds == 3600, f"Gap found between {sorted_hours[i-1]} and {sorted_hours[i]}"


# ---------------------------------------------------------------------------
# parse_billing_file — inter-import gap fill
# ---------------------------------------------------------------------------


class TestParseBillingInterImportGapFill:
    def test_inter_import_gap_filled(self, tmp_path: Path) -> None:
        """First import ends at Jan 31. Second import starts Feb 2 (gap: Feb 1).
        With last_effective_end pointing to Feb 1 start, the gap should be filled."""
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-02-02", "2026-02-28", "72.50")))

        # Simulate previous import ended at Eastern midnight Feb 1 = 2026-02-01 05:00 UTC
        last_effective_end = "2026-02-01 05:00:00+00:00"
        result = parse_billing_file(str(f), "electric", "", last_effective_end)
        assert result.success
        assert result.cycles_imported == 1

        # The first hourly cost should be at the last_effective_end, not at Feb 2 start
        sorted_hours = sorted(h for h, _ in result.hourly_costs)
        first_hour = sorted_hours[0]
        expected_start = datetime(2026, 2, 1, 5, 0, 0, tzinfo=UTC)
        assert first_hour == expected_start, (
            f"Expected gap fill to start at {expected_start}, got {first_hour}"
        )

    def test_no_gap_fill_when_no_last_effective_end(self, tmp_path: Path) -> None:
        """Without last_effective_end, import starts at cycle's CSV start."""
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-02-02", "2026-02-28", "72.50")))
        result = parse_billing_file(str(f), "electric", "", "")
        assert result.success

        sorted_hours = sorted(h for h, _ in result.hourly_costs)
        first_hour = sorted_hours[0]
        # Feb 2 Eastern midnight = Feb 2 05:00 UTC
        expected_start = datetime(2026, 2, 2, 5, 0, 0, tzinfo=UTC)
        assert first_hour == expected_start


# ---------------------------------------------------------------------------
# parse_billing_file — all cycles already imported (no valid_cycles)
# ---------------------------------------------------------------------------


class TestParseBillingAllSkipped:
    def test_all_skipped_returns_success_with_zero_cycles(self, tmp_path: Path) -> None:
        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))
        # last_time is after the cycle start, so it gets skipped
        result = parse_billing_file(str(f), "electric", "2026-06-01 05:00:00+00:00")
        assert result.success
        assert result.cycles_imported == 0
        assert result.hourly_costs == []


# ---------------------------------------------------------------------------
# Billing WebSocket handler and _find_cost_sensor — __init__.py
# ---------------------------------------------------------------------------


async def _setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    with (
        patch("homeassistant.setup.async_process_deps_reqs"),
        patch("custom_components.green_button_energy._async_register_panel", return_value=None),
        patch("custom_components.green_button_energy.async_register_command"),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()


def _mock_connection() -> MagicMock:
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    return conn


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestFindCostSensor:
    async def test_find_electric_cost_sensor(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        from custom_components.green_button_energy import _find_cost_sensor

        await _setup_integration(hass, mock_config_entry)
        sensor = _find_cost_sensor(hass, "electric")
        assert sensor is not None
        assert sensor.unique_id == "green_button_energy_electric_cost"

    async def test_find_gas_cost_sensor(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        from custom_components.green_button_energy import _find_cost_sensor

        await _setup_integration(hass, mock_config_entry)
        sensor = _find_cost_sensor(hass, "gas")
        assert sensor is not None
        assert sensor.unique_id == "green_button_energy_gas_cost"

    async def test_find_cost_sensor_unknown_returns_none(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        from custom_components.green_button_energy import _find_cost_sensor

        await _setup_integration(hass, mock_config_entry)
        assert _find_cost_sensor(hass, "steam") is None


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestWsHandleImportBilling:
    async def _call_handler(self, hass: HomeAssistant, msg: dict) -> MagicMock:
        from custom_components.green_button_energy import ws_handle_import_billing

        conn = _mock_connection()
        await ws_handle_import_billing.__wrapped__(hass, conn, msg)
        await hass.async_block_till_done()
        return conn

    def _billing_csv(self) -> str:
        return _make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15"))

    async def test_successful_billing_import(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        conn = await self._call_handler(
            hass,
            {
                "id": 1,
                "type": "green_button_energy/import_billing",
                "filename": "billing.csv",
                "content": self._billing_csv(),
                "service_type": "electric",
            },
        )
        conn.send_result.assert_called_once()
        payload = conn.send_result.call_args[0][1]
        assert payload["success"] is True
        assert payload["rows_written"] > 0
        conn.send_error.assert_not_called()

    async def test_billing_file_too_large(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        conn = await self._call_handler(
            hass,
            {
                "id": 2,
                "type": "green_button_energy/import_billing",
                "filename": "big.csv",
                "content": "x" * (11 * 1024 * 1024),
                "service_type": "electric",
            },
        )
        conn.send_error.assert_called_once()
        assert conn.send_error.call_args[0][1] == "file_too_large"

    async def test_billing_non_csv_extension(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        conn = await self._call_handler(
            hass,
            {
                "id": 3,
                "type": "green_button_energy/import_billing",
                "filename": "billing.xml",
                "content": "<feed/>",
                "service_type": "electric",
            },
        )
        conn.send_error.assert_called_once()
        assert conn.send_error.call_args[0][1] == "invalid_format"

    async def test_billing_sensor_not_found(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        with patch(
            "custom_components.green_button_energy._find_cost_sensor",
            return_value=None,
        ):
            conn = await self._call_handler(
                hass,
                {
                    "id": 4,
                    "type": "green_button_energy/import_billing",
                    "filename": "billing.csv",
                    "content": self._billing_csv(),
                    "service_type": "electric",
                },
            )
        conn.send_error.assert_called_once()
        assert conn.send_error.call_args[0][1] == "sensor_not_found"

    async def test_reimport_billing_returns_zero_rows(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        msg = {
            "id": 5,
            "type": "green_button_energy/import_billing",
            "filename": "billing.csv",
            "content": self._billing_csv(),
            "service_type": "electric",
        }
        await self._call_handler(hass, {**msg, "id": 5})
        conn = await self._call_handler(hass, {**msg, "id": 6})
        payload = conn.send_result.call_args[0][1]
        assert payload["success"] is True
        assert payload["rows_written"] == 0

    async def test_billing_parse_error_returns_error_payload(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        conn = await self._call_handler(
            hass,
            {
                "id": 7,
                "type": "green_button_energy/import_billing",
                "filename": "bad.csv",
                "content": "",   # empty — will produce a parse error
                "service_type": "electric",
            },
        )
        conn.send_result.assert_called_once()
        payload = conn.send_result.call_args[0][1]
        assert payload["success"] is False

    async def test_billing_temp_file_deleted_after_import(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        written_paths: list[str] = []
        original_unlink = __import__("os").unlink

        def _tracking_unlink(path: str) -> None:
            written_paths.append(path)
            original_unlink(path)

        with patch(
            "custom_components.green_button_energy.os.unlink",
            side_effect=_tracking_unlink,
        ):
            await self._call_handler(
                hass,
                {
                    "id": 8,
                    "type": "green_button_energy/import_billing",
                    "filename": "billing.csv",
                    "content": self._billing_csv(),
                    "service_type": "electric",
                },
            )

        assert len(written_paths) == 1
        assert not Path(written_paths[0]).exists()

    async def test_gas_billing_import(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        gas_csv = _make_billing_csv(
            _billing_row("2026-01-01", "2026-01-31", "42.00", "Gas", "gas")
        )
        conn = await self._call_handler(
            hass,
            {
                "id": 9,
                "type": "green_button_energy/import_billing",
                "filename": "gas_billing.csv",
                "content": gas_csv,
                "service_type": "gas",
            },
        )
        conn.send_result.assert_called_once()
        payload = conn.send_result.call_args[0][1]
        assert payload["success"] is True
        assert payload["rows_written"] > 0


# ---------------------------------------------------------------------------
# GreenButtonCostSensor.async_process_billing_file — sensor.py coverage
# ---------------------------------------------------------------------------


def _get_cost_sensor(hass: HomeAssistant, service_type: str):
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if isinstance(entry_data, dict):
            sensor = entry_data.get(f"{service_type}_cost")
            if sensor is not None:
                return sensor
    raise KeyError(f"Cost sensor for service_type='{service_type}' not found")


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
class TestCostSensorProcessBillingFile:
    async def test_electric_billing_import_writes_stats(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()

        assert sensor.last_result is not None
        assert sensor.last_result.success
        assert sensor.last_rows_written > 0

    async def test_gas_billing_import_writes_stats(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "gas")

        f = tmp_path / "gas_billing.csv"
        f.write_text(
            _make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "42.00", "Gas", "gas"))
        )

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()

        assert sensor.last_rows_written > 0

    async def test_reimport_billing_writes_zero(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()
        first_written = sensor.last_rows_written
        assert first_written > 0

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()
        assert sensor.last_rows_written == 0

    async def test_billing_parse_error_sets_last_result_errors(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        empty = tmp_path / "empty.csv"
        empty.write_text("")

        await sensor.async_process_billing_file(str(empty))
        await hass.async_block_till_done()

        assert sensor.last_result is not None
        assert not sensor.last_result.success
        assert sensor.last_rows_written == 0

    async def test_cost_sensor_native_value_stays_none(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()

        assert sensor.native_value is None

    async def test_running_total_accumulates(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        from custom_components.green_button_energy.const import ELECTRIC_COST_KEY

        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()

        assert abs(sensor._data.get(ELECTRIC_COST_KEY, 0.0) - 85.15) < 0.01

    async def test_last_effective_end_persisted(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry, tmp_path: Path
    ) -> None:
        from custom_components.green_button_energy.const import ELECTRIC_COST_END_KEY

        await _setup_integration(hass, mock_config_entry)
        sensor = _get_cost_sensor(hass, "electric")

        f = tmp_path / "billing.csv"
        f.write_text(_make_billing_csv(_billing_row("2026-01-01", "2026-01-31", "85.15")))

        await sensor.async_process_billing_file(str(f))
        await hass.async_block_till_done()

        assert sensor._data.get(ELECTRIC_COST_END_KEY, "") != ""
