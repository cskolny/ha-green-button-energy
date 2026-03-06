"""
Parser for RG&E Green Button exports — CSV and ESPI XML formats.

CSV format (RG&E Opower export):
    Columns: Name, Address, Account Number, Service, Type, Date,
             Start Time, End Time, Usage, Units, Costs, Weather
    - Timestamp: "Start Time" column, timezone-aware ISO format
                 e.g. "2026-03-01 00:00:00-05:00"
    - Usage: "Usage" column, float in kWh (electric) or therms (gas)
    - Type: "Type" column ("electric" or "gas") — used to filter rows
              so a single file containing both types is handled correctly

XML format (ESPI / Green Button standard):
    - IntervalReading values are in Wh * 10^powerOfTenMultiplier
      (RG&E uses powerOfTenMultiplier=-3, uom=72/Wh → divide by 1000 to get kWh)
    - Timestamps are Unix epoch integers (UTC)

Both parsers:
    - Skip rows/readings already imported (newer than last_seen_time)
    - Run in executor thread pool (no event loop blocking)
    - Return a ParseResult dataclass with full diagnostics
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

_LOGGER = logging.getLogger(__name__)

# ESPI XML namespace
_ESPI_NS = "http://naesb.org/espi"

# Canonical storage format for last_time — always UTC, no tzinfo ambiguity
_STORAGE_FMT = "%Y-%m-%d %H:%M:%S+00:00"


@dataclass
class ParseResult:
    """Result returned from a parse operation."""

    new_usage: float = 0.0
    newest_time: str = ""          # UTC timestamp string, stored in _STORAGE_FMT
    rows_imported: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    # Individual hourly readings as (aware UTC datetime, usage float) tuples.
    # Used by sensor.py to write historical statistics into the recorder.
    hourly_readings: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def has_new_data(self) -> bool:
        """
        True if any new rows were imported, regardless of their usage value.

        Checking rows_imported (not new_usage > 0) ensures that legitimate
        zero-usage intervals — e.g. a day with no consumption — are not
        silently treated as "no new data".
        """
        return self.rows_imported > 0


def _parse_stored_time(value: str) -> Optional[datetime]:
    """
    Parse a stored last_time string back to an aware UTC datetime.

    Returns a timezone-aware datetime, or None if the value is empty or
    cannot be parsed. Callers must handle the None case.
    """
    if not value:
        return None
    # fromisoformat handles "2026-03-01 05:00:00+00:00" correctly
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Fallback for legacy naive strings — treat as UTC
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_csv_timestamp(value: str) -> Optional[datetime]:
    """
    Parse an RG&E CSV timestamp which may be timezone-aware.
    e.g. "2026-03-01 00:00:00-05:00"
    Returns an aware UTC datetime.
    """
    value = value.strip()
    # Python 3.7+ fromisoformat handles offset-aware strings
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        pass
    # Fallback for bare dates
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_file(file_path: str, service_type: str, last_time: str) -> ParseResult:
    """
    Parse an RG&E Green Button file (CSV or XML).

    Args:
        file_path:    Absolute path to the file.
        service_type: "electric" or "gas" — used to filter CSV rows by the
                      Type column, and inferred from XML commodity code.
        last_time:    UTC timestamp string of the last imported reading
                      (empty string = import everything).
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _parse_csv(path, service_type, last_time)
    elif suffix == ".xml":
        return _parse_xml(path, service_type, last_time)
    else:
        result = ParseResult(newest_time=last_time)
        result.errors.append(
            f"Unsupported file extension '{suffix}'. Expected .csv or .xml."
        )
        return result


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

