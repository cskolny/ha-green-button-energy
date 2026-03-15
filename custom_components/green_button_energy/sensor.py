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
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
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

    # NOTE: update_before_add is intentionally omitted (defaults to False).
    # Passing True causes HA to call async_update() then async_write_ha_state()
    # at startup — even though we never call async_write_ha_state() ourselves.
    # That state write causes HA's recorder to write a stat at the current hour
    # with sum=stored_total, poisoning the DB chain and creating a negative spike
    # in the Energy Dashboard the next time historical data is imported.
    async_add_entities([electric_sensor, gas_sensor])

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
    _attr_has_entity_name = False
    # NOTE: _attr_state_class is intentionally NOT set.
    # Setting state_class=TOTAL_INCREASING causes HA's recorder hourly job to
    # write a stat for this entity at the top of every hour using the sensor's
    # current state machine value. Since we never call async_write_ha_state(),
    # that value is always the startup value (0 on a fresh install). The recorder
    # then writes sum=0 at today's hour, creating a massive negative spike where
    # the historical chain (~5500 kWh) meets that rogue stat 30-60 minutes after
    # a successful import. Without state_class the recorder ignores this entity
    # entirely. async_import_statistics and the Energy Dashboard do not need it.

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
        self.last_rows_written: int = 0   # rows actually committed to DB on last import

    async def async_process_file(self, file_path: str) -> None:
        """
        Parse a file and write hourly statistics into HA's recorder database.

        Uses async_import_statistics so every hourly reading gets its correct
        past timestamp in the Energy Dashboard.
        """
        # Reset both so callers never see stale data from a prior import.
        self.last_result = None
        self.last_rows_written = 0

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
            self.last_rows_written = rows_written

            if rows_written == 0:
                # All rows were clipped — nothing new was written to the DB.
                # Do not advance last_time or update the running total.
                return

            self._data[self._total_key] = round(
                float(self._data.get(self._total_key, 0.0)) + written_usage, 6
            )
            self._data[self._time_key] = newest_written
            self._data[LAST_FILE_KEY] = Path(file_path).name
            await self._store.async_save(self._data)

            self._attr_native_value = self._data[self._total_key]
            # NOTE: async_write_ha_state() is intentionally NOT called here.
            # Calling it causes HA's recorder to write a stat for this entity
            # at the current hour's timestamp. That stat then poisons
            # get_last_statistics, causing all rows in the next import file
            # (which cover historical dates) to be discarded as "already in DB".
            # Since this integration has no live sensor there is no benefit to
            # updating HA state here, and significant harm in doing so.

            _LOGGER.info(
                "[%s] Imported %.4f %s (%d rows written) from '%s'. Total: %.4f.",
                self._attr_name,
                written_usage,
                self._attr_native_unit_of_measurement,
                rows_written,
                Path(file_path).name,
                self._attr_native_value,
            )

            self._send_success_notification(file_path, written_usage, rows_written, newest_written)

    async def _import_statistics(self, result: ParseResult) -> tuple[int, float, str]:
        """
        Write hourly readings as long-term statistics into the recorder.

        ── Why we use stored last_time, not get_last_statistics ────────────────

        After every successful import, async_write_ha_state() updates the
        sensor's state in HA.  HA's recorder observes this state change and
        writes its own stat for the entity at the CURRENT hour's timestamp —
        even though this integration has no live sensor.  If we then use
        get_last_statistics to find the baseline, it returns that recorder-
        written stat at TODAY, causing every row in the next import file
        (which covers historical dates) to be discarded as "already in the DB".

        The fix: use the stored last_import_time from .storage as the true
        boundary.  That value is only ever written by us, after a confirmed
        successful write to the recorder.  It accurately reflects the last
        row we actually imported — recorder-written live stats are invisible
        to it.

        ── Algorithm ───────────────────────────────────────────────────────────

        The parser has already skipped all rows <= stored last_import_time,
        so every row in hourly_readings is genuinely new.

        We use get_last_statistics solely to get the cumulative sum baseline
        (the kWh total to continue from), not to make any filtering decisions.

        Returns:
            (rows_written, written_usage, newest_written_time)
        """
        if not result.hourly_readings:
            _LOGGER.warning("[%s] No hourly readings to import.", self._attr_name)
            return 0, 0.0, result.newest_time

        sorted_readings = sorted(result.hourly_readings, key=lambda x: x[0])

        # ── Get the cumulative sum baseline ───────────────────────────────────
        # We only use this for its sum value, never for filtering decisions.
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.entity_id,
            True,
            {"sum"},
        )

        running_sum = 0.0
        if last_stats and self.entity_id in last_stats:
            last = last_stats[self.entity_id][0]
            running_sum = float(last.get("sum") or 0.0)
            _LOGGER.debug(
                "[%s] Baseline sum=%.4f (from last stat in DB)",
                self._attr_name, running_sum,
            )
        else:
            _LOGGER.debug("[%s] First import — no existing stats, baseline=0.", self._attr_name)

        if not sorted_readings:
            return 0, 0.0, result.newest_time

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

        written_usage = sum(u for _, u in sorted_readings)
        newest_written = sorted_readings[-1][0].strftime(_STORAGE_FMT)
        return len(statistic_data), written_usage, newest_written

    def _send_success_notification(
        self,
        file_path: str,
        written_usage: float,
        rows_written: int,
        newest_written: str,
    ) -> None:
        """Show a persistent notification on successful import."""
        pn_create(
            self.hass,
            message=(
                f"**{self._attr_name}**\n\n"
                f"📄 File: `{Path(file_path).name}`\n"
                f"✅ Rows written: {rows_written}\n"
                f"📊 New usage: {written_usage:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🔢 Running total: {self._attr_native_value:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🕐 Data through: {newest_written}"
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