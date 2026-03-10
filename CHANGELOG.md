# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-03-10

### Added
- **10 MB file size limit** enforced in the WebSocket handler before any disk
  write or parsing. Returns a clear error message if exceeded. Green Button
  exports for a full year of hourly data are typically well under 2 MB.
- **Live data protection (`db_boundary` clipping)** — when importing a file
  whose date range overlaps with hours already recorded by the live HA sensor,
  rows beyond the last existing stat in the database are now clipped rather than
  overwritten. This prevents the incorrect cumulative sums that caused negative
  consumption values in the Energy Dashboard.
- **Rows clipped** field in the success notification — shown only when > 0, so
  normal imports remain uncluttered.

### Fixed
- **Negative consumption values in Energy Dashboard** — root cause was a
  historical import overwriting live sensor stats with incorrectly calculated
  cumulative sums. The `db_boundary` clipping fix above prevents this entirely
  for all future imports.
- **Negative and zero usage rows now skipped** in both CSV and XML parsers.
  Utility correction rows (negative values) previously corrupted the cumulative
  sum stored in the sensor state.
- **XML uom inference fallback** — gas XML files where `ReadingType` is missing
  or has no `uom` element now correctly infer the unit from `service_type`
  rather than defaulting to the electric (Wh ÷ 1000) conversion, which produced
  values ~1000× too small.

### Changed
- Success notification now reports **rows written** (rows actually committed to
  the long-term statistics database) rather than rows parsed from the file, so
  the count is accurate when clipping occurs.
- Stored `last_time` and running total now reflect only the rows actually
  written to the DB, not the full extent of the imported file.

## [1.0.0] - 2026-03-01

### Added
- Initial release.
- Drag-and-drop sidebar panel for importing Avangrid Green Button CSV and XML files.
- Electric (kWh) and gas (CCF/therms) sensor entities with correct `device_class`,
  `state_class`, and units for the HA Energy Dashboard.
- Full historical backfill via `recorder.async_import_statistics` — hourly
  readings are written with correct past timestamps.
- Duplicate prevention — last imported timestamp stored in HA `.storage`;
  already-imported rows are skipped on subsequent imports.
- Support for RG&E, NYSEG, Central Maine Power, United Illuminating,
  Connecticut Natural Gas, Southern Connecticut Gas, and Berkshire Gas.
- CSV parser for Avangrid Opower export format.
- ESPI XML parser with auto-detection of service type, unit, and conversion factor.
- Persistent notifications on import success and failure with row counts and
  usage totals.
- WebSocket backend handler — no filesystem access required from the browser.