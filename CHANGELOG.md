# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.7.0] - 2026-03-28

### Added
- **Monthly billing import** — a new 🧾 Electric Billing and 🧾 Gas Billing drop
  zone in the sidebar panel accepts the monthly billing CSV export from Avangrid
  utilities (the same file format as the hourly usage CSV, differentiated by
  which drop zone it is placed in). Each billing cycle's total dollar cost is
  spread evenly across all hours in the cycle and written into HA's long-term
  statistics database, enabling the Energy Dashboard to display actual billed
  costs instead of a static per-kWh price estimate.

- **Two new sensor entities** created automatically on setup:

  | Sensor | Entity ID | Unit | Device Class | State Class |
  |--------|-----------|------|-------------|-------------|
  | Avangrid Electric Cost | `sensor.avangrid_electric_cost` | USD | `monetary` | `total` |
  | Avangrid Gas Cost | `sensor.avangrid_gas_cost` | USD | `monetary` | `total` |

  After importing billing data, go to **Settings → Energy → Electricity grid →
  Edit** and switch from "Use a static price" to "Use an entity tracking the
  total cost" → select **Avangrid Electric Cost**. Repeat for gas.

- **New WebSocket command** `green_button_energy/import_billing` handles billing
  file uploads independently of the existing `green_button_energy/import_file`
  usage command.

- **`billing_parser.py`** — new module containing `BillingParseResult` dataclass
  and the full billing CSV parse pipeline (pass 1: collect and validate cycles;
  pass 2: sort, gap-fill, and enumerate hourly cost records).

- **`_parse_billing_timestamp()`** — interprets naive Avangrid CSV timestamps as
  America/New_York local midnight before converting to UTC, ensuring billing
  cycle boundaries align with Eastern local calendar days in the Energy Dashboard.

- **Six new storage keys** in `const.py`:
  `electric_cost_total`, `gas_cost_total`, `last_electric_cost_time`,
  `last_gas_cost_time`, `last_electric_cost_effective_end`,
  `last_gas_cost_effective_end`.

### Fixed
- **Intra-file gap day ($0.00 between consecutive billing cycles)** — Avangrid
  billing CSVs have a one-day gap between consecutive cycles (cycle N ends on
  day X, cycle N+1 starts on day X+2). Without gap filling, the gap day had zero
  cost records and the Energy Dashboard showed $0.00 or an interpolated partial
  amount.

  Fix: the billing parser uses a two-pass approach. Pass 1 collects all valid
  cycles. Pass 2 sorts them and extends each cycle's effective end to the next
  cycle's start before enumerating hours, ensuring every calendar day has exactly
  one cost record.

- **Inconsistent daily costs caused by UTC vs Eastern timezone mismatch** —
  naive billing CSV timestamps treated as UTC placed cycle boundaries at
  19:00–20:00 Eastern rather than local midnight. The HA Energy Dashboard groups
  statistics by local calendar day, so a boundary mid-evening caused the last
  4–5 hours of one local day and the first 19–20 hours of the next to straddle
  two different billing cycles, producing blended daily totals that differed from
  every surrounding day.

  Fix: `_parse_billing_timestamp()` treats naive timestamps as
  `America/New_York` local midnight and converts to UTC. Cycle boundaries now
  land at local midnight, so every local calendar day is fully covered by a
  single cycle's hourly rate. DST transition days (spring-forward = 23 hours,
  fall-back = 25 hours) show proportionally adjusted costs, which is
  mathematically correct and intentional.

- **Inter-import gap day ($0.00 on the day after the previous import's last
  cycle)** — when importing a single new billing cycle as a monthly update, the
  last day of the previous import's final billing period was left with no cost
  records. Example: previous import wrote hours through 2026-02-24 05:00 UTC
  (Eastern midnight of 2/24); the new 2026-02-25 cycle started at
  2026-02-25 05:00 UTC, leaving all 24 hours of local 2/24 showing $0.00.

  Fix: `parse_billing_file` now accepts a `last_effective_end` parameter — the
  UTC timestamp where the previous import's DB chain actually ended. If the first
  new cycle's start is after `last_effective_end`, that cycle's effective start
  is moved back to cover the gap. This value is persisted in `.storage` after
  each import under `last_electric_cost_effective_end` /
  `last_gas_cost_effective_end` and used automatically on every subsequent import.

- **`state_class` validation error for cost sensors** — HA 2026.x rejects
  `TOTAL_INCREASING` for `monetary` device class sensors. Fixed by using
  `SensorStateClass.TOTAL` on `GreenButtonCostSensor`.

- **`unit_class="monetary"` recorder error** — HA's recorder statistics API does
  not accept `"monetary"` as a `unit_class` value. Fixed by passing
  `unit_class=None` in `StatisticMetaData` for cost sensors, allowing HA to
  infer the class from `unit_of_measurement="USD"`.

## [1.6.0] - 2026-03-15

### Changed
- Sidebar panel updated with billing import section, info box with Energy
  Dashboard cost configuration instructions, and billing-specific result cards
  in the import history log.