def _parse_csv(path: Path, service_type: str, last_time: str) -> ParseResult:
    """Parse RG&E Opower CSV export."""
    result = ParseResult(newest_time=last_time)

    last_dt = _parse_stored_time(last_time)

    # Stream the file directly rather than reading it all into memory first,
    # which would create a full second copy as an io.StringIO buffer.
    try:
        f = path.open(encoding="utf-8-sig")
    except Exception as exc:
        result.errors.append(f"Could not read file '{path.name}': {exc}")
        return result

    with f:
        try:
            reader = csv.DictReader(f)
        except Exception as exc:
            result.errors.append(f"Failed to parse CSV '{path.name}': {exc}")
            return result

        if not reader.fieldnames:
            result.errors.append(f"'{path.name}' has no header row.")
            return result

        # Normalise header lookup (case-insensitive).
        # Guard against None entries that malformed CSV files can produce.
        headers_lower = {
            h.strip().lower(): h.strip()
            for h in reader.fieldnames
            if h is not None
        }

        def col(name: str) -> Optional[str]:
            return headers_lower.get(name.lower())

        time_col  = col("start time")
        usage_col = col("usage")
        type_col  = col("type")

        if not time_col:
            result.errors.append(
                f"'{path.name}': missing 'Start Time' column. "
                f"Found headers: {list(reader.fieldnames)}"
            )
            return result
        if not usage_col:
            result.errors.append(
                f"'{path.name}': missing 'Usage' column. "
                f"Found headers: {list(reader.fieldnames)}"
            )
            return result

        _LOGGER.debug(
            "[%s] CSV columns — time: '%s', usage: '%s', type: '%s'",
            path.name, time_col, usage_col, type_col,
        )

        for row in reader:
            # Filter by service type if the Type column exists
            if type_col:
                row_type = (row.get(type_col) or "").strip().lower()
                if row_type and row_type != service_type.lower():
                    result.rows_skipped += 1
                    continue

            raw_time  = (row.get(time_col)  or "").strip()
            raw_usage = (row.get(usage_col) or "").strip()

            if not raw_time:
                result.rows_skipped += 1
                continue

            row_dt = _parse_csv_timestamp(raw_time)
            if row_dt is None:
                _LOGGER.debug("[%s] Unparseable timestamp: '%s'", path.name, raw_time)
                result.rows_skipped += 1
                continue

            # Skip already-imported rows
            if last_dt is not None and row_dt <= last_dt:
                result.rows_skipped += 1
                continue

            try:
                usage = float(raw_usage.replace(",", ""))
            except (ValueError, AttributeError):
                _LOGGER.debug("[%s] Non-numeric usage: '%s'", path.name, raw_usage)
                result.rows_skipped += 1
                continue

            result.new_usage     += usage
            result.rows_imported += 1
            result.hourly_readings.append((row_dt, usage))

            stored = row_dt.strftime(_STORAGE_FMT)
            if not result.newest_time or stored > result.newest_time:
                result.newest_time = stored

    _LOGGER.info(
        "[%s] CSV %s: %d rows imported (%.4f units), %d skipped.",
        path.name, service_type, result.rows_imported, result.new_usage, result.rows_skipped,
    )
    return result


# ---------------------------------------------------------------------------
# XML / ESPI parser
# ---------------------------------------------------------------------------

