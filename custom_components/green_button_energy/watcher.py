"""Folder-watcher for automatic Green Button file imports.

Polls a set of drop folders under the Home Assistant config directory on a
fixed interval and imports any files found using the same processing methods
the sidebar panel's WebSocket handlers use.  Successfully (or permanently
unsuccessfully) processed files are moved out of the watch folder so they are
never re-scanned.

Folder layout (created automatically under ``<config>/green_button_energy_watch/``)::

    green_button_energy_watch/
    ├── electric/            <- drop hourly electric CSV/XML here
    ├── gas/                 <- drop hourly gas CSV/XML here
    ├── electric_billing/    <- drop monthly electric billing CSV here
    ├── gas_billing/         <- drop monthly gas billing CSV here
    ├── processed/           <- successfully imported files are moved here
    └── errored/             <- files that failed to parse are moved here

Why polling instead of inotify/watchdog
----------------------------------------
- No new pip dependency — ``manifest.json`` currently declares
  ``"requirements": []``; ``watchdog`` would add a compiled dependency across
  every platform HA runs on.
- Works identically on every filesystem HA might see the config directory
  through (including network shares like SMB/NFS, where inotify events are
  unreliable or unsupported).
- Avangrid utilities update smart meter data with a ~48-hour delay, so a
  60-second poll interval is functionally instantaneous for this use case.

All directory scanning and file moves run in the executor thread pool via
``hass.async_add_executor_job`` — never on the event loop.  Imports are
delegated straight to ``GreenButtonSensor.async_process_file`` /
``GreenButtonCostSensor.async_process_billing_file``, so watched imports get
identical dedup, statistics-writing, and notification behavior as
panel-driven imports.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    WATCH_DIR_NAME,
    WATCH_ERRORED_SUBDIR,
    WATCH_PROCESSED_SUBDIR,
    WATCH_SCAN_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from .sensor import GreenButtonCostSensor, GreenButtonSensor

_LOGGER = logging.getLogger(__name__)

# Watch sub-folder name -> (key in the entry's sensor dict, is_billing)
_WATCH_TARGETS: dict[str, tuple[str, bool]] = {
    "electric": ("electric", False),
    "gas": ("gas", False),
    "electric_billing": ("electric_cost", True),
    "gas_billing": ("gas_cost", True),
}

_USAGE_EXTENSIONS = frozenset({".csv", ".xml"})
_BILLING_EXTENSIONS = frozenset({".csv"})


class FolderWatcher:
    """Polls watch folders and imports any files found.

    One instance is created per config entry in ``async_setup_entry`` and
    stopped in ``async_unload_entry``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        scan_interval_seconds: int = WATCH_SCAN_INTERVAL_SECONDS,
    ) -> None:
        """Initialise the watcher.

        Args:
            hass: The Home Assistant instance.
            entry_data: The ``hass.data[DOMAIN][entry.entry_id]`` dict
                containing the already-created sensor instances
                (``"electric"``, ``"gas"``, ``"electric_cost"``, ``"gas_cost"``).
            scan_interval_seconds: How often to poll the watch folders.
        """
        self.hass = hass
        self._entry_data = entry_data
        self._base_dir = Path(hass.config.path(WATCH_DIR_NAME))
        self._scan_interval_seconds = scan_interval_seconds
        self._unsub: CALLBACK_TYPE | None = None
        # Reentrancy guard so a slow scan (e.g. a very large file importing)
        # can't overlap with the next timer tick.
        self._scanning = False

    async def async_start(self) -> None:
        """Create the folder structure, run an initial scan, then start polling."""
        await self.hass.async_add_executor_job(self._ensure_folders)

        self._unsub = async_track_time_interval(
            self.hass,
            self._async_scan_tick,
            timedelta(seconds=self._scan_interval_seconds),
        )

        _LOGGER.info(
            "[%s] Folder watcher started — watching '%s' every %ds.",
            DOMAIN,
            self._base_dir,
            self._scan_interval_seconds,
        )

        # Run one scan immediately so files dropped while HA was offline are
        # picked up right away instead of waiting a full interval.
        await self._async_scan()

    @callback
    def async_stop(self) -> None:
        """Cancel the polling timer."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        _LOGGER.info("[%s] Folder watcher stopped.", DOMAIN)

    # ------------------------------------------------------------------
    # Scan loop
    # ------------------------------------------------------------------

    async def _async_scan_tick(self, _now: datetime) -> None:
        """Timer callback — delegates to the shared scan routine."""
        await self._async_scan()

    async def _async_scan(self) -> None:
        """Scan every watch sub-folder and import any files found."""
        if self._scanning:
            return
        self._scanning = True
        try:
            for subdir, (sensor_key, is_billing) in _WATCH_TARGETS.items():
                sensor = self._entry_data.get(sensor_key)
                if sensor is None:
                    # Sensor not yet registered (e.g. mid-reload) — skip this tick.
                    continue

                folder = self._base_dir / subdir
                files = await self.hass.async_add_executor_job(
                    self._list_files, folder, is_billing
                )
                for file_path in files:
                    await self._async_import_one(file_path, sensor, is_billing)
        finally:
            self._scanning = False

    async def _async_import_one(
        self,
        file_path: Path,
        sensor: GreenButtonSensor | GreenButtonCostSensor,
        is_billing: bool,
    ) -> None:
        """Import a single file and move it to processed/ or errored/.

        Args:
            file_path: Absolute path to the file inside the watch folder.
            sensor: The target ``GreenButtonSensor`` or ``GreenButtonCostSensor``.
            is_billing: Whether this is a billing-CSV import.
        """
        _LOGGER.info("[%s] Watcher picked up '%s'.", DOMAIN, file_path.name)

        try:
            if is_billing:
                await sensor.async_process_billing_file(str(file_path))  # type: ignore[union-attr]
            else:
                await sensor.async_process_file(str(file_path))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            # Defensive: async_process_file/async_process_billing_file already
            # catch parse errors into ParseResult.errors, but this guards
            # against anything unexpected (e.g. a locked/partially-written
            # file) so the watcher itself never crashes or gets stuck retrying
            # the same broken file forever.
            _LOGGER.exception(
                "[%s] Unexpected error importing '%s' from watch folder.",
                DOMAIN,
                file_path.name,
            )
            await self.hass.async_add_executor_job(self._move_file, file_path, WATCH_ERRORED_SUBDIR)
            return

        result = sensor.last_result
        destination = (
            WATCH_ERRORED_SUBDIR if (result is None or not result.success) else WATCH_PROCESSED_SUBDIR
        )
        await self.hass.async_add_executor_job(self._move_file, file_path, destination)

    # ------------------------------------------------------------------
    # Blocking filesystem helpers — always called via executor job
    # ------------------------------------------------------------------

    def _ensure_folders(self) -> None:
        """Create the drop folders and processed/errored folders if missing."""
        for subdir in (*_WATCH_TARGETS, WATCH_PROCESSED_SUBDIR, WATCH_ERRORED_SUBDIR):
            (self._base_dir / subdir).mkdir(parents=True, exist_ok=True)

    def _list_files(self, folder: Path, is_billing: bool) -> list[Path]:
        """Return sorted, extension-filtered files waiting in *folder*.

        Args:
            folder: The sub-folder to scan.
            is_billing: If ``True``, only ``.csv`` is accepted; otherwise
                ``.csv`` and ``.xml`` are both accepted.

        Returns:
            Sorted list of file paths (oldest name first — filenames from
            utility exports are date-ordered, so this processes chronologically).
        """
        if not folder.exists():
            return []
        valid_ext = _BILLING_EXTENSIONS if is_billing else _USAGE_EXTENSIONS
        return sorted(
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in valid_ext and not p.name.startswith(".")
        )

    def _move_file(self, file_path: Path, destination_subdir: str) -> None:
        """Move *file_path* into *destination_subdir*, timestamp-prefixed.

        The timestamp prefix prevents filename collisions when the same
        filename is dropped more than once over time, and makes the
        processed/errored folders self-documenting in a file browser.

        Args:
            file_path: The file to move.
            destination_subdir: ``WATCH_PROCESSED_SUBDIR`` or ``WATCH_ERRORED_SUBDIR``.
        """
        dest_dir = self._base_dir / destination_subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = dest_dir / f"{timestamp}_{file_path.name}"
        try:
            shutil.move(str(file_path), str(dest_path))
        except OSError:
            _LOGGER.exception(
                "[%s] Failed to move '%s' to '%s' after processing. "
                "It will be re-scanned and re-imported on the next tick.",
                DOMAIN,
                file_path,
                dest_dir,
            )
