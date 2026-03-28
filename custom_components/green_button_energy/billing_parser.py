"""Parser for Avangrid monthly billing CSV exports.

Billing CSV format (Avangrid Opower monthly export)
----------------------------------------------------
Columns: Name, Address, Account Number, Service, Type, Date,
         Start Time, End Time, Usage, Units, Costs, Weather

Each row represents a single billing cycle (typically 28--33 days).

- Start Time : billing period start (midnight local time, naive string)
- End Time   : billing period end (midnight local time, naive string)
- Usage      : total kWh or therms for the billing period
- Costs      : dollar amount billed for the period (float, e.g. 85.15)

Timezone handling
-----------------
Avangrid billing CSV timestamps are naive strings (no UTC offset), but
they represent **local midnight** in the utility's service territory
(America/New_York for all supported Avangrid utilities).  They must be
interpreted as Eastern time before conversion to UTC.

If naive timestamps were treated as UTC, cycle boundaries would land at
UTC midnight = 19:00 or 20:00 Eastern (depending on DST).  The HA
Energy Dashboard groups statistics into local calendar days, so a cycle
boundary at 19:00 Eastern would cause the last 5 hours of one local day
and the first 19 hours of the next to straddle two different billing
cycles, producing inconsistent daily cost totals.

Treating the timestamps as Eastern local time ensures every cycle
boundary lands at local midnight, so every local calendar day is
covered entirely by a single cycle's hourly rate.

Intra-file gap filling
----------------------
Avangrid billing CSVs have a one-day gap between consecutive billing
cycles (cycle N ends on day X and cycle N+1 starts on day X+2).  To
prevent $0 gap days in the Energy Dashboard, each cycle's effective end
is extended to the next cycle's start before hours are enumerated.
The last cycle keeps its original end date.

Inter-import gap filling
------------------------
When a new billing CSV is imported that covers only the most recent
cycle (e.g. the monthly update), there is a gap between where the
previous import's DB chain ended and where the new cycle starts.

Example: previous import covered cycles through 2026-02-24 (Eastern).
New file adds cycle 2026-02-25 to 2026-03-25.  The previous import's
last cycle ended at 2026-02-24 Eastern midnight, so the DB chain stops
at 2026-02-24 05:00 UTC.  The new cycle starts at 2026-02-25 05:00 UTC.
Local day 2026-02-24 (Eastern) spans 2026-02-24 05:00 to
2026-02-25 05:00 UTC -- entirely in the gap, showing $0.00.

Fix: ``parse_billing_file`` accepts a ``last_effective_end`` timestamp
(stored after each import) indicating where the DB chain actually ends.
If the first new cycle's start is after ``last_effective_end``, that
cycle's effective start is moved back to ``last_effective_end``, filling
the gap at the new cycle's rate.  The cost is unchanged; it is simply
spread over a slightly larger number of hours.

Deduplication
-------------
``last_time`` (UTC, ``STORAGE_TIME_FMT``) is compared against each
row's ``Start Time`` (converted to UTC).  Any billing cycle whose START
is at or before ``last_time`` is skipped.

Returns
-------
:class:`BillingParseResult` -- carries ``hourly_costs`` as a list of
``(aware UTC datetime, cost_usd)`` tuples, one per hour across every
accepted billing cycle (all gaps filled).  Also carries
``last_effective_end`` so the caller can persist it for the next import.
"""

from __future__ import annotations

import csv
import io
import logging
import zoneinfo
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .parser import STORAGE_TIME_FMT, _parse_stored_time

_LOGGER = logging.getLogger(__name__)

