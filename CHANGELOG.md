# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.2.0] — 2026-03-10

### Fixed
- **10 MB file size limit not enforced** — The limit was documented in the README but never implemented. Files of any size were accepted, written to disk, and fully parsed. The limit is now enforced in both the WebSocket handler (`__init__.py`) and the browser panel (client-side, before `FileReader` is called), so oversized files are rejected with a clear message before any processing occurs.
- **XML files with missing `ReadingType` element defaulted to electric** — When `uom` was absent from the XML, the parser silently applied the electric (Wh ÷ 1000) conversion to all service types. For gas files this produced values approximately 1000× too small with no warning. The parser now infers `uom` from the `service_type` argument when `ReadingType` is missing.
- **Negative and zero usage rows accepted** — Utility correction rows with negative or zero values were imported and subtracted from the cumulative sum, corrupting the running total and potentially causing negative consumption in the Energy Dashboard. Both the CSV and XML parsers now skip any row where `usage <= 0`.
- **WebSocket disconnect produced an unhelpful error message** — A dropped connection during import showed `"Connection error: undefined"`. The panel now checks connection state before sending and surfaces a descriptive message telling the user to refresh the page.

### Changed
- `_STORAGE_FMT` promoted to `STORAGE_TIME_FMT` as a public constant in `parser.py`. The private `_STORAGE_FMT` alias is retained for backward compatibility.
- Removed unused constants `CONF_ELECTRIC_KEYWORD` and `CONF_GAS_KEYWORD` from `const.py`.
- `FileReader` error handler now returns a descriptive message instead of a generic failure string.
- Storage schema documented in `storage.py` with migration guidance.

---

## [1.1.0] — 2026-03-10

### Fixed
- **Negative consumption values in the Energy Dashboard** — Importing a historical file on a day when Home Assistant had already recorded live sensor stats overwrote those stats with incorrectly calculated cumulative sums. This caused the Energy Dashboard to display large negative consumption values (e.g. −47 kWh) in the hour following the import. The integration now clips any import rows at or beyond the last existing stat in the database (`db_boundary`), so live sensor data is never touched. If rows are clipped, the count is shown in the success notification as "⚠️ Rows clipped (live data protected): N".

### Changed
- `_import_statistics` now returns `(rows_written, written_usage, newest_written)` so the stored running total and success notification reflect only what was actually written to the database, not the full parsed set (which could include clipped rows).
- The stored `last_time` is now set to the newest timestamp actually written to the DB rather than the newest timestamp parsed from the file.
- Top-level imports: `get_last_statistics`, `statistics_during_period`, and `StatisticMeanType` moved from inline (inside `_import_statistics`) to module-level imports in `sensor.py`.
- `_STORAGE_FMT` imported from `.parser` in `sensor.py` for consistent timestamp formatting.
- Success notification now shows **Rows written** (rows committed to DB) instead of **Rows imported** (rows parsed from file), and includes a clipped-row count when > 0.

---

## [1.0.0] — 2026-03-09

### Added
- Initial release.
- Drag-and-drop sidebar panel for importing Avangrid Green Button CSV and XML exports into the Home Assistant Energy Dashboard.
- Support for **electric** (kWh) and **gas** (CCF/therms) usage data.
- Full historical backfill via `recorder.async_import_statistics` — all hourly readings are written with correct past timestamps.
- Duplicate row prevention — the newest imported timestamp is stored in `.storage` and all previously-imported rows are skipped on re-import.
- Support for both **CSV** (Avangrid Opower export) and **ESPI XML** (Green Button standard) file formats.
- Auto-detection of service type and unit conversion from XML `ReadingType` metadata (`uom` + `powerOfTenMultiplier`).
- Persistent HA notifications on import success and failure, showing row counts and usage totals.
- Fully automatic sidebar panel registration — no `configuration.yaml` changes required.
- One-click integration setup via HA config flow (Settings → Devices & Services).
- Sensors created: `sensor.avangrid_electric_total` (kWh, `energy`) and `sensor.avangrid_gas_total` (CCF, `gas`), both with `state_class: total_increasing`.
- Supported utilities: RG&E, NYSEG, Central Maine Power, United Illuminating, Connecticut Natural Gas, Southern Connecticut Gas, Berkshire Gas.

---

[1.2.0]: https://github.com/cskolny/ha-green-button-energy/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/cskolny/ha-green-button-energy/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cskolny/ha-green-button-energy/releases/tag/v1.0.0
