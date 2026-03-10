"""
Sensor platform for the Green Button Energy Import integration.

After parsing a file, hourly readings are written directly into HA's
long-term statistics database using recorder.async_import_statistics.
This is the only way to get historical data into the Energy Dashboard
with correct past timestamps — simply updating sensor state only
records a single point at the current time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    ELECTRIC_SENSOR_KEY,
    ELECTRIC_TIME_KEY,
    GAS_SENSOR_KEY,
    GAS_TIME_KEY,
    LAST_FILE_KEY,
    NOTIF_ERROR,
    NOTIF_SUCCESS,
    SENSOR_ELECTRIC_NAME,
    SENSOR_ELECTRIC_UID,
    SENSOR_GAS_NAME,
    SENSOR_GAS_UID,
    UNIT_ELECTRIC,
    UNIT_GAS,
)
# FIX #2: must be ".parser" (relative import), not "parser" (Python stdlib)
# FIX #1: UNIT_CLASS_MAP is NOT in const.py — unit_class is inlined below instead
from .parser import ParseResult, _STORAGE_FMT, parse_file
from .storage import load_store

_LOGGER = logging.getLogger(__name__)

# How far before the earliest new row to look for a pre-overlap baseline.
# 25 hours covers DST transitions where a 1-hour gap can appear in stats.
_OVERLAP_LOOKBACK = timedelta(hours=25)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Green Button Energy sensors from a config entry."""

    store, data = await load_store(hass)

    electric_sensor = GreenButtonSensor(
        hass=hass,
        store=store,
        data=data,
        service_type="electric",
        total_key=ELECTRIC_SENSOR_KEY,
        time_key=ELECTRIC_TIME_KEY,
        unit=UNIT_ELECTRIC,
        device_class=SensorDeviceClass.ENERGY,
        name=SENSOR_ELECTRIC_NAME,
        unique_id=SENSOR_ELECTRIC_UID,
    )

    gas_sensor = GreenButtonSensor(
        hass=hass,
        store=store,
        data=data,
        service_type="gas",
        total_key=GAS_SENSOR_KEY,
        time_key=GAS_TIME_KEY,
        unit=UNIT_GAS,
        device_class=SensorDeviceClass.GAS,
        name=SENSOR_GAS_NAME,
        unique_id=SENSOR_GAS_UID,
    )

    async_add_entities([electric_sensor, gas_sensor], update_before_add=True)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "electric": electric_sensor,
        "gas": gas_sensor,
    }


