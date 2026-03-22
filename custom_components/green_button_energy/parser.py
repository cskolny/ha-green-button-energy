"""Parser for Avangrid Green Button exports — CSV and ESPI XML formats.

CSV format (Avangrid Opower export)
------------------------------------
Columns: Name, Address, Account Number, Service, Type, Date,
         Start Time, End Time, Usage, Units, Costs, Weather

- Timestamp : ``Start Time`` column, timezone-aware ISO format,
              e.g. ``"2026-03-01 00:00:00-05:00"``
- Usage     : ``Usage`` column, float in kWh (electric) or therms (gas)
- Type      : ``Type`` column (``"electric"`` or ``"gas"``) — used to filter
              rows so a single file containing both commodity types is handled
              correctly.

XML format (ESPI / Green Button standard)
------------------------------------------
- ``IntervalReading`` values are in Wh × 10^powerOfTenMultiplier.
  RG&E uses ``powerOfTenMultiplier=-3`` and ``uom=72`` (Wh); divide by 1000
  to get kWh.
- Timestamps are Unix epoch integers (UTC).

Both parsers
-------------
- Skip rows/readings already imported (timestamp ≤ ``last_time``).
- Skip rows with negative or zero usage (utility correction rows).
- Run in an executor thread pool — no event-loop blocking.
- Return a :class:`ParseResult` dataclass with full diagnostics.
"""

from __future__ import annotations

import contextlib
import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

_LOGGER = logging.getLogger(__name__)

# ESPI XML namespace used by all Green Button / NAESB exports.
_ESPI_NS = "http://naesb.org/espi"

# Canonical UTC timestamp format used for storage.
# Public name is imported by sensor.py; the private alias is kept for
# backward compatibility with any external callers that used _STORAGE_FMT.
STORAGE_TIME_FMT = "%Y-%m-%d %H:%M:%S+00:00"
_STORAGE_FMT = STORAGE_TIME_FMT  # backward-compat alias


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Diagnostics and data returned by a single parse operation.

    Attributes:
        new_usage: Total usage accumulated from newly-imported rows.
        newest_time: UTC timestamp string (``STORAGE_TIME_FMT``) of the most
            recent row accepted. Empty when no rows were imported.
        rows_imported: Number of rows accepted and included in
            ``hourly_readings``.
        rows_skipped: Number of rows rejected (duplicate, non-positive usage,
            wrong service type, or parse failure).
        errors: Human-readable error strings.  A non-empty list means the
            parse failed and ``hourly_readings`` is unreliable.
        hourly_readings: Individual accepted readings as
            ``(aware UTC datetime, usage float)`` tuples.  Consumed by
            ``sensor.py`` to write historical statistics into the recorder.
    """

    new_usage: float = 0.0
    newest_time: str = ""
    rows_imported: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    hourly_readings: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return ``True`` when no errors were recorded."""
        return len(self.errors) == 0

    @property
    def has_new_data(self) -> bool:
        """Return ``True`` when at least one row contributed positive usage."""
        return self.new_usage > 0


# ---------------------------------------------------------------------------
# Internal timestamp helpers
# ---------------------------------------------------------------------------


def _parse_stored_time(value: str) -> datetime | None:
    """Parse a stored ``last_time`` string into an aware UTC datetime.

    Always returns a timezone-aware :class:`datetime` so comparisons with
    other aware datetimes never raise :exc:`TypeError`.  Legacy naive strings
    are treated as UTC.

    Args:
        value: Timestamp string as written to ``.storage``, or an empty string.

    Returns:
        An aware UTC :class:`datetime`, or ``None`` when *value* is empty or
        unparseable.
    """
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    # Fallback: handle legacy naive strings written by very old versions.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue

    return None


