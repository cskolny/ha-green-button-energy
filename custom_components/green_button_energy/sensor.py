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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from .parser import ParseResult, _STORAGE_FMT, parse_file
from .storage import load_store

_LOGGER = logging.getLogger(__name__)


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
        self.last_result: ParseResult | None = None

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
                # All rows were filtered out — nothing new was written to the DB.
                # Do not advance last_time or update the running total.
                return

            self._data[self._total_key] = round(
                float(self._data.get(self._total_key, 0.0)) + written_usage, 6
            )
            self._data[self._time_key] = newest_written
            self._data[LAST_FILE_KEY] = Path(file_path).name
            await self._store.async_save(self._data)

            self._attr_native_value = self._data[self._total_key]
            self.async_write_ha_state()

            rows_skipped = result.rows_imported - rows_written
            _LOGGER.info(
                "[%s] Imported %.4f %s (%d rows written, %d skipped) from '%s'. Total: %.4f.",
                self._attr_name,
                written_usage,
                self._attr_native_unit_of_measurement,
                rows_written,
                rows_skipped,
                Path(file_path).name,
                self._attr_native_value,
            )

            self._send_success_notification(
                file_path, written_usage, rows_written, rows_skipped, newest_written
            )

    async def _import_statistics(self, result: ParseResult) -> tuple[int, float, str]:
        """
        Write hourly readings as long-term statistics into the recorder.

        ── The invariant that prevents negative consumption values ─────────────

        The Energy Dashboard computes hourly consumption as sum[N] - sum[N-1].
        A negative value means sum[N] < sum[N-1], i.e. the cumulative sum
        decreased. This happens whenever two consecutive rows in the DB were
        written by DIFFERENT import chains that used different baselines.

        The only guarantee against this is: every row we write must directly
        follow the last row already in the DB, using that row's sum as the
        baseline.  We never write a row whose timestamp already exists in the
        DB (that would overwrite it with a potentially different sum), and we
        never write a row that would be followed by an existing row whose sum
        was computed from a different baseline.

        ── How this is achieved ────────────────────────────────────────────────

        1.  The parser already skipped rows at or before stored_last_time, so
            every row in hourly_readings is genuinely new to us.

        2.  We call get_last_statistics to find the current end of the DB chain:
            its timestamp (last_stat_dt) and cumulative sum (last_stat_sum).

        3.  Any row in hourly_readings whose timestamp is ≤ last_stat_dt already
            exists in the DB — it was written by a previous import of a file
            that covered a longer range, or by a live sensor.  Writing it again
            would overwrite its sum with a value from our chain, splitting the
            existing chain and creating a seam.  We discard those rows.

        4.  We keep only rows with timestamp > last_stat_dt.  These truly follow
            the end of the DB chain.  We use last_stat_sum as our baseline and
            append from there.

        5.  If no rows survive this filter (the file contained no data newer
            than what's already in the DB), we return without writing anything.

        This reduces to two cases with unified handling:

          Normal append (no other data beyond stored_last_time):
            last_stat_dt == stored_last_time
            All hourly_readings rows have dt > stored_last_time > last_stat_dt
            → all rows kept, baseline = last_stat_sum ✓

          Other data exists beyond stored_last_time (prior longer import or live sensor):
            last_stat_dt > stored_last_time
            Rows with dt ≤ last_stat_dt are discarded (already in DB)
            Rows with dt > last_stat_dt are truly new and follow the chain end
            baseline = last_stat_sum (end of the existing chain) ✓

        Returns:
            (rows_written, written_usage, newest_written_time)
        """
        if not result.hourly_readings:
            _LOGGER.warning("[%s] No hourly readings to import.", self._attr_name)
            return 0, 0.0, result.newest_time

        sorted_readings = sorted(result.hourly_readings, key=lambda x: x[0])

        # ── Find the current end of the DB chain ──────────────────────────────
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.entity_id,
            True,
            {"sum"},
        )

        last_stat_dt: datetime | None = None
        running_sum: float = 0.0

        if last_stats and self.entity_id in last_stats:
            last = last_stats[self.entity_id][0]

            # HA 2026.x returns start as a datetime; older versions return an
            # epoch float. Handle both to stay compatible across HA versions.
            start_val = last["start"]
            if isinstance(start_val, datetime):
                last_stat_dt = (
                    start_val if start_val.tzinfo else start_val.replace(tzinfo=timezone.utc)
                )
            else:
                last_stat_dt = datetime.fromtimestamp(float(start_val), tz=timezone.utc)

            running_sum = float(last.get("sum") or 0.0)

            _LOGGER.debug(
                "[%s] DB chain ends at %s with sum=%.4f.",
                self._attr_name, last_stat_dt, running_sum,
            )
        else:
            _LOGGER.debug("[%s] First import — no existing stats, baseline sum=0.", self._attr_name)

        # ── Discard rows already covered by the DB chain ──────────────────────
        #
        # Any row whose timestamp is ≤ last_stat_dt already exists in the DB.
        # Writing it would overwrite its sum and break the chain that follows.
        if last_stat_dt is not None:
            before = len(sorted_readings)
            sorted_readings = [(dt, u) for dt, u in sorted_readings if dt > last_stat_dt]
            discarded = before - len(sorted_readings)
            if discarded:
                _LOGGER.debug(
                    "[%s] Discarded %d row(s) already present in DB chain (≤ %s).",
                    self._attr_name, discarded, last_stat_dt,
                )

        if not sorted_readings:
            _LOGGER.info(
                "[%s] No rows newer than the current DB chain end (%s) — nothing to write.",
                self._attr_name, last_stat_dt,
            )
            return 0, 0.0, result.newest_time

        # ── Append new rows to the end of the DB chain ────────────────────────
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

        unit_class = (
            "energy" if self._attr_native_unit_of_measurement == UNIT_ELECTRIC else "volume"
        )

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

        _LOGGER.info(
            "[%s] Writing %d statistics (sum %.4f → %.4f %s).",
            self._attr_name,
            len(statistic_data),
            statistic_data[0]["sum"],
            statistic_data[-1]["sum"],
            self._attr_native_unit_of_measurement,
        )

        async_import_statistics(self.hass, metadata, statistic_data)

        written_usage = sum(u for _, u in sorted_readings)
        newest_written = sorted_readings[-1][0].strftime(_STORAGE_FMT)
        return len(statistic_data), written_usage, newest_written

    def _send_success_notification(
        self,
        file_path: str,
        written_usage: float,
        rows_written: int,
        rows_skipped: int,
        newest_written: str,
    ) -> None:
        """Show a persistent notification on successful import."""
        skipped_note = (
            f"\n⚠️ Rows skipped (already in DB): {rows_skipped}"
            if rows_skipped > 0
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
                f"{skipped_note}"
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