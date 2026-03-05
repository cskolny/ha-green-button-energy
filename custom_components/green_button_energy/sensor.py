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
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics
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
from .parser import ParseResult, parse_file
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

            await self._import_statistics(result)

            self._data[self._total_key] = round(
                float(self._data.get(self._total_key, 0.0)) + result.new_usage, 6
            )
            self._data[self._time_key] = result.newest_time
            self._data[LAST_FILE_KEY] = Path(file_path).name
            await self._store.async_save(self._data)

            self._attr_native_value = self._data[self._total_key]
            self.async_write_ha_state()

            _LOGGER.info(
                "[%s] Imported %.4f %s (%d rows) from '%s'. Total: %.4f.",
                self._attr_name,
                result.new_usage,
                self._attr_native_unit_of_measurement,
                result.rows_imported,
                Path(file_path).name,
                self._attr_native_value,
            )

            self._send_success_notification(file_path, result)

    async def _import_statistics(self, result: ParseResult) -> None:
        """
        Write hourly readings as long-term statistics into the recorder.

        Finds the correct cumulative sum baseline by looking up the last
        recorded stat strictly BEFORE our earliest new row. Handles three
        cases:
          1. Clean append   — new data starts after all existing stats
          2. Overlap        — find pre-overlap sum so existing rows are
                             overwritten with correct cumulative sums
          3. First import   — no existing stats; baseline = 0
        """
        if not result.hourly_readings:
            _LOGGER.warning("[%s] No hourly readings to import.", self._attr_name)
            return

        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
            statistics_during_period,
        )
        from homeassistant.components.recorder.models import StatisticMeanType

        sorted_readings = sorted(result.hourly_readings)
        earliest_dt = sorted_readings[0][0]

        # Get the overall last stat to detect overlap
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
            last_start = datetime.fromtimestamp(float(last["start"]), tz=timezone.utc)

            if last_start < earliest_dt:
                # Clean append — use last sum directly as baseline
                running_sum = float(last.get("sum") or 0.0)
                _LOGGER.debug(
                    "[%s] Clean append — baseline sum=%.4f from %s",
                    self._attr_name, running_sum, last_start,
                )
            else:
                # Overlap — find sum from the hour before our earliest row
                window_start = earliest_dt - timedelta(hours=2)
                window_end = earliest_dt

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
                    before = [
                        r for r in pre_list
                        if datetime.fromtimestamp(float(r["start"]), tz=timezone.utc) < earliest_dt
                    ]
                    if before:
                        running_sum = float(before[-1].get("sum") or 0.0)
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

        # Build statistic rows with correct cumulative sums
        statistic_data: list[StatisticData] = []
        for dt_utc, usage in sorted_readings:
            running_sum = round(running_sum + usage, 6)
            statistic_data.append({
                "start": dt_utc,
                "state": round(usage, 6),
                "sum": running_sum,
            })

        # unit_class values: kWh → 'energy', CCF → 'volume'
        unit_class = "energy" if self._attr_native_unit_of_measurement == "kWh" else "volume"

        metadata: StatisticMetaData = {
            "has_mean": False,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": self._attr_name,
            "source": "recorder",
            "statistic_id": self.entity_id,
            "unit_class": unit_class,
            "unit_of_measurement": self._attr_native_unit_of_measurement,
        }

        first_sum = statistic_data[0]["sum"]
        last_sum = statistic_data[-1]["sum"]
        _LOGGER.info(
            "[%s] Writing %d statistics (sum %.4f → %.4f %s).",
            self._attr_name, len(statistic_data), first_sum, last_sum,
            self._attr_native_unit_of_measurement,
        )

        async_import_statistics(self.hass, metadata, statistic_data)

    def _send_success_notification(self, file_path: str, result: ParseResult) -> None:
        """Show a persistent notification on successful import."""
        pn_create(
            self.hass,
            message=(
                f"**{self._attr_name}**\n\n"
                f"📄 File: `{Path(file_path).name}`\n"
                f"✅ Rows imported: {result.rows_imported}\n"
                f"📊 New usage: {result.new_usage:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🔢 Running total: {self._attr_native_value:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🕐 Data through: {result.newest_time}"
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