- README updated with billing sensors table, Configuring Billing Costs section,
  updated architecture diagram, and expanded Resetting / Starting Fresh and
  Troubleshooting sections.

## [1.5.0] - 2026-03-15

### Fixed
- **Negative consumption spike appearing ~30–60 minutes after a fresh import**
  — the last remaining pathway for HA's recorder to write a poisoning stat.

  `async_add_entities(..., update_before_add=True)` causes HA's entity platform
  to call `async_update()` on each sensor at startup, and then automatically
  call `async_write_ha_state()` after it returns — even though the integration
  never calls `async_write_ha_state()` explicitly. That state write is observed
  by HA's recorder, which writes a stat for the entity at the **current hour's
  timestamp** with `sum = stored_total`. On a fresh install (after clearing all
  data), `stored_total = 0`, so a stat with `sum = 0` is written at today's
  hour. The import then correctly writes thousands of historical rows with sums
  climbing to ~5500 kWh. At the end of the clock hour HA commits the aggregate,
  leaving `sum = 0` sitting in the DB at today's hour. The Energy Dashboard
  computes `0 - 5500 = -5500 kWh` at that hour — a massive negative spike
  that appears 30–60 minutes after a successful import.

  Fix: removed `update_before_add=True` from `async_add_entities()`. Also
  removed the `async_update()` method entirely — it was redundant since
  `__init__` already sets `_attr_native_value` from stored data, and keeping
  it was misleading given that `_attr_should_poll = False` means HA never
  calls it after startup anyway.

  This eliminates all remaining pathways for HA's recorder to write a live
  stat for this entity.

## [1.4.0] - 2026-03-15

### Fixed
- **Every import after the first silently wrote 0 rows** — the root cause of
  the recurring "data stops at the previous file's last date" problem.

  After each successful import, `async_write_ha_state()` was called to update
  the sensor's state in HA. Even though this integration has no live sensor,
  HA's recorder observes every state change on a `TOTAL_INCREASING` entity and
  writes its own stat at the **current hour's timestamp**. On the next import,
  `get_last_statistics` returned that recorder-written stat timestamped *today*,
  not the last row our import actually wrote. The import code then discarded
  every row in the new file as "already in the DB chain" — because all
  historical rows predate today. Result: 0 rows written, no persistent
  notification, no storage update, and re-importing the same file repeatedly
  reported "N rows" without ever writing anything.

  Fix: removed `async_write_ha_state()` entirely.

### Fixed
- **Import panel falsely reported parser row count instead of rows written** —
  fixed to report `rows_written` (rows actually committed to the DB).

## [1.3.0] - 2026-03-13

### Fixed
- **Negative consumption values — definitive root cause fix** in `_import_statistics`.

  The Energy Dashboard computes hourly consumption as `sum[N] - sum[N-1]`. A
  negative value means the cumulative sum decreased between two consecutive
  hours. This always happens when those two hours were written by different
  import chains that used different baselines. Fix: `_import_statistics` now
  calls `get_last_statistics` to find the current end of the DB chain and
  discards any incoming row whose timestamp is ≤ `last_stat_dt`, then writes
  only rows with timestamp > `last_stat_dt` appending from that sum as baseline.

## [1.2.0] - 2026-03-12

### Fixed
- **Negative consumption values on overlap imports** — the overlap baseline
  query window was too narrow. Fixed by widening `window_end` to
  `earliest_dt + timedelta(hours=1)`.

## [1.1.0] - 2026-03-10

### Added
- **10 MB file size limit** enforced in both the browser and the WebSocket handler.
- **Live data protection (`db_boundary` clipping)** — incoming rows at or after
  the last existing stat timestamp are clipped rather than written.
- **Rows clipped** field in the success notification.
- **HA version compatibility** for `get_last_statistics` return values.

### Fixed
- Negative consumption values in Energy Dashboard.
- Running total and newest_time inflated when rows were clipped.
- Negative and zero usage rows now skipped in both CSV and XML parsers.
- XML uom inference fallback for gas files where `ReadingType` is absent.
- Various import errors corrected (`from .parser import`, `UNIT_CLASS_MAP`,
  `StatisticData` access, clipping operator, `last_result` reset race).

### Changed
- `_import_statistics` now returns `tuple[int, float, str]`.
- Success notification reports rows written and usage of written rows only.
- `_ensure_frontend_file` now always copies the panel JS on startup.

## [1.0.0] - 2026-03-01

### Added
- Initial release.
- Drag-and-drop sidebar panel for importing Avangrid Green Button CSV and XML files.
- Electric (kWh) and gas (CCF/therms) sensor entities.
- Full historical backfill via `recorder.async_import_statistics`.
- Duplicate prevention via `.storage` last-imported timestamp.
- Safe re-import of overlapping date ranges.
- Support for RG&E, NYSEG, Central Maine Power, United Illuminating,
  Connecticut Natural Gas, Southern Connecticut Gas, and Berkshire Gas.
- CSV parser for Avangrid Opower export format.
- ESPI XML parser with auto-detection of service type and unit conversion.
- Persistent notifications on import success and failure.
- WebSocket backend handler.
- `unit_class` metadata required by HA 2026.x recorder statistics API.
