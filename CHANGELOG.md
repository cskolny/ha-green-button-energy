# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-03-12

### Fixed
- **Negative consumption values on overlap imports** — the overlap baseline
  query window (`statistics_during_period`) used `window_end = earliest_dt +
  1 second`, which was too narrow. Because `statistics_during_period` uses
  inclusive boundaries and HA stores hourly stats on the hour, a 1-second
  window could return no results. When the `before` list came back empty,
  `running_sum` silently reset to 0, causing all newly-written cumulative sums
  to be far below the values already in the database. The Energy Dashboard
  interpreted the difference as a large negative consumption value at the day
  boundary where old and new data met.

  Fix: widened `window_end` to `earliest_dt + timedelta(hours=1)`. The
  existing `r_dt < earliest_dt` filter still ensures the stat at `earliest_dt`
  itself is never used as the baseline — only the stat strictly before it.

## [1.1.0] - 2026-03-10

### Added
- **10 MB file size limit** enforced in both the browser (before reading) and the
  WebSocket handler (before writing to disk or parsing). Files exceeding the limit
  are rejected immediately with a clear error message showing the actual file size.
  Green Button exports for a full year of hourly data are typically well under 2 MB.
- **Live data protection (`db_boundary` clipping)** — before writing any stats,
  `_import_statistics` now queries the last existing stat in the recorder database.
  Any incoming rows at or after that timestamp (`db_boundary`) are clipped rather
  than written. This prevents historical imports from overwriting live sensor stats
  with incorrectly calculated cumulative sums, which caused large negative
  consumption values in the Energy Dashboard.
- **Rows clipped** field in the success notification — shown only when > 0 so
  normal imports remain uncluttered. Also logged at INFO level with the boundary
  timestamp for debugging.
- **`_OVERLAP_LOOKBACK` constant** (25 hours) for the pre-overlap baseline query
  window, sized to cover DST transitions where a 1-hour gap can appear in stats.
- **HA version compatibility** for `get_last_statistics` return values — handles
  both the `datetime` objects returned by HA 2026.x and the epoch floats returned
  by older versions.

### Fixed
- **Negative consumption values in Energy Dashboard** — root cause was a historical
  import overwriting live sensor stats with incorrectly calculated cumulative sums.
  The `db_boundary` clipping fix above prevents this for all future imports.
- **Running total and newest_time inflated when rows were clipped** — the stored
  running total and `last_time` now reflect only the rows actually written to the
  database, not the full extent of the parsed file. Previously, clipped rows were
  still counted toward the running total, causing the sensor state and storage to
  drift from the actual database contents.
- **Negative and zero usage rows now skipped** in both CSV and XML parsers. Utility
  correction rows with negative or zero values previously corrupted the cumulative
  sum stored in the sensor state.
- **XML uom inference fallback** — gas XML files where `ReadingType` is absent or
  has no `uom` element now correctly infer the unit from `service_type` (therms)
  rather than defaulting to the electric conversion (Wh ÷ 1000), which produced
  values approximately 1000× too small.
- **`from parser import _STORAGE_FMT`** — was accidentally importing Python's
  standard library `parser` module instead of the integration's `.parser` module,
  which would have caused an `ImportError` at startup. Fixed to `from .parser import`.
- **`UNIT_CLASS_MAP` import** — was referenced in sensor.py but never defined in
  `const.py`, causing an `ImportError` at startup. Replaced with an inline
  expression: `"energy" if unit == UNIT_ELECTRIC else "volume"`.
- **`StatisticData` access** — log lines were using `statistic_data[0]["sum"]`
  (dict-style). In some HA versions `StatisticData` is a TypedDict and this is
  correct; in others it is a dataclass requiring `.sum`. Resolved by keeping
  dict-style access consistent with the actual HA version in use (confirmed at
  runtime to be TypedDict-backed).
- **Clipping operator** — overlap detection was using `dt <= db_boundary` (inclusive),
  meaning the most recent live stat itself was included in the import and overwritten.
  Fixed to strict `dt < db_boundary` so the live boundary stat is never touched.
- **`last_result` reset race** — `self.last_result = None` is now set before
  acquiring the processing lock, so callers never see stale results from a prior
  import if the current import raises before completing.

### Changed
- `_import_statistics` now returns `tuple[int, float, str]`
  (rows_written, written_usage, newest_written_time) instead of `None`, so the
  caller can store only what was actually committed to the database.
- `get_last_statistics`, `statistics_during_period`, and `StatisticMeanType`
  moved from lazy local imports inside `_import_statistics` to top-level imports.
- Success notification now reports **rows written** (rows actually committed to the
  long-term statistics database) and **usage of written rows only**, not the full
  parsed set. Clipped row count shown when > 0.
- `STORAGE_TIME_FMT` promoted to a public constant in `parser.py`; private alias
  `_STORAGE_FMT` retained for backward compatibility.
- Dead constants `CONF_ELECTRIC_KEYWORD` and `CONF_GAS_KEYWORD` removed from
  `const.py` — they were defined but never used anywhere.
- `_ensure_frontend_file` now always copies the panel JS on startup (unconditional
  `shutil.copy2`) rather than checking `mtime`, ensuring the correct JS version is
  always served after an update.

## [1.0.0] - 2026-03-01

### Added
- Initial release.
- Drag-and-drop sidebar panel for importing Avangrid Green Button CSV and XML files.
  No `configuration.yaml` changes required — the panel registers itself automatically.
- Electric (kWh) and gas (CCF/therms) sensor entities with correct `device_class`,
  `state_class`, and units for the HA Energy Dashboard.
- Full historical backfill via `recorder.async_import_statistics` — hourly readings
  are written directly into HA's long-term statistics database with correct past
  timestamps, enabling months of history to appear in the Energy Dashboard
  immediately after the first import.
- Duplicate prevention — last imported timestamp stored in HA `.storage`;
  already-imported rows are automatically skipped on subsequent imports.
- Safe re-import of overlapping date ranges — files covering dates already in the
  database are handled without double-counting.
- Support for RG&E, NYSEG, Central Maine Power, United Illuminating,
  Connecticut Natural Gas, Southern Connecticut Gas, and Berkshire Gas.
- CSV parser for Avangrid Opower export format with case-insensitive column
  matching and timezone-aware timestamp handling.
- ESPI XML parser with auto-detection of service type, unit (Wh vs therms), and
  conversion factor from `ReadingType` metadata.
- Three-case cumulative sum baseline logic: clean append, overlap/backfill,
  and first import (baseline = 0).
- Persistent notifications on import success and failure with row counts,
  usage totals, and newest imported timestamp.
- WebSocket backend handler — file content is sent as UTF-8 text over the existing
  HA WebSocket connection; no extra authentication or filesystem access required
  from the browser.
- `unit_class` metadata (`"energy"` for kWh, `"volume"` for CCF) required by
  HA 2026.x recorder statistics API.