class GreenButtonSensor(SensorEntity):
    """
    Cumulative energy/gas sensor that writes historical statistics directly
    into HA's recorder database for Energy Dashboard backfill support.
    """

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = False

    def __init__(
        self,
        hass: HomeAssistant,
        store: Store,
        data: dict[str, Any],
        service_type: str,
        total_key: str,
        time_key: str,
        unit: str,
        device_class: SensorDeviceClass,
        name: str,
        unique_id: str,
    ) -> None:
        self.hass = hass
        self._store = store
        self._data = data
        self._service_type = service_type
        self._total_key = total_key
        self._time_key = time_key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_value: float = float(data.get(total_key, 0.0))
        self._processing_lock = asyncio.Lock()
        self.last_result: Optional[ParseResult] = None

    async def async_update(self) -> None:
        """Refresh state from stored data (called once at startup)."""
        self._attr_native_value = float(self._data.get(self._total_key, 0.0))

    async def async_process_file(self, file_path: str) -> None:
        """
        Parse a file and write hourly statistics into HA's recorder database.

        Uses async_import_statistics so every hourly reading gets its correct
        past timestamp in the Energy Dashboard.
        """
        # Reset last_result so callers never see stale data from a prior import
        # if this call raises before completing.
        self.last_result = None

        async with self._processing_lock:
            last_time: str = self._data.get(self._time_key, "")

            _LOGGER.debug(
                "[%s] Processing '%s' (last_time='%s')",
                self._attr_name, file_path, last_time or "none",
            )

            result: ParseResult = await self.hass.async_add_executor_job(
                parse_file, file_path, self._service_type, last_time
            )

            self.last_result = result

            if result.errors:
                for err in result.errors:
                    _LOGGER.error("[%s] Parse error: %s", self._attr_name, err)
                self._send_error_notification(file_path, result)
                return

            if not result.has_new_data:
                _LOGGER.info(
                    "[%s] '%s' — no new rows (already up to date).",
                    self._attr_name, Path(file_path).name,
                )
                return

            rows_written, written_usage, newest_written = await self._import_statistics(result)

            if rows_written == 0:
                # All rows were clipped — nothing new was written to the DB.
                # Do not advance last_time or update the running total.
                return

            # FIX #6: use written_usage (usage of surviving rows only), not
            # result.new_usage, which includes any clipped rows and would
            # inflate the stored running total.
            self._data[self._total_key] = round(
                float(self._data.get(self._total_key, 0.0)) + written_usage, 6
            )
            # Use the actual newest timestamp written to the DB, not the
            # parser's newest_time, which may include rows that were clipped.
            self._data[self._time_key] = newest_written
            self._data[LAST_FILE_KEY] = Path(file_path).name
            await self._store.async_save(self._data)

            self._attr_native_value = self._data[self._total_key]
            self.async_write_ha_state()

            rows_clipped = result.rows_imported - rows_written
            _LOGGER.info(
                "[%s] Imported %.4f %s (%d rows written, %d clipped) from '%s'. Total: %.4f.",
                self._attr_name,
                written_usage,
                self._attr_native_unit_of_measurement,
                rows_written,
                rows_clipped,
                Path(file_path).name,
                self._attr_native_value,
            )

            self._send_success_notification(file_path, written_usage, rows_written, rows_clipped, newest_written)

    async def _import_statistics(self, result: ParseResult) -> tuple[int, float, str]:
        """
        Write hourly readings as long-term statistics into the recorder.

        Finds the correct cumulative sum baseline by looking up the last
        recorded stat strictly BEFORE our earliest new row. Handles three
        cases:
          1. Clean append   — new data starts after all existing stats
          2. Overlap        — find pre-overlap sum so existing rows are
                             overwritten with correct cumulative sums,
                             but rows AT OR BEYOND the existing last stat
                             are clipped to avoid overwriting live sensor stats
          3. First import   — no existing stats; baseline = 0

        Returns:
            (rows_written, written_usage, newest_written_time)
        """
        if not result.hourly_readings:
            _LOGGER.warning("[%s] No hourly readings to import.", self._attr_name)
            return 0, 0.0, result.newest_time

        sorted_readings = sorted(result.hourly_readings, key=lambda x: x[0])
        earliest_dt = sorted_readings[0][0]

        # Get the overall last stat to detect clean-append vs overlap
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.entity_id,
            True,
            {"sum"},
        )

        running_sum = 0.0
        # db_boundary: timestamp of the last stat already in the DB.
        # We must NOT write rows at or after this point — doing so overwrites
        # live-sensor stats with import-calculated sums, breaking the
        # cumulative chain and producing negative deltas in the Energy Dashboard.
        db_boundary: Optional[datetime] = None

        if last_stats and self.entity_id in last_stats:
            last = last_stats[self.entity_id][0]

            # HA 2026.x returns start as a datetime; older versions return an
            # epoch float. Handle both to stay compatible across HA versions.
            start_val = last["start"]
            if isinstance(start_val, datetime):
                last_start = start_val if start_val.tzinfo else start_val.replace(tzinfo=timezone.utc)
            else:
                last_start = datetime.fromtimestamp(float(start_val), tz=timezone.utc)

            if last_start < earliest_dt:
                # Clean append — new data is entirely after existing stats.
                # Use the last existing sum as the baseline.
                running_sum = float(last.get("sum") or 0.0)
                _LOGGER.debug(
                    "[%s] Clean append — baseline sum=%.4f from %s",
                    self._attr_name, running_sum, last_start,
                )
            else:
                # Overlap — the file starts before (or at) the last existing
                # stat. Record db_boundary so we can clip rows that go beyond
                # it, then find the sum from just before our earliest row to
                # use as the cumulative baseline.
                db_boundary = last_start

                window_start = earliest_dt - _OVERLAP_LOOKBACK
                window_end   = earliest_dt + timedelta(seconds=1)

                pre_stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    window_start,
                    window_end,
                    {self.entity_id},
                    "hour",
                    None,
                    {"sum"},
                )

                if pre_stats and self.entity_id in pre_stats:
                    pre_list = pre_stats[self.entity_id]
                    before = []
                    for r in pre_list:
                        r_start = r["start"]
                        if isinstance(r_start, datetime):
                            r_dt = r_start if r_start.tzinfo else r_start.replace(tzinfo=timezone.utc)
                        else:
                            r_dt = datetime.fromtimestamp(float(r_start), tz=timezone.utc)
                        if r_dt < earliest_dt:
                            before.append((r_dt, r))

                    if before:
                        running_sum = float(before[-1][1].get("sum") or 0.0)
                        _LOGGER.debug(
                            "[%s] Overlap — pre-overlap baseline sum=%.4f",
                            self._attr_name, running_sum,
                        )
                    else:
                        running_sum = 0.0
                        _LOGGER.debug("[%s] Overlap — no pre-overlap stat, using 0", self._attr_name)
                else:
                    running_sum = 0.0
                    _LOGGER.debug("[%s] Overlap — no stats in window, using 0", self._attr_name)

        # FIX #4: use strict less-than (<) so the db_boundary stat itself is
        # excluded from the import. The db_boundary row IS the last live stat;
        # including it (<=) would overwrite it with our calculated sum, which
        # is the exact failure mode we're guarding against.
        if db_boundary is not None:
            original_count = len(sorted_readings)
            sorted_readings = [(dt, u) for dt, u in sorted_readings if dt < db_boundary]
            clipped = original_count - len(sorted_readings)
            if clipped:
                _LOGGER.debug(
                    "[%s] Clipped %d row(s) at/beyond db_boundary %s to avoid "
                    "overwriting live stats.",
                    self._attr_name, clipped, db_boundary,
                )

        if not sorted_readings:
            _LOGGER.info(
                "[%s] No rows remain after clipping to db_boundary — "
                "all data already present in the database.",
                self._attr_name,
            )
            return 0, 0.0, result.newest_time

        # Build StatisticData rows with correct running sums.
        statistic_data: list[StatisticData] = []
        for dt_utc, usage in sorted_readings:
            running_sum = round(running_sum + usage, 6)
            statistic_data.append(
                StatisticData(
                    start=dt_utc,
                    state=round(usage, 6),
                    sum=running_sum,
                )
            )

        # FIX #1: UNIT_CLASS_MAP was referenced but never added to const.py.
        # Inline the unit-class mapping here instead.
        unit_class = "energy" if self._attr_native_unit_of_measurement == UNIT_ELECTRIC else "volume"

        metadata = StatisticMetaData(
            has_mean=False,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._attr_name,
            source="recorder",
            statistic_id=self.entity_id,
            unit_class=unit_class,
            unit_of_measurement=self._attr_native_unit_of_measurement,
        )

        first_sum = statistic_data[0]["sum"]
        last_sum  = statistic_data[-1]["sum"]
        _LOGGER.info(
            "[%s] Writing %d statistics (sum %.4f → %.4f %s).",
            self._attr_name, len(statistic_data), first_sum, last_sum,
            self._attr_native_unit_of_measurement,
        )

        async_import_statistics(self.hass, metadata, statistic_data)

        # Return actual written count, usage of written rows only, and newest timestamp.
        written_usage = sum(u for _, u in sorted_readings)
        newest_written = sorted_readings[-1][0].strftime(_STORAGE_FMT)
        return len(statistic_data), written_usage, newest_written

    def _send_success_notification(
        self,
        file_path: str,
        written_usage: float,
        rows_written: int,
        rows_clipped: int,
        newest_written: str,
    ) -> None:
        """Show a persistent notification on successful import."""
        clipped_note = (
            f"\n⚠️ Rows clipped (live data protected): {rows_clipped}"
            if rows_clipped > 0
            else ""
        )
        pn_create(
            self.hass,
            message=(
                f"**{self._attr_name}**\n\n"
                f"📄 File: `{Path(file_path).name}`\n"
                f"✅ Rows written: {rows_written}\n"
                f"📊 New usage: {written_usage:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🔢 Running total: {self._attr_native_value:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🕐 Data through: {newest_written}"
                f"{clipped_note}"
            ),
            title="Avangrid Green Button — Import Successful",
            notification_id=f"{NOTIF_SUCCESS}_{self._attr_unique_id}",
        )

    def _send_error_notification(self, file_path: str, result: ParseResult) -> None:
        """Show a persistent notification on parse failure."""
        errors_fmt = "\n".join(f"- {e}" for e in result.errors)
        pn_create(
            self.hass,
            message=(
                f"**{self._attr_name}**\n\n"
                f"📄 File: `{Path(file_path).name}`\n"
                f"❌ Import failed.\n\n"
                f"**Errors:**\n{errors_fmt}"
            ),
            title="Avangrid Green Button — Import Failed",
            notification_id=f"{NOTIF_ERROR}_{self._attr_unique_id}",
        )