# All supported Avangrid utilities are in the Eastern timezone.
_EASTERN = zoneinfo.ZoneInfo("America/New_York")


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
        last_effective_end: UTC timestamp string (``STORAGE_TIME_FMT``) of
            where the DB chain actually ends after this import -- the
            effective end of the last written cycle.  Persisted by the caller
            and passed back as ``last_effective_end`` on the next import so
            inter-import gaps can be filled.  Empty when no rows were written.
        cycles_imported: Number of billing cycles accepted.
        cycles_skipped: Number of cycles skipped (duplicate or invalid).
        errors: Human-readable error strings.  Non-empty means parse failed.
        hourly_costs: Individual per-hour cost records as
            ``(aware UTC datetime, cost_usd float)`` tuples, one per hour
            across every accepted billing cycle (all gaps filled).
    """

    new_cost: float = 0.0
    newest_time: str = ""
    last_effective_end: str = ""
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
    last_effective_end: str = "",
) -> BillingParseResult:
    """Parse an Avangrid monthly billing CSV export.

    Only ``.csv`` files are supported for billing data -- there is no
    standard Green Button XML billing format containing cost data.

    Args:
        file_path: Absolute path to the file on disk.
        service_type: ``"electric"`` or ``"gas"`` -- used to filter rows by
            the ``Type`` column.
        last_time: UTC timestamp string of the last successfully-imported
            billing cycle START (``STORAGE_TIME_FMT``).  Pass an empty
            string to import all cycles in the file.
        last_effective_end: UTC timestamp string of where the previous
            import's DB chain actually ended -- the effective end of the
            last written cycle (``STORAGE_TIME_FMT``).  Used to fill the
            gap between imports when a new cycle's start is after this
            value.  Pass an empty string on first import.

    Returns:
        A :class:`BillingParseResult` with accepted hourly cost records,
        diagnostics, and the new ``last_effective_end`` to persist.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix != ".csv":
        result = BillingParseResult(newest_time=last_time)
        result.errors.append(
            f"Billing imports only support .csv files; got '{suffix}'."
        )
        return result

    return _parse_billing_csv(path, service_type, last_time, last_effective_end)


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _parse_billing_timestamp(value: str) -> datetime | None:
    """Parse a naive billing CSV timestamp as Eastern local time -> UTC.

    Avangrid billing exports use naive midnight strings (e.g.
    ``"2026-01-28 00:00:00"`` or ``"2026-01-28"``).  These represent
    local midnight in America/New_York.  Interpreting them as UTC would
    place cycle boundaries at 19:00 or 20:00 Eastern, causing daily cost
    totals in the Energy Dashboard to straddle two billing cycles.

    Args:
        value: Raw timestamp string from the CSV.

    Returns:
        An aware UTC :class:`datetime`, or ``None`` if unparseable.
    """
    value = value.strip()

    # If the string already carries a UTC offset, honour it directly.
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            return dt.astimezone(UTC)
    except ValueError:
        pass

    # Naive string -- treat as Eastern local midnight.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(value, fmt)
            local = naive.replace(tzinfo=_EASTERN)
            return local.astimezone(UTC)
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


