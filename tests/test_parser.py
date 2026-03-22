"""Unit tests for parser.py.

These tests are pure Python — no HA instance required.  They cover every
meaningful code path in the CSV and XML parsers, the timestamp helpers, and
the ParseResult dataclass.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from custom_components.green_button_energy.parser import (
    STORAGE_TIME_FMT,
    ParseResult,
    _parse_csv_timestamp,
    _parse_stored_time,
    parse_file,
)


# ---------------------------------------------------------------------------
# _parse_stored_time
# ---------------------------------------------------------------------------


class TestParseStoredTime:
    """Tests for the internal _parse_stored_time helper."""

    def test_empty_string_returns_none(self) -> None:
        assert _parse_stored_time("") is None

    def test_canonical_utc_format(self) -> None:
        dt = _parse_stored_time("2026-03-01 05:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 3, 1, 5, 0, 0, tzinfo=timezone.utc)

    def test_aware_non_utc_is_normalised_to_utc(self) -> None:
        # "-05:00" offset → UTC should be +5 hours
        dt = _parse_stored_time("2026-03-01 00:00:00-05:00")
        assert dt is not None
        assert dt == datetime(2026, 3, 1, 5, 0, 0, tzinfo=timezone.utc)

    def test_legacy_naive_datetime_string(self) -> None:
        """Legacy naive strings (written by v1.0) must be treated as UTC."""
        dt = _parse_stored_time("2026-03-01 05:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 5

    def test_legacy_naive_hhmm_string(self) -> None:
        dt = _parse_stored_time("2026-03-01 05:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_unparseable_returns_none(self) -> None:
        assert _parse_stored_time("not-a-date") is None

    def test_result_is_always_timezone_aware(self) -> None:
        """Regardless of input format, the returned datetime must have tzinfo."""
        for value in (
            "2026-03-01 05:00:00+00:00",
            "2026-03-01 05:00:00",
            "2026-03-01 05:00",
        ):
            dt = _parse_stored_time(value)
            assert dt is not None, f"Expected datetime for '{value}'"
            assert dt.tzinfo is not None, f"Expected aware datetime for '{value}'"


# ---------------------------------------------------------------------------
# _parse_csv_timestamp
# ---------------------------------------------------------------------------


class TestParseCsvTimestamp:
    """Tests for the internal _parse_csv_timestamp helper."""

    def test_aware_iso_with_negative_offset(self) -> None:
        dt = _parse_csv_timestamp("2026-03-01 00:00:00-05:00")
        assert dt is not None
        assert dt == datetime(2026, 3, 1, 5, 0, 0, tzinfo=timezone.utc)

    def test_aware_iso_with_positive_offset(self) -> None:
        dt = _parse_csv_timestamp("2026-06-15 12:00:00+01:00")
        assert dt is not None
        assert dt.hour == 11  # 12:00 +01:00 = 11:00 UTC

    def test_naive_iso_treated_as_utc(self) -> None:
        dt = _parse_csv_timestamp("2026-03-01 05:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_bare_date_string(self) -> None:
        dt = _parse_csv_timestamp("2026-03-01")
        assert dt is not None
        assert dt == datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_whitespace_is_stripped(self) -> None:
        dt = _parse_csv_timestamp("  2026-03-01 00:00:00-05:00  ")
        assert dt is not None

    def test_garbage_returns_none(self) -> None:
        assert _parse_csv_timestamp("not-a-date") is None

    def test_result_is_always_timezone_aware(self) -> None:
        dt = _parse_csv_timestamp("2026-03-01 00:00:00-05:00")
        assert dt is not None
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# ParseResult dataclass
# ---------------------------------------------------------------------------


class TestParseResult:
    """Tests for the ParseResult dataclass properties."""

    def test_success_true_when_no_errors(self) -> None:
        r = ParseResult()
        assert r.success is True

    def test_success_false_when_errors_present(self) -> None:
        r = ParseResult(errors=["something went wrong"])
        assert r.success is False

    def test_has_new_data_false_when_zero_usage(self) -> None:
        r = ParseResult(new_usage=0.0)
        assert r.has_new_data is False

    def test_has_new_data_true_when_positive_usage(self) -> None:
        r = ParseResult(new_usage=1.234)
        assert r.has_new_data is True

    def test_default_hourly_readings_is_empty_list(self) -> None:
        r = ParseResult()
        assert r.hourly_readings == []

    def test_default_errors_is_empty_list(self) -> None:
        r = ParseResult()
        assert r.errors == []


# ---------------------------------------------------------------------------
# parse_file — unsupported extension
# ---------------------------------------------------------------------------


class TestParseFileUnsupportedExtension:
    def test_unsupported_extension_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "data.txt"
        f.write_text("irrelevant")
        result = parse_file(str(f), "electric", "")
        assert not result.success
        assert any(".txt" in e for e in result.errors)

    def test_unsupported_extension_preserves_last_time(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text("{}")
        last = "2026-01-01 00:00:00+00:00"
        result = parse_file(str(f), "electric", last)
        assert result.newest_time == last


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


class TestCsvParser:
    """Tests for _parse_csv via the public parse_file entry point."""

    def test_electric_rows_imported(self, csv_electric_file: Path) -> None:
        result = parse_file(str(csv_electric_file), "electric", "")
        assert result.success
        assert result.rows_imported == 3
        assert result.rows_skipped == 0
        assert abs(result.new_usage - (1.234 + 0.987 + 1.100)) < 1e-6

    def test_gas_rows_imported(self, csv_gas_file: Path) -> None:
        result = parse_file(str(csv_gas_file), "gas", "")
        assert result.success
        assert result.rows_imported == 2
        assert abs(result.new_usage - (0.045 + 0.032)) < 1e-6

    def test_mixed_file_filters_by_service_type_electric(
        self, csv_mixed_file: Path
    ) -> None:
        result = parse_file(str(csv_mixed_file), "electric", "")
        assert result.rows_imported == 1
        assert abs(result.new_usage - 1.234) < 1e-6

    def test_mixed_file_filters_by_service_type_gas(
        self, csv_mixed_file: Path
    ) -> None:
        result = parse_file(str(csv_mixed_file), "gas", "")
        assert result.rows_imported == 1
        assert abs(result.new_usage - 0.045) < 1e-6

    def test_zero_and_negative_usage_rows_skipped(
        self, csv_corrections_file: Path
    ) -> None:
        """Zero and negative usage rows must be silently skipped."""
        result = parse_file(str(csv_corrections_file), "electric", "")
        # 4 raw rows: 1.000 (ok), 0.000 (skip), -0.500 (skip), 2.000 (ok)
        assert result.rows_imported == 2
        assert result.rows_skipped == 2
        assert abs(result.new_usage - 3.000) < 1e-6

    def test_duplicate_rows_skipped_when_last_time_set(
        self, csv_electric_file: Path
    ) -> None:
        """Rows at or before last_time must be skipped."""
        # First full import
        first = parse_file(str(csv_electric_file), "electric", "")
        assert first.rows_imported == 3

        # Re-import using newest_time as cursor — should get 0 new rows
        second = parse_file(str(csv_electric_file), "electric", first.newest_time)
        assert second.rows_imported == 0
        assert second.rows_skipped == 3

    def test_partial_reimport_skips_already_seen(
        self, tmp_path: Path
    ) -> None:
        """Only rows strictly after last_time should be imported."""
        csv_content = textwrap.dedent(
            """\
            Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather
            U,A,1,E,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,1.0,kWh,$0.12,45
            U,A,1,E,electric,2026-01-01,2026-01-01 01:00:00-05:00,2026-01-01 02:00:00-05:00,2.0,kWh,$0.24,45
            U,A,1,E,electric,2026-01-01,2026-01-01 02:00:00-05:00,2026-01-01 03:00:00-05:00,3.0,kWh,$0.36,45
            """
        )
        f = tmp_path / "partial.csv"
        f.write_text(csv_content)

        # Import only row 1 by setting last_time to its UTC timestamp
        cutoff = "2026-01-01 05:00:00+00:00"  # = 2026-01-01 00:00 EST
        result = parse_file(str(f), "electric", cutoff)
        # Row at 05:00 UTC is <= cutoff, so only rows at 06:00 and 07:00 UTC are new
        assert result.rows_imported == 2
        assert abs(result.new_usage - 5.0) < 1e-6

    def test_newest_time_is_latest_row(self, csv_electric_file: Path) -> None:
        result = parse_file(str(csv_electric_file), "electric", "")
        # The three rows are at 05:00, 06:00, 07:00 UTC
        assert result.newest_time == "2026-01-01 07:00:00+00:00"

    def test_newest_time_in_storage_format(self, csv_electric_file: Path) -> None:
        result = parse_file(str(csv_electric_file), "electric", "")
        # Must be parseable back to a datetime without error
        dt = datetime.strptime(result.newest_time, STORAGE_TIME_FMT)
        assert dt is not None

    def test_hourly_readings_populated(self, csv_electric_file: Path) -> None:
        result = parse_file(str(csv_electric_file), "electric", "")
        assert len(result.hourly_readings) == 3
        for dt, usage in result.hourly_readings:
            assert dt.tzinfo is not None  # always aware
            assert usage > 0

    def test_missing_start_time_column_returns_error(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("Name,Usage,Type\nFoo,1.0,electric\n")
        result = parse_file(str(bad), "electric", "")
        assert not result.success
        assert any("Start Time" in e for e in result.errors)

    def test_missing_usage_column_returns_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("Name,Start Time,Type\nFoo,2026-01-01 00:00:00-05:00,electric\n")
        result = parse_file(str(bad), "electric", "")
        assert not result.success
        assert any("Usage" in e for e in result.errors)

    def test_empty_file_returns_error(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_text("")
        result = parse_file(str(empty), "electric", "")
        assert not result.success

    def test_non_numeric_usage_row_skipped(self, tmp_path: Path) -> None:
        csv = tmp_path / "bad_usage.csv"
        csv.write_text(
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,N/A,kWh,$0.00,45\n"
            "U,A,1,E,electric,2026-01-01,2026-01-01 01:00:00-05:00,2026-01-01 02:00:00-05:00,1.5,kWh,$0.18,45\n"
        )
        result = parse_file(str(csv), "electric", "")
        assert result.rows_imported == 1
        assert result.rows_skipped == 1

    def test_comma_in_usage_value_parsed_correctly(self, tmp_path: Path) -> None:
        """Usage values like '1,234.5' (thousands separator) must parse."""
        csv = tmp_path / "comma_usage.csv"
        csv.write_text(
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,\"1,234\",kWh,$0.15,45\n"
        )
        result = parse_file(str(csv), "electric", "")
        assert result.rows_imported == 1
        assert abs(result.new_usage - 1234.0) < 1e-6

    def test_headers_are_case_insensitive(self, tmp_path: Path) -> None:
        """Column matching must work regardless of capitalisation."""
        csv = tmp_path / "caps.csv"
        csv.write_text(
            "NAME,ADDRESS,ACCOUNT NUMBER,SERVICE,TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COSTS,WEATHER\n"
            "U,A,1,E,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,2.0,kWh,$0.24,45\n"
        )
        result = parse_file(str(csv), "electric", "")
        assert result.rows_imported == 1

    def test_utf8_bom_file_reads_correctly(self, tmp_path: Path) -> None:
        """Files exported with a UTF-8 BOM (common from Excel) must parse."""
        csv = tmp_path / "bom.csv"
        content = (
            "Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather\n"
            "U,A,1,E,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,1.0,kWh,$0.12,45\n"
        )
        csv.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        result = parse_file(str(csv), "electric", "")
        assert result.rows_imported == 1

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.csv"
        result = parse_file(str(missing), "electric", "")
        assert not result.success
        assert result.errors


# ---------------------------------------------------------------------------
# XML parser
# ---------------------------------------------------------------------------


class TestXmlParser:
    """Tests for _parse_xml via the public parse_file entry point."""

    def test_electric_xml_imported_correctly(self, xml_electric_file: Path) -> None:
        result = parse_file(str(xml_electric_file), "electric", "")
        assert result.success
        assert result.rows_imported == 2
        # 938000 × 10⁻³ ÷ 1000 = 0.938 kWh; 1056000 → 1.056 kWh
        assert abs(result.new_usage - (0.938 + 1.056)) < 1e-4

    def test_gas_xml_imported_correctly(self, xml_gas_file: Path) -> None:
        result = parse_file(str(xml_gas_file), "gas", "")
        assert result.success
        assert result.rows_imported == 2
        # 702 × 10⁻³ = 0.702 therms; 450 × 10⁻³ = 0.450 therms
        assert abs(result.new_usage - (0.702 + 0.450)) < 1e-4

    def test_electric_uom_conversion_wh_to_kwh(self, xml_electric_file: Path) -> None:
        """uom=72 (Wh) with powerOfTenMultiplier=-3 must produce kWh values < 10."""
        result = parse_file(str(xml_electric_file), "electric", "")
        for _, usage in result.hourly_readings:
            # Typical hourly values are < 10 kWh; if conversion is wrong they'd be ~1000
            assert usage < 10.0, f"Suspiciously large value {usage} — conversion may be wrong"

    def test_gas_uom_no_extra_divide(self, xml_gas_file: Path) -> None:
        """uom=169 (therms) must NOT divide by 1000 again."""
        result = parse_file(str(xml_gas_file), "gas", "")
        for _, usage in result.hourly_readings:
            assert usage < 5.0, f"Suspiciously large gas value {usage} — extra ÷1000 applied?"

    def test_duplicate_rows_skipped_when_last_time_set(
        self, xml_electric_file: Path
    ) -> None:
        first = parse_file(str(xml_electric_file), "electric", "")
        second = parse_file(str(xml_electric_file), "electric", first.newest_time)
        assert second.rows_imported == 0
        assert second.rows_skipped == 2

    def test_newest_time_in_storage_format(self, xml_electric_file: Path) -> None:
        result = parse_file(str(xml_electric_file), "electric", "")
        dt = datetime.strptime(result.newest_time, STORAGE_TIME_FMT)
        assert dt is not None

    def test_hourly_readings_are_aware_utc(self, xml_electric_file: Path) -> None:
        result = parse_file(str(xml_electric_file), "electric", "")
        for dt, _ in result.hourly_readings:
            assert dt.tzinfo is not None
            assert dt.tzinfo == timezone.utc

    def test_service_mismatch_logs_warning_but_continues(
        self, xml_gas_file: Path
    ) -> None:
        """Passing service_type='electric' for a gas XML should still parse."""
        result = parse_file(str(xml_gas_file), "electric", "")
        # It should not error out — the warning is logged but parsing continues.
        # The uom (169=therms) will be applied regardless.
        assert result.rows_imported == 2

    def test_empty_xml_returns_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "empty.xml"
        bad.write_text("")
        result = parse_file(str(bad), "electric", "")
        assert not result.success

    def test_no_interval_readings_returns_error(self, tmp_path: Path) -> None:
        xml = tmp_path / "no_readings.xml"
        xml.write_text(
            '<?xml version="1.0"?>'
            '<feed xmlns="http://naesb.org/espi"><entry></entry></feed>'
        )
        result = parse_file(str(xml), "electric", "")
        assert not result.success
        assert any("IntervalReading" in e for e in result.errors)

    def test_missing_uom_inferred_from_service_type(self, tmp_path: Path) -> None:
        """When uom is absent, the service_type hint must be used."""
        xml = tmp_path / "no_uom.xml"
        xml.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <feed xmlns="http://naesb.org/espi">
                  <entry><content>
                    <ReadingType>
                      <powerOfTenMultiplier>-3</powerOfTenMultiplier>
                    </ReadingType>
                  </content></entry>
                  <entry><content>
                    <IntervalBlock>
                      <IntervalReading>
                        <timePeriod><duration>3600</duration><start>1751328000</start></timePeriod>
                        <value>938000</value>
                      </IntervalReading>
                    </IntervalBlock>
                  </content></entry>
                </feed>
                """
            )
        )
        # Without uom, service_type='electric' should infer uom=72 (Wh → kWh)
        result = parse_file(str(xml), "electric", "")
        assert result.rows_imported == 1
        assert abs(result.hourly_readings[0][1] - 0.938) < 1e-4

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.xml"
        result = parse_file(str(missing), "electric", "")
        assert not result.success
