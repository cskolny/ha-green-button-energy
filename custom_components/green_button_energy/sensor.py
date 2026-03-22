"""Sensor platform for the Green Button Energy Import integration.

After parsing a file, hourly readings are written directly into HA's
long-term statistics database using
:func:`~homeassistant.components.recorder.statistics.async_import_statistics`.
This is the only way to backfill historical data into the Energy Dashboard
with correct past timestamps — simply updating sensor state records a single
point at the current time and does not appear in historical hourly charts.

Design constraints
-------------------
- ``_attr_state_class`` is set to ``TOTAL_INCREASING`` so the entity appears
  in the Energy Dashboard configuration picker.
- ``_attr_native_value`` is permanently ``None`` so HA's recorder never writes
  a live boundary stat that could corrupt the historical cumulative-sum chain.
  See inline notes throughout :class:`GreenButtonSensor` for full rationale.
- ``async_write_ha_state()`` is never called for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
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
from .parser import _STORAGE_FMT, ParseResult, parse_file
from .storage import load_store

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up both Green Button sensor entities from a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being loaded.
        async_add_entities: Callback to register entities with HA.
    """
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

    # NOTE: ``update_before_add`` is intentionally omitted (defaults to False).
    # Passing ``True`` causes HA to call ``async_update()`` at startup, which
    # triggers an automatic ``async_write_ha_state()``.  That state write is
    # observed by HA's recorder, which inserts a stat at the CURRENT hour with
    # ``sum = stored_total``.  On a fresh install ``stored_total = 0``, so a
    # ``sum = 0`` stat lands at today's hour.  After a historical import writes
    # thousands of rows summing to ~5 500 kWh, the Energy Dashboard computes
    # ``0 − 5 500 = −5 500 kWh`` — a massive negative spike appearing 30–60
    # minutes after the import completes.
    async_add_entities([electric_sensor, gas_sensor])

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "electric": electric_sensor,
        "gas": gas_sensor,
    }


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------