def _parse_billing_csv(
    path: Path,
    service_type: str,
    last_time: str,
    last_effective_end: str,
) -> BillingParseResult:
    """Parse an Avangrid monthly billing CSV.

    Two-pass approach:

    **Pass 1** -- Read and validate every row, collecting accepted cycles
    into a list as ``(start_utc, end_utc, cost)`` tuples.  Naive
    timestamps are interpreted as Eastern local midnight and converted
    to UTC so cycle boundaries align with local calendar days.

    **Pass 2** -- Sort cycles chronologically, then for each cycle:

    - If it is the *first* new cycle and there is a gap between
      ``last_effective_end`` and this cycle's start, extend the cycle's
      effective start back to ``last_effective_end`` (inter-import gap
      fill).
    - Extend the cycle's effective end to the *next* cycle's start
      (intra-file gap fill).
    - Enumerate hours and compute cost-per-hour.

    Args:
        path: Path to the ``.csv`` file.
        service_type: ``"electric"`` or ``"gas"``.
        last_time: UTC cutoff -- billing cycles whose START is at or before
            this timestamp are skipped.
        last_effective_end: Where the previous import's DB chain ended.
            Empty string on first import.

    Returns:
        A :class:`BillingParseResult` with accepted records, diagnostics,
        and ``last_effective_end`` for the caller to persist.
    """
    result = BillingParseResult(newest_time=last_time)
    last_dt = _parse_stored_time(last_time)
    prev_end_dt = _parse_stored_time(last_effective_end)

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
        "[%s] Billing CSV columns -- start: '%s', end: '%s', cost: '%s', type: '%s'",
        path.name,
        start_col,
        end_col,
        cost_col,
        type_col,
    )

    # ----------------------------------------------------------------
    # Pass 1 -- collect valid cycles
    # ----------------------------------------------------------------

    # Each entry: (start_utc, end_utc, cost)
    valid_cycles: list[tuple[datetime, datetime, float]] = []
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

        # Parse as Eastern local midnight -> UTC.
        start_utc = _parse_billing_timestamp(raw_start)
        end_utc = _parse_billing_timestamp(raw_end)

        if start_utc is None or end_utc is None:
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
        if last_dt is not None and start_utc <= last_dt:
            result.cycles_skipped += 1
            continue

        # Parse cost -- strip leading $ if present (e.g. "$85.15" or "85.15").
        cost_str = raw_cost.lstrip("$").replace(",", "").strip()
        try:
            cost = float(cost_str)
        except (ValueError, AttributeError):
            _LOGGER.debug("[%s] Non-numeric cost: '%s'", path.name, raw_cost)
            result.cycles_skipped += 1
            continue

        if cost <= 0:
            _LOGGER.debug(
                "[%s] Skipping non-positive cost %.4f at %s",
                path.name,
                cost,
                start_utc,
            )
            result.cycles_skipped += 1
            continue

        valid_cycles.append((start_utc, end_utc, cost))

    if rows_found == 0:
        result.errors.append(
            f"'{path.name}': no billing rows found for service_type='{service_type}'. "
            "Verify the file is an Avangrid monthly billing CSV export."
        )
        return result

    if not valid_cycles:
        _LOGGER.info(
            "[%s] Billing CSV %s: 0 new cycles (all skipped).",
            path.name,
            service_type,
        )
        return result

    # Sort chronologically so gap-filling and newest_time tracking are correct.
    valid_cycles.sort(key=lambda c: c[0])

    # ----------------------------------------------------------------
    # Pass 2 -- gap-fill and enumerate hours
    # ----------------------------------------------------------------

    final_effective_end: datetime | None = None

    for i, (start_utc, end_utc, cost) in enumerate(valid_cycles):
        # --- Intra-file gap fill: extend end to next cycle's start ---
        if i + 1 < len(valid_cycles):
            effective_end = valid_cycles[i + 1][0]
        else:
            effective_end = end_utc

        # --- Inter-import gap fill: extend start back to previous DB end ---
        # If this is the first new cycle and there's a gap between where
        # the DB chain ended (prev_end_dt) and this cycle's CSV start,
        # push the effective start back to prev_end_dt to cover the gap.
        if i == 0 and prev_end_dt is not None and prev_end_dt < start_utc:
            gap_hours = int((start_utc - prev_end_dt).total_seconds() / 3600)
            if gap_hours > 0:
                _LOGGER.info(
                    "[%s] Inter-import gap: extending cycle %s start back %d hours "
                    "to %s to cover gap since last import.",
                    path.name,
                    start_utc.astimezone(_EASTERN).date(),
                    gap_hours,
                    prev_end_dt,
                )
                effective_start = prev_end_dt
            else:
                effective_start = start_utc
        else:
            effective_start = start_utc

        if effective_end <= effective_start:
            _LOGGER.warning(
                "[%s] Billing cycle %s has zero or negative duration after gap fill; skipping.",
                path.name,
                start_utc,
            )
            result.cycles_skipped += 1
            continue

        cycle_hours = _enumerate_hours(effective_start, effective_end)
        if not cycle_hours:
            _LOGGER.warning(
                "[%s] Billing cycle %s -> %s yielded no hours; skipping.",
                path.name,
                effective_start,
                effective_end,
            )
            result.cycles_skipped += 1
            continue

        cost_per_hour = cost / len(cycle_hours)

        _LOGGER.debug(
            "[%s] Cycle %s -> %s (eff start %s, eff end %s): %d hours, $%.6f/hr",
            path.name,
            start_utc.astimezone(_EASTERN).date(),
            end_utc.astimezone(_EASTERN).date(),
            effective_start.astimezone(_EASTERN).date(),
            effective_end.astimezone(_EASTERN).date(),
            len(cycle_hours),
            cost_per_hour,
        )

        for hour_dt in cycle_hours:
            result.hourly_costs.append((hour_dt, cost_per_hour))

        result.new_cost += cost
        result.cycles_imported += 1
        final_effective_end = effective_end

        stored = start_utc.strftime(STORAGE_TIME_FMT)
        if not result.newest_time or stored > result.newest_time:
            result.newest_time = stored

    # Store where the DB chain now ends so the next import can fill any gap.
    if final_effective_end is not None:
        result.last_effective_end = final_effective_end.strftime(STORAGE_TIME_FMT)

    _LOGGER.info(
        "[%s] Billing CSV %s: %d cycles imported ($%.2f total, %d hourly records), "
        "%d skipped. DB chain ends at: %s",
        path.name,
        service_type,
        result.cycles_imported,
        result.new_cost,
        len(result.hourly_costs),
        result.cycles_skipped,
        result.last_effective_end or "n/a",
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

    Args:
        start_dt: Effective cycle start (aware UTC datetime).
        end_dt: Effective cycle end, exclusive (aware UTC datetime).

    Returns:
        List of hour-aligned UTC datetimes.
    """
    current = start_dt.replace(minute=0, second=0, microsecond=0)

    hours: list[datetime] = []
    while current < end_dt:
        hours.append(current)
        current = current + timedelta(hours=1)

    return hours