def _parse_csv_timestamp(value: str) -> datetime | None:
    """Parse an Avangrid CSV timestamp into an aware UTC datetime.

    Handles timezone-aware ISO strings (e.g. ``"2026-03-01 00:00:00-05:00"``)
    and falls back to bare date strings (``"YYYY-MM-DD"``).

    Args:
        value: Raw timestamp string from the ``Start Time`` CSV column.

    Returns:
        An aware UTC :class:`datetime`, or ``None`` if *value* is unparseable.
    """
    value = value.strip()

    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        pass

    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        pass

    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_file(file_path: str, service_type: str, last_time: str) -> ParseResult:
    """Dispatch to the correct parser based on the file extension.

    Args:
        file_path: Absolute path to the file on disk.
        service_type: ``"electric"`` or ``"gas"`` — used to filter CSV rows
            by the ``Type`` column, and cross-checked against XML commodity
            metadata.
        last_time: UTC timestamp string of the last successfully-imported
            reading (``STORAGE_TIME_FMT``).  Pass an empty string to import
            everything in the file.

    Returns:
        A :class:`ParseResult` containing all accepted readings and diagnostics.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _parse_csv(path, service_type, last_time)
    if suffix == ".xml":
        return _parse_xml(path, service_type, last_time)

    result = ParseResult(newest_time=last_time)
    result.errors.append(
        f"Unsupported file extension '{suffix}'. Expected .csv or .xml."
    )
    return result


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


def _parse_csv(path: Path, service_type: str, last_time: str) -> ParseResult:
    """Parse an Avangrid Opower CSV export.

    Args:
        path: Path to the ``.csv`` file.
        service_type: ``"electric"`` or ``"gas"``.
        last_time: UTC cutoff — rows at or before this timestamp are skipped.

    Returns:
        A :class:`ParseResult` with accepted rows and diagnostics.
    """
    result = ParseResult(newest_time=last_time)
    last_dt = _parse_stored_time(last_time)

    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        result.errors.append(f"Could not read file '{path.name}': {exc}")
        return result

    try:
        reader = csv.DictReader(io.StringIO(content))
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Failed to parse CSV '{path.name}': {exc}")
        return result

    if not reader.fieldnames:
        result.errors.append(f"'{path.name}' has no header row.")
        return result

    # Build a case-insensitive header lookup so column names are matched
    # regardless of how the utility capitalises them.
    headers_lower: dict[str, str] = {
        h.strip().lower(): h.strip() for h in reader.fieldnames
    }

    def _col(name: str) -> str | None:
        return headers_lower.get(name.lower())

    time_col = _col("start time")
    usage_col = _col("usage")
    type_col = _col("type")

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
        path.name,
        time_col,
        usage_col,
        type_col,
    )

    for row in reader:
        # Filter by service type when the Type column is present.
        if type_col:
            row_type = (row.get(type_col) or "").strip().lower()
            if row_type and row_type != service_type.lower():
                result.rows_skipped += 1
                continue

        raw_time = (row.get(time_col) or "").strip()
        raw_usage = (row.get(usage_col) or "").strip()

        if not raw_time:
            result.rows_skipped += 1
            continue

        row_dt = _parse_csv_timestamp(raw_time)
        if row_dt is None:
            _LOGGER.debug("[%s] Unparseable timestamp: '%s'", path.name, raw_time)
            result.rows_skipped += 1
            continue

        # Skip rows that have already been imported.
        if last_dt is not None and row_dt <= last_dt:
            result.rows_skipped += 1
            continue

        try:
            usage = float(raw_usage.replace(",", ""))
        except (ValueError, AttributeError):
            _LOGGER.debug("[%s] Non-numeric usage: '%s'", path.name, raw_usage)
            result.rows_skipped += 1
            continue

        # Skip negative or zero usage — these are utility correction rows that
        # would corrupt the cumulative sum if accepted.
        if usage <= 0:
            _LOGGER.debug(
                "[%s] Skipping non-positive usage %.4f at %s",
                path.name,
                usage,
                row_dt,
            )
            result.rows_skipped += 1
            continue

        result.new_usage += usage
        result.rows_imported += 1
        result.hourly_readings.append((row_dt, usage))

        stored = row_dt.strftime(STORAGE_TIME_FMT)
        if not result.newest_time or stored > result.newest_time:
            result.newest_time = stored

    _LOGGER.info(
        "[%s] CSV %s: %d rows imported (%.4f units), %d skipped.",
        path.name,
        service_type,
        result.rows_imported,
        result.new_usage,
        result.rows_skipped,
    )
    return result


# ---------------------------------------------------------------------------
# XML / ESPI parser
# ---------------------------------------------------------------------------


def _parse_xml(path: Path, service_type: str, last_time: str) -> ParseResult:
    """Parse an ESPI Green Button XML export.

    RG&E emits two distinct XML schemas depending on commodity:

    **Electric** (``ServiceCategory kind=0``, ``ReadingType uom=72`` / Wh)::

        value=938000, powerOfTenMultiplier=-3
        -> 938 000 x 10^-3 = 938 Wh / 1 000 = 0.938 kWh

    **Gas** (``ServiceCategory kind=1``, ``ReadingType uom=169`` / therms)::

        value=702, powerOfTenMultiplier=-3
        -> 702 x 10^-3 = 0.702 therms  (no extra /1 000 -- already in therms)

    The unit conversion is read from ``ReadingType`` metadata and applied
    automatically, so both commodity types are handled correctly without any
    caller-supplied hints.

    Args:
        path: Path to the ``.xml`` file.
        service_type: ``"electric"`` or ``"gas"``.
        last_time: UTC cutoff -- readings at or before this timestamp are skipped.

    Returns:
        A :class:`ParseResult` with accepted readings and diagnostics.
    """
    result = ParseResult(newest_time=last_time)
    last_dt = _parse_stored_time(last_time)

    # ESPI uom codes relevant to Avangrid exports.
    _UOM_WH = 72       # Wh  (electric) -- needs / 1 000 to produce kWh
    _UOM_THERMS = 169  # therms (gas)   -- powerOfTenMultiplier alone is sufficient

    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        result.errors.append(f"XML parse error in '{path.name}': {exc}")
        return result
    except OSError as exc:
        result.errors.append(f"Could not read '{path.name}': {exc}")
        return result

    root = tree.getroot()

    def _espi(tag: str) -> str:
        """Return a fully-qualified ESPI element name."""
        return f"{{{_ESPI_NS}}}{tag}"

    # -- Detect commodity type from ServiceCategory/kind -------------------
    detected_kind: int | None = None
    for sc in root.iter(_espi("ServiceCategory")):
        kind_el = sc.find(_espi("kind"))
        if kind_el is not None and kind_el.text:
            with contextlib.suppress(ValueError):
                detected_kind = int(kind_el.text.strip())
        break  # Only the first ServiceCategory is needed.

    _KIND_NAMES: dict[int, str] = {0: "electric", 1: "gas"}
    detected_service = (
        _KIND_NAMES.get(detected_kind, "unknown")
        if detected_kind is not None
        else "unknown"
    )

    if detected_kind is not None and detected_service != service_type.lower():
        _LOGGER.warning(
            "[%s] XML ServiceCategory kind=%d (%s) does not match expected "
            "service_type='%s'. Processing anyway.",
            path.name,
            detected_kind,
            detected_service,
            service_type,
        )

    # -- Read ReadingType: powerOfTenMultiplier and uom --------------------
    power_of_ten: int | None = None
    uom: int | None = None

    for rt in root.iter(_espi("ReadingType")):
        pot_el = rt.find(_espi("powerOfTenMultiplier"))
        uom_el = rt.find(_espi("uom"))
        if pot_el is not None and pot_el.text:
            with contextlib.suppress(ValueError):
                power_of_ten = int(pot_el.text.strip())
        if uom_el is not None and uom_el.text:
            with contextlib.suppress(ValueError):
                uom = int(uom_el.text.strip())
        break  # Only the first ReadingType is needed.

    # If uom is absent, infer from service_type rather than defaulting to the
    # electric conversion (Wh / 1 000), which would produce values ~1 000x
    # too small for gas exports.
    if uom is None:
        uom = _UOM_WH if service_type.lower() == "electric" else _UOM_THERMS
        _LOGGER.warning(
            "[%s] uom not found in ReadingType -- inferred %d from service_type='%s'.",
            path.name,
            uom,
            service_type,
        )

    if power_of_ten is None:
        power_of_ten = -3
        _LOGGER.warning(
            "[%s] powerOfTenMultiplier not found in ReadingType -- defaulting to -3.",
            path.name,
        )

    # -- Determine final unit-conversion multiplier ------------------------
    base_multiplier = 10.0**power_of_ten

    if uom == _UOM_WH:
        unit_conversion = 1.0 / 1000.0  # Wh -> kWh
        unit_label = "kWh"
    elif uom == _UOM_THERMS:
        unit_conversion = 1.0  # therms -> therms (stored as CCF)
        unit_label = "therms"
    else:
        _LOGGER.warning(
            "[%s] Unknown uom=%d; applying powerOfTenMultiplier only. "
            "Values may be in unexpected units.",
            path.name,
            uom,
        )
        unit_conversion = 1.0
        unit_label = f"uom{uom}"

    final_multiplier = base_multiplier * unit_conversion

    _LOGGER.debug(
        "[%s] XML: service=%s, uom=%d (%s), powerOfTenMultiplier=%d "
        "-> final_multiplier=%.8f",
        path.name,
        detected_service,
        uom,
        unit_label,
        power_of_ten,
        final_multiplier,
    )

    # -- Parse all IntervalReading elements --------------------------------
    readings_found = 0

    for interval_reading in root.iter(_espi("IntervalReading")):
        time_period = interval_reading.find(_espi("timePeriod"))
        if time_period is None:
            result.rows_skipped += 1
            continue

        start_el = time_period.find(_espi("start"))
        value_el = interval_reading.find(_espi("value"))

        if start_el is None or value_el is None:
            result.rows_skipped += 1
            continue

        try:
            epoch = int(start_el.text.strip())  # type: ignore[union-attr]
            raw_value = int(value_el.text.strip())  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            result.rows_skipped += 1
            continue

        row_dt = datetime.fromtimestamp(epoch, tz=UTC)
        usage = raw_value * final_multiplier

        readings_found += 1

        if last_dt is not None and row_dt <= last_dt:
            result.rows_skipped += 1
            continue

        # Skip negative or zero usage -- utility correction rows.
        if usage <= 0:
            _LOGGER.debug(
                "[%s] Skipping non-positive usage %.4f at %s",
                path.name,
                usage,
                row_dt,
            )
            result.rows_skipped += 1
            continue

        result.new_usage += usage
        result.rows_imported += 1
        result.hourly_readings.append((row_dt, usage))

        stored = row_dt.strftime(STORAGE_TIME_FMT)
        if not result.newest_time or stored > result.newest_time:
            result.newest_time = stored

    if readings_found == 0:
        result.errors.append(
            f"'{path.name}': no IntervalReading elements found. "
            "Verify this is a valid Green Button ESPI XML export."
        )

    _LOGGER.info(
        "[%s] XML %s: %d readings imported (%.4f %s), %d skipped.",
        path.name,
        service_type,
        result.rows_imported,
        result.new_usage,
        unit_label,
        result.rows_skipped,
    )
    return result