def _parse_xml(path: Path, service_type: str, last_time: str) -> ParseResult:
    """
    Parse an ESPI Green Button XML export.

    RG&E emits two distinct XML formats depending on commodity:

    Electric  (ServiceCategory kind=0, ReadingType commodity=1, uom=72  / Wh):
        value=938000, powerOfTenMultiplier=-3
        → 938000 × 10⁻³ = 938 Wh ÷ 1000 = 0.938 kWh

    Gas       (ServiceCategory kind=1, ReadingType commodity=7, uom=169 / therms):
        value=702,    powerOfTenMultiplier=-3
        → 702 × 10⁻³ = 0.702 therms  (no extra ÷1000 — already in therms)

    The parser reads uom from ReadingType and applies the appropriate
    conversion automatically, so it works correctly for both without
    any caller-supplied hints.
    """
    result = ParseResult(newest_time=last_time)
    last_dt = _parse_stored_time(last_time)

    # ESPI uom codes relevant to RG&E
    _UOM_WH     = 72   # Wh  (electric) — needs ÷1000 to produce kWh
    _UOM_THERMS = 169  # therms (gas)   — powerOfTenMultiplier alone is sufficient

    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        result.errors.append(f"XML parse error in '{path.name}': {exc}")
        return result
    except Exception as exc:
        result.errors.append(f"Could not read '{path.name}': {exc}")
        return result

    root = tree.getroot()

    # Handle both bare ESPI namespace and Atom feed wrapper
    # RG&E may emit either:
    #   <feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">
    #   <feed xmlns="http://naesb.org/espi">
    # In both cases iter(espi("tag")) finds the elements correctly.
    def espi(tag: str) -> str:
        return f"{{{_ESPI_NS}}}{tag}"

    # ── Read ServiceCategory/kind to detect electric (0) vs gas (1) ──────────
    detected_kind: Optional[int] = None
    for sc in root.iter(espi("ServiceCategory")):
        kind_el = sc.find(espi("kind"))
        if kind_el is not None and kind_el.text:
            try:
                detected_kind = int(kind_el.text.strip())
            except ValueError:
                pass
            break

    _KIND_NAMES = {0: "electric", 1: "gas"}
    detected_service = _KIND_NAMES.get(detected_kind, "unknown") if detected_kind is not None else "unknown"

    if detected_kind is not None and detected_service != service_type.lower():
        _LOGGER.warning(
            "[%s] XML ServiceCategory kind=%d (%s) does not match expected "
            "service_type='%s'. Processing anyway.",
            path.name, detected_kind, detected_service, service_type,
        )

    # ── Read ReadingType: powerOfTenMultiplier and uom ───────────────────────
    power_of_ten: Optional[int] = None
    uom: int = _UOM_WH        # safe default; overridden below

    for rt in root.iter(espi("ReadingType")):
        pot_el = rt.find(espi("powerOfTenMultiplier"))
        uom_el = rt.find(espi("uom"))
        if pot_el is not None and pot_el.text:
            try:
                power_of_ten = int(pot_el.text.strip())
            except ValueError:
                pass
        if uom_el is not None and uom_el.text:
            try:
                uom = int(uom_el.text.strip())
            except ValueError:
                pass
        break  # only need the first ReadingType

    # If powerOfTenMultiplier was missing, default based on uom:
    # uom=72 (Wh): RG&E always uses -3, giving milli-Wh values
    # uom=169 (therms): RG&E always uses -3, giving milli-therms values
    if power_of_ten is None:
        power_of_ten = -3
        _LOGGER.warning(
            "[%s] powerOfTenMultiplier not found in ReadingType — defaulting to -3.",
            path.name,
        )

    # ── Determine unit conversion factor ─────────────────────────────────────
    base_multiplier = 10.0 ** power_of_ten

    if uom == _UOM_WH:
        unit_conversion = 1.0 / 1000.0   # Wh → kWh
        unit_label = "kWh"
    elif uom == _UOM_THERMS:
        unit_conversion = 1.0             # therms → therms (stored as CCF)
        unit_label = "therms"
    else:
        _LOGGER.warning(
            "[%s] Unknown uom=%d; applying powerOfTenMultiplier only. "
            "Values may be in unexpected units.",
            path.name, uom,
        )
        unit_conversion = 1.0
        unit_label = f"uom{uom}"

    final_multiplier = base_multiplier * unit_conversion

    _LOGGER.debug(
        "[%s] XML: service=%s, uom=%d (%s), powerOfTenMultiplier=%d "
        "→ final_multiplier=%.8f",
        path.name, detected_service, uom, unit_label, power_of_ten, final_multiplier,
    )

    # ── Parse IntervalReadings ────────────────────────────────────────────────
    readings_found = 0

    for interval_reading in root.iter(espi("IntervalReading")):
        time_period = interval_reading.find(espi("timePeriod"))
        if time_period is None:
            result.rows_skipped += 1
            continue

        start_el = time_period.find(espi("start"))
        value_el = interval_reading.find(espi("value"))

        if start_el is None or value_el is None:
            result.rows_skipped += 1
            continue

        try:
            epoch     = int(start_el.text.strip())
            raw_value = int(value_el.text.strip())
        except (ValueError, AttributeError):
            result.rows_skipped += 1
            continue

        row_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        usage  = raw_value * final_multiplier

        readings_found += 1

        if last_dt is not None and row_dt <= last_dt:
            result.rows_skipped += 1
            continue

        result.new_usage     += usage
        result.rows_imported += 1
        result.hourly_readings.append((row_dt, usage))

        stored = row_dt.strftime(_STORAGE_FMT)
        if not result.newest_time or stored > result.newest_time:
            result.newest_time = stored

    if readings_found == 0:
        result.errors.append(
            f"'{path.name}': no IntervalReading elements found. "
            "Verify this is a valid Green Button ESPI XML export."
        )

    _LOGGER.info(
        "[%s] XML %s: %d readings imported (%.4f %s), %d skipped.",
        path.name, service_type, result.rows_imported,
        result.new_usage, unit_label, result.rows_skipped,
    )
    return result