class GreenButtonSensor(SensorEntity):
    """Cumulative energy/gas sensor backed by HA's long-term statistics DB.

    This sensor has **no live data source**.  Its sole purpose is to provide
    an entity ID and metadata that ``async_import_statistics`` can attach
    historical readings to, making them visible in the Energy Dashboard.

    Key design decisions (see CHANGELOG for full history):

    - ``_attr_native_value = None`` — prevents HA's recorder from writing
      boundary stats that corrupt the cumulative-sum chain.
    - ``_attr_state_class = TOTAL_INCREASING`` — required for the entity to
      appear in the Energy Dashboard configuration picker.
    - ``async_write_ha_state()`` is never called — same reason as above.
    - ``_processing_lock`` serialises concurrent import requests so two
      simultaneous file drops cannot interleave their DB writes.
    """

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = False

    def __init__(
        self,
        hass: HomeAssistant,
        store: Store[dict[str, Any]],
        data: dict[str, Any],
        service_type: str,
        total_key: str,
        time_key: str,
        unit: str,
        device_class: SensorDeviceClass,
        name: str,
        unique_id: str,
    ) -> None:
        """Initialise the sensor from persisted storage data.

        Args:
            hass: The Home Assistant instance.
            store: Open storage handle for persisting import state.
            data: Current storage data dict (may be empty on first run).
            service_type: ``"electric"`` or ``"gas"``.
            total_key: Storage key for the cumulative usage total.
            time_key: Storage key for the last-imported timestamp.
            unit: Unit of measurement (``"kWh"`` or ``"CCF"``).
            device_class: HA sensor device class.
            name: Human-readable sensor name shown in the UI.
            unique_id: Stable unique ID for entity registry persistence.
        """
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
        # Permanently None: prevents HA's recorder from writing hourly boundary
        # stats that would corrupt the Energy Dashboard statistics chain.
        self._attr_native_value: float | None = None
        self._processing_lock = asyncio.Lock()
        # Populated by async_process_file; read by the WebSocket handler.
        self.last_result: ParseResult | None = None
        self.last_rows_written: int = 0

    # ------------------------------------------------------------------
    # Public API consumed by the WebSocket handler
    # ------------------------------------------------------------------

    async def async_process_file(self, file_path: str) -> None:
        """Parse *file_path* and write new hourly statistics into the recorder.

        Uses :func:`~homeassistant.components.recorder.statistics.async_import_statistics`
        so every reading lands at its correct historical timestamp in the Energy
        Dashboard.  Results are stored in :attr:`last_result` and
        :attr:`last_rows_written` for the WebSocket handler to report back to
        the frontend.

        Args:
            file_path: Absolute path to the temporary file written by the
                WebSocket handler.
        """
        # Reset before acquiring the lock so callers never see stale results
        # from a prior import when the current one raises before completing.
        self.last_result = None
        self.last_rows_written = 0

        async with self._processing_lock:
            last_time: str = self._data.get(self._time_key, "")

            _LOGGER.debug(
                "[%s] Processing '%s' (last_time='%s')",
                self._attr_name,
                file_path,
                last_time or "none",
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
                    self._attr_name,
                    Path(file_path).name,
                )
                return

            rows_written, written_usage, newest_written = await self._import_statistics(result)
            self.last_rows_written = rows_written

            if rows_written == 0:
                # Every row the parser found already exists in the DB.
                # Do not advance last_time or update the running total.
                return

            self._data[self._total_key] = round(
                float(self._data.get(self._total_key, 0.0)) + written_usage, 6
            )
            self._data[self._time_key] = newest_written
            self._data[LAST_FILE_KEY] = Path(file_path).name
            await self._store.async_save(self._data)

            # Deliberately NOT calling async_write_ha_state() — see class docstring.

            _LOGGER.info(
                "[%s] Imported %.4f %s (%d rows written) from '%s'. Total: %.4f.",
                self._attr_name,
                written_usage,
                self._attr_native_unit_of_measurement,
                rows_written,
                Path(file_path).name,
                self._data.get(self._total_key, 0.0),
            )

            self._send_success_notification(file_path, written_usage, rows_written, newest_written)

    # ------------------------------------------------------------------
    # Statistics import
    # ------------------------------------------------------------------

    async def _import_statistics(
        self,
        result: ParseResult,
    ) -> tuple[int, float, str]:
        """Write accepted hourly readings into the HA long-term statistics DB.

        Why stored ``last_time``, not ``get_last_statistics``, for filtering
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Calling ``async_write_ha_state()`` causes HA's recorder to write a
        stat for this entity at the CURRENT hour — even though this integration
        has no live sensor.  If ``get_last_statistics`` were used for row
        filtering it would return that recorder-written stat (timestamped
        *today*), and every row in the next import file (covering historical
        dates) would be discarded as "already in the DB."  Result: 0 rows
        written, no notification, and no storage update.

        Resolution: the parser applies the stored ``last_time`` as the
        deduplication cutoff.  ``get_last_statistics`` is called here
        **solely** to obtain the cumulative-sum baseline (the kWh total to
        continue from) — never for filtering.

        Algorithm
        ~~~~~~~~~~
        1. Sort accepted readings chronologically.
        2. Retrieve the current end-of-chain cumulative sum from the DB.
        3. Append each reading to that sum and build :class:`StatisticData`.
        4. Call :func:`async_import_statistics` to persist all rows atomically.

        Args:
            result: A successful :class:`~.parser.ParseResult` with at least
                one entry in ``hourly_readings``.

        Returns:
            A ``(rows_written, written_usage, newest_written_time)`` tuple.
            ``rows_written`` is the count of :class:`StatisticData` objects
            passed to :func:`async_import_statistics`.
        """
        if not result.hourly_readings:
            _LOGGER.warning("[%s] No hourly readings to import.", self._attr_name)
            return 0, 0.0, result.newest_time

        sorted_readings = sorted(result.hourly_readings, key=lambda x: x[0])

        # Retrieve the current cumulative-sum baseline from the recorder DB.
        # Used only for the baseline value — never for filtering decisions.
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
                self._attr_name,
                running_sum,
            )
        else:
            _LOGGER.debug(
                "[%s] First import — no existing stats, baseline=0.",
                self._attr_name,
            )

        statistic_data: list[StatisticData] = []
        for dt_utc, usage in sorted_readings:
            running_sum = round(running_sum + usage, 6)
            statistic_data.append(
                StatisticData(
                    start=dt_utc,
                    state=round(usage, 6),
                    sum=running_sum,
                ),
            )

        unit_class = (
            "energy"
            if self._attr_native_unit_of_measurement == UNIT_ELECTRIC
            else "volume"
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

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _send_success_notification(
        self,
        file_path: str,
        written_usage: float,
        rows_written: int,
        newest_written: str,
    ) -> None:
        """Display a persistent HA notification confirming a successful import.

        Args:
            file_path: Full path to the processed temp file (basename shown).
            written_usage: Total usage from rows committed to the DB.
            rows_written: Number of rows committed to the DB.
            newest_written: UTC timestamp string of the last written row.
        """
        pn_create(
            self.hass,
            message=(
                f"**{self._attr_name}**\n\n"
                f"📄 File: `{Path(file_path).name}`\n"
                f"✅ Rows written: {rows_written}\n"
                f"📊 New usage: {written_usage:.4f} {self._attr_native_unit_of_measurement}\n"
                f"🔢 Running total: "
                f"{self._data.get(self._total_key, 0.0):.4f} "
                f"{self._attr_native_unit_of_measurement}\n"
                f"🕐 Data through: {newest_written}"
            ),
            title="Avangrid Green Button — Import Successful",
            notification_id=f"{NOTIF_SUCCESS}_{self._attr_unique_id}",
        )

    def _send_error_notification(
        self,
        file_path: str,
        result: ParseResult,
    ) -> None:
        """Display a persistent HA notification reporting a parse failure.

        Args:
            file_path: Full path to the processed temp file (basename shown).
            result: The failed :class:`~.parser.ParseResult` containing errors.
        """
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
