"""Tests for watcher.py — the automatic folder-import poller.

These are mostly pure-Python / mocked-sensor tests that don't need a full HA
recorder instance, since FolderWatcher only orchestrates filesystem moves and
delegates the actual parsing/statistics work to sensor methods it doesn't
implement itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.green_button_energy.const import (
    WATCH_ERRORED_SUBDIR,
    WATCH_PROCESSED_SUBDIR,
)
from custom_components.green_button_energy.watcher import FolderWatcher


def _make_sensor(success: bool = True) -> MagicMock:
    """Return a mock sensor with async_process_file / last_result set up."""
    sensor = MagicMock()
    sensor.async_process_file = AsyncMock()
    sensor.async_process_billing_file = AsyncMock()
    result = MagicMock()
    result.success = success
    sensor.last_result = result
    sensor.last_rows_written = 1 if success else 0
    return sensor


@pytest.fixture
def entry_data() -> dict[str, MagicMock]:
    return {
        "electric": _make_sensor(),
        "gas": _make_sensor(),
        "electric_cost": _make_sensor(),
        "gas_cost": _make_sensor(),
    }


class TestEnsureFolders:
    async def test_creates_all_expected_subfolders(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        hass.config.config_dir = str(tmp_path)
        watcher = FolderWatcher(hass, entry_data)
        await hass.async_add_executor_job(watcher._ensure_folders)

        base = tmp_path / "green_button_energy_watch"
        for sub in ("electric", "gas", "electric_billing", "gas_billing", "processed", "errored"):
            assert (base / sub).is_dir()


class TestListFiles:
    def test_filters_by_extension_usage(self, hass: HomeAssistant, entry_data: dict, tmp_path: Path) -> None:
        watcher = FolderWatcher(hass, entry_data)
        folder = tmp_path / "electric"
        folder.mkdir()
        (folder / "a.csv").write_text("x")
        (folder / "b.xml").write_text("x")
        (folder / "c.txt").write_text("x")
        (folder / ".DS_Store").write_text("x")

        files = watcher._list_files(folder, is_billing=False)
        names = sorted(f.name for f in files)
        assert names == ["a.csv", "b.xml"]

    def test_filters_by_extension_billing(self, hass: HomeAssistant, entry_data: dict, tmp_path: Path) -> None:
        watcher = FolderWatcher(hass, entry_data)
        folder = tmp_path / "electric_billing"
        folder.mkdir()
        (folder / "bill.csv").write_text("x")
        (folder / "bill.xml").write_text("x")

        files = watcher._list_files(folder, is_billing=True)
        assert [f.name for f in files] == ["bill.csv"]

    def test_nonexistent_folder_returns_empty(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        assert watcher._list_files(tmp_path / "does_not_exist", is_billing=False) == []

    def test_results_sorted(self, hass: HomeAssistant, entry_data: dict, tmp_path: Path) -> None:
        watcher = FolderWatcher(hass, entry_data)
        folder = tmp_path / "gas"
        folder.mkdir()
        (folder / "z.csv").write_text("x")
        (folder / "a.csv").write_text("x")

        files = watcher._list_files(folder, is_billing=False)
        assert [f.name for f in files] == ["a.csv", "z.csv"]


class TestMoveFile:
    def test_moves_and_timestamps_file(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        src_dir = tmp_path / "watch" / "electric"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "readings.csv"
        src_file.write_text("data")

        watcher._move_file(src_file, WATCH_PROCESSED_SUBDIR)

        dest_dir = tmp_path / "watch" / WATCH_PROCESSED_SUBDIR
        moved = list(dest_dir.iterdir())
        assert len(moved) == 1
        assert moved[0].name.endswith("_readings.csv")
        assert not src_file.exists()


class TestAsyncImportOne:
    async def test_successful_usage_import_moves_to_processed(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        (watcher._base_dir).mkdir(parents=True)
        src = watcher._base_dir / "electric.csv"
        src.write_text("data")

        sensor = entry_data["electric"]
        await watcher._async_import_one(src, sensor, is_billing=False)

        sensor.async_process_file.assert_awaited_once_with(str(src))
        assert not src.exists()
        assert list((watcher._base_dir / WATCH_PROCESSED_SUBDIR).iterdir())

    async def test_failed_import_moves_to_errored(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        (watcher._base_dir).mkdir(parents=True)
        src = watcher._base_dir / "bad.csv"
        src.write_text("data")

        sensor = _make_sensor(success=False)
        await watcher._async_import_one(src, sensor, is_billing=False)

        assert not src.exists()
        assert list((watcher._base_dir / WATCH_ERRORED_SUBDIR).iterdir())

    async def test_exception_during_import_moves_to_errored(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        (watcher._base_dir).mkdir(parents=True)
        src = watcher._base_dir / "broken.csv"
        src.write_text("data")

        sensor = _make_sensor()
        sensor.async_process_file.side_effect = RuntimeError("boom")

        await watcher._async_import_one(src, sensor, is_billing=False)

        assert not src.exists()
        assert list((watcher._base_dir / WATCH_ERRORED_SUBDIR).iterdir())

    async def test_billing_import_calls_billing_method(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        (watcher._base_dir).mkdir(parents=True)
        src = watcher._base_dir / "billing.csv"
        src.write_text("data")

        sensor = entry_data["electric_cost"]
        await watcher._async_import_one(src, sensor, is_billing=True)

        sensor.async_process_billing_file.assert_awaited_once_with(str(src))
        sensor.async_process_file.assert_not_awaited()


class TestAsyncScan:
    async def test_scan_processes_files_in_all_target_folders(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        for sub in ("electric", "gas", "electric_billing", "gas_billing"):
            (watcher._base_dir / sub).mkdir(parents=True)

        (watcher._base_dir / "electric" / "e.csv").write_text("x")
        (watcher._base_dir / "gas" / "g.xml").write_text("x")
        (watcher._base_dir / "electric_billing" / "eb.csv").write_text("x")

        await watcher._async_scan()

        entry_data["electric"].async_process_file.assert_awaited_once()
        entry_data["gas"].async_process_file.assert_awaited_once()
        entry_data["electric_cost"].async_process_billing_file.assert_awaited_once()
        entry_data["gas_cost"].async_process_billing_file.assert_not_awaited()

    async def test_scan_skips_missing_sensor(
        self, hass: HomeAssistant, tmp_path: Path
    ) -> None:
        """If a sensor key is absent from entry_data, that folder is skipped silently."""
        watcher = FolderWatcher(hass, {"electric": _make_sensor()})
        watcher._base_dir = tmp_path / "watch"
        (watcher._base_dir / "gas").mkdir(parents=True)
        (watcher._base_dir / "gas" / "g.csv").write_text("x")

        # Should not raise even though "gas" sensor is missing from entry_data.
        await watcher._async_scan()

    async def test_reentrancy_guard_prevents_overlap(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        watcher._base_dir = tmp_path / "watch"
        watcher._scanning = True

        # Should return immediately without touching the (nonexistent) folders.
        await watcher._async_scan()
        entry_data["electric"].async_process_file.assert_not_awaited()


class TestStartStop:
    async def test_start_creates_folders_and_runs_initial_scan(
        self, hass: HomeAssistant, entry_data: dict, tmp_path: Path
    ) -> None:
        hass.config.config_dir = str(tmp_path)
        watcher = FolderWatcher(hass, entry_data, scan_interval_seconds=3600)

        await watcher.async_start()

        assert (tmp_path / "green_button_energy_watch" / "electric").is_dir()
        assert watcher._unsub is not None

        watcher.async_stop()
        assert watcher._unsub is None

    async def test_stop_is_safe_when_not_started(
        self, hass: HomeAssistant, entry_data: dict
    ) -> None:
        watcher = FolderWatcher(hass, entry_data)
        # Should not raise even though async_start() was never called.
        watcher.async_stop()
