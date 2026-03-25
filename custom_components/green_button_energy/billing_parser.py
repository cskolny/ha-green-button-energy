"""Parser for Avangrid monthly billing CSV exports.

Billing CSV format (Avangrid Opower monthly export)
----------------------------------------------------
Columns: Name, Address, Account Number, Service, Type, Date,
         Start Time, End Time, Usage, Units, Costs, Weather

Each row represents a single billing cycle (typically 28–33 days).

- Start Time : billing period start (midnight, naive local or ISO)
- End Time   : billing period end (exclusive, midnight)
- Usage      : total kWh or therms for the billing period
- Costs      : dollar amount billed for the period (float, e.g. 85.15)

This parser **spreads** each bill's cost evenly across all hours in the
billing period so that HA's long-term statistics database receives one
hourly cost record per hour — exactly as the Energy Dashboard cost
feature expects.

For example, a $85.15 bill covering 768 hours becomes $0.1109/hour
written to each of those 768 hourly statistics rows.

Both commodity types (electric and gas) use the same column layout, so
the same parser handles both.  The ``service_type`` parameter filters
rows by the ``Type`` column exactly as the usage parser does.

Deduplication
-------------
``last_time`` (UTC, ``STORAGE_TIME_FMT``) is compared against each
row's ``Start Time``.  Any billing cycle whose START is at or before
``last_time`` is skipped.  Because billing cycles do not overlap, this
guarantees that a re-import of the same file writes 0 new rows.

Returns
-------
:class:`BillingParseResult` — structurally similar to
:class:`~.parser.ParseResult` but carries ``hourly_costs`` (a list of
``(aware UTC datetime, cost_usd)`` tuples) rather than usage readings.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .parser import STORAGE_TIME_FMT, _parse_csv_timestamp, _parse_stored_time

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BillingParseResult:
    """Diagnostics and data returned by a billing parse operation.

    Attributes:
        new_cost: Total dollar amount accumulated from newly-imported cycles.
        newest_time: UTC timestamp string (``STORAGE_TIME_FMT``) of the most
            recent billing cycle START accepted.  Empty when no rows imported.
        cycles_imported: Number of billing cycles accepted.
        cycles_skipped: Number of cycles skipped (duplicate or invalid).
        errors: Human-readable error strings.  Non-empty means parse failed.
        hourly_costs: Individual per-hour cost records as
            ``(aware UTC datetime, cost_usd float)`` tuples, one per hour
            across every accepted billing cycle.  Consumed by ``sensor.py``
            to write historical cost statistics into the recorder.
    """

    new_cost: float = 0.0
    newest_time: str = ""
    cycles_imported: int = 0
    cycles_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    hourly_costs: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return ``True`` when no errors were recorded."""
        return len(self.errors) == 0

    @property
    def has_new_data(self) -> bool:
        """Return ``True`` when at least one billing cycle was accepted."""
        return self.new_cost > 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_billing_file(
    file_path: str,
    service_type: str,
    last_time: str,
) -> BillingParseResult:
    """Parse an Avangrid monthly billing CSV export.

    Only ``.csv`` files are supported for billing data — there is no
    standard Green Button XML billing format.

    Args:
        file_path: Absolute path to the file on disk.
        service_type: ``"electric"`` or ``"gas"`` — used to filter rows by
            the ``Type`` column.
        last_time: UTC timestamp string of the last successfully-imported
            billing cycle START (``STORAGE_TIME_FMT``).  Pass an empty
            string to import all cycles in the file.

    Returns:
        A :class:`BillingParseResult` with accepted hourly cost records and
        diagnostics.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix != ".csv":
        result = BillingParseResult(newest_time=last_time)
        result.errors.append(
            f"Billing imports only support .csv files; got '{suffix}'."
        )
        return result

    return _parse_billing_csv(path, service_type, last_time)


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


def _parse_billing_csv(
    path: Path,
    service_type: str,
    last_time: str,
) -> BillingParseResult:
    """Parse an Avangrid monthly billing CSV.

    Spreads each billing cycle's cost evenly across the hours in the cycle,
    producing one hourly-cost :class:`StatisticData` record per hour.

    Args:
        path: Path to the ``.csv`` file.
        service_type: ``"electric"`` or ``"gas"``.
        last_time: UTC cutoff — billing cycles whose START is at or before
            this timestamp are skipped.

    Returns:
        A :class:`BillingParseResult` with accepted records and diagnostics.
    """
    result = BillingParseResult(newest_time=last_time)
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

    # Case-insensitive header lookup.
    headers_lower: dict[str, str] = {
        h.strip().lower(): h.strip() for h in reader.fieldnames
    }

    def _col(name: str) -> str | None:
        return headers_lower.get(name.lower())

    start_col = _col("start time")
    end_col = _col("end time")
    cost_col = _col("costs")
    type_col = _col("type")

    if not start_col:
        result.errors.append(
            f"'{path.name}': missing 'Start Time' column. "
            f"Found headers: {list(reader.fieldnames)}"
        )
        return result
    if not end_col:
        result.errors.append(
            f"'{path.name}': missing 'End Time' column. "
            f"Found headers: {list(reader.fieldnames)}"
        )
        return result
    if not cost_col:
        result.errors.append(
            f"'{path.name}': missing 'Costs' column. "
            f"Found headers: {list(reader.fieldnames)}"
        )
        return result

    _LOGGER.debug(
        "[%s] Billing CSV columns — start: '%s', end: '%s', cost: '%s', type: '%s'",
        path.name,
        start_col,
        end_col,
        cost_col,
        type_col,
    )

    rows_found = 0

    for row in reader:
        # Filter by service type when the Type column is present.
        if type_col:
            row_type = (row.get(type_col) or "").strip().lower()
            if row_type and row_type != service_type.lower():
                result.cycles_skipped += 1
                continue

        raw_start = (row.get(start_col) or "").strip()
        raw_end = (row.get(end_col) or "").strip()
        raw_cost = (row.get(cost_col) or "").strip()

        if not raw_start or not raw_end:
            result.cycles_skipped += 1
            continue

        start_dt = _parse_csv_timestamp(raw_start)
        end_dt = _parse_csv_timestamp(raw_end)

        if start_dt is None or end_dt is None:
            _LOGGER.debug(
                "[%s] Unparseable timestamps: start='%s' end='%s'",
                path.name,
                raw_start,
                raw_end,
            )
            result.cycles_skipped += 1
            continue

        rows_found += 1

        # Skip billing cycles already imported (compare by START timestamp).
        if last_dt is not None and start_dt <= last_dt:
            result.cycles_skipped += 1
            continue

        # Parse cost — strip leading $ if present (e.g. "$85.15" or "85.15").
        cost_str = raw_cost.lstrip("$").replace(",", "").strip()
        try:
            cost = float(cost_str)
        except (ValueError, AttributeError):
            _LOGGER.debug("[%s] Non-numeric cost: '%s'", path.name, raw_cost)
            result.cycles_skipped += 1
            continue

        # Skip zero or negative cost rows.
        if cost <= 0:
            _LOGGER.debug(
                "[%s] Skipping non-positive cost %.4f at %s",
                path.name,
                cost,
                start_dt,
            )
            result.cycles_skipped += 1
            continue

        # Spread cost evenly across all whole hours in the billing cycle.
        # HA's statistics DB stores one record per hour, so we write one
        # cost entry per hour-aligned slot covering [start_dt, end_dt).
        cycle_hours = _enumerate_hours(start_dt, end_dt)
        if not cycle_hours:
            _LOGGER.warning(
                "[%s] Billing cycle %s -> %s yielded no hours; skipping.",
                path.name,
                start_dt,
                end_dt,
            )
            result.cycles_skipped += 1
            continue

        cost_per_hour = cost / len(cycle_hours)

        for hour_dt in cycle_hours:
            result.hourly_costs.append((hour_dt, cost_per_hour))

        result.new_cost += cost
        result.cycles_imported += 1

        stored = start_dt.strftime(STORAGE_TIME_FMT)
        if not result.newest_time or stored > result.newest_time:
            result.newest_time = stored

    if rows_found == 0:
        result.errors.append(
            f"'{path.name}': no billing rows found for service_type='{service_type}'. "
            "Verify the file is an Avangrid monthly billing CSV export."
        )

    _LOGGER.info(
        "[%s] Billing CSV %s: %d cycles imported ($%.2f), %d skipped.",
        path.name,
        service_type,
        result.cycles_imported,
        result.new_cost,
        result.cycles_skipped,
    )
    return result


# ---------------------------------------------------------------------------
# Hour enumeration helper
# ---------------------------------------------------------------------------


def _enumerate_hours(
    start_dt: datetime,
    end_dt: datetime,
) -> list[datetime]:
    """Return a list of hour-aligned UTC datetimes in [start_dt, end_dt).

    Each returned datetime is truncated to the whole hour (minutes/seconds
    zeroed out) and is timezone-aware (UTC).  The range is half-open:
    ``start_dt`` is included; ``end_dt`` is excluded.

    For a billing cycle spanning 768 hours this returns 768 datetimes.

    Args:
        start_dt: Billing cycle start (aware UTC datetime).
        end_dt: Billing cycle end, exclusive (aware UTC datetime).

    Returns:
        List of hour-aligned UTC datetimes.
    """
    # Truncate start to the top of its hour.
    current = start_dt.replace(minute=0, second=0, microsecond=0)

    hours: list[datetime] = []
    while current < end_dt:
        hours.append(current)
        current = current + timedelta(hours=1)

    return hours
