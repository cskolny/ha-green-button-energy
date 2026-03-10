# Green Button Energy Import — Home Assistant Custom Integration

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-blue?logo=homeassistant)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Release](https://img.shields.io/github/v/release/cskolny/ha-green-button-energy)](https://github.com/cskolny/ha-green-button-energy/releases)

Import your **Avangrid utility** smart meter usage data directly into the [Home Assistant Energy Dashboard](https://www.home-assistant.io/docs/energy/) via a drag-and-drop sidebar panel. Supports both **electric** (kWh) and **gas** (CCF/therms) usage from Green Button CSV and XML exports.

![Energy Dashboard](screenshots/energy-dashboard.png)

---

## Supported Utilities

This integration works with any Avangrid utility that provides Green Button data exports:

| Utility | State | Service |
|---------|-------|---------|
| Rochester Gas & Electric (RG&E) | New York | Electric & Gas |
| New York State Electric & Gas (NYSEG) | New York | Electric & Gas |
| Central Maine Power (CMP) | Maine | Electric |
| United Illuminating (UI) | Connecticut | Electric |
| Connecticut Natural Gas (CNG) | Connecticut | Gas |
| Southern Connecticut Gas (SCG) | Connecticut | Gas |
| Berkshire Gas | Massachusetts | Gas |

---

## Features

- ⚡ **Drag-and-drop import** — dedicated sidebar panel, no command line needed
- 📊 **Full historical backfill** — imports all hourly data with correct past timestamps into the Energy Dashboard
- 🔁 **Safe re-imports** — duplicate rows are automatically skipped; overlapping files can be re-dropped safely
- 🛡️ **Live data protection** — imports are clipped at the last existing stat boundary, preventing overwrites of live sensor data and the negative consumption values they would cause
- 📁 **CSV and XML support** — works with both Avangrid Opower CSV exports and standard Green Button ESPI XML exports
- 🔔 **Import notifications** — persistent HA notifications confirm rows written, rows clipped, and usage totals on success or failure
- 🔌 **No YAML configuration** — fully UI-driven setup; the sidebar panel registers itself automatically
- 🏠 **Energy Dashboard ready** — sensors use the correct `device_class`, `state_class`, and units for HA's Energy Dashboard

---

## Sensors Created

| Sensor | Entity ID | Unit | Device Class | State Class |
|--------|-----------|------|-------------|-------------|
| Avangrid Electric Total | `sensor.avangrid_electric_total` | kWh | `energy` | `total_increasing` |
| Avangrid Gas Total | `sensor.avangrid_gas_total` | CCF | `gas` | `total_increasing` |

Both sensors are automatically available in **Settings → Energy** for the Electricity grid and Gas consumption sections.

---

## Requirements

- Home Assistant **2025.1 or later** (tested on 2026.3)
- An Avangrid utility account with smart meter data and Green Button export access
- File access to your HA config directory (for initial install only)

---

## Installation

### Manual

1. Download or clone this repository
2. Copy the `green_button_energy` folder into your HA config directory:
   ```
   config/custom_components/green_button_energy/
   ```
3. Verify the file structure looks like this:
   ```
   custom_components/green_button_energy/
   ├── frontend/
   │   └── green-button-energy-panel.js
   ├── images/
   │   ├── icon.png
   │   └── logo.png
   ├── translations/
   │   └── en.json
   ├── __init__.py
   ├── config_flow.py
   ├── const.py
   ├── manifest.json
   ├── parser.py
   ├── sensor.py
   ├── storage.py
   └── strings.json
   ```

### HACS

**Custom repository (available now):**

1. In HACS → Integrations → three-dot menu → **Custom repositories**
2. Add `https://github.com/cskolny/ha-green-button-energy` with category **Integration**
3. Search for "Green Button Energy Import" and install

**Default HACS store:** Submission pending.

---

## Configuration

No `configuration.yaml` changes are required. The sidebar panel registers itself automatically when the integration loads.

### Step 1 — Restart Home Assistant

After copying the files:

```bash
docker compose restart homeassistant
# or via UI: Settings → System → Restart
```

### Step 2 — Add the Integration

1. Go to **Settings → Devices & Services**
2. Click **+ Add Integration**
3. Search for **Green Button Energy Import**
4. Click **Submit** — no additional configuration required

The **Green Button Import** panel will appear in your sidebar immediately after setup completes.

### Step 3 — Add Sensors to the Energy Dashboard

1. Go to **Settings → Energy**
2. Under **Electricity → Grid consumption** → **Add consumption** → select `Avangrid Electric Total`
3. Under **Gas consumption** → **Add gas source** → select `Avangrid Gas Total`
4. Click **Save**

---

## Usage

### Downloading Your Data

Log in to your Avangrid utility website and navigate to your energy usage or account section. Look for a **Green Button** or **Download My Data** option and select your desired date range. Download as **CSV** or **Green Button XML**. Download a separate file for electric and gas if needed.

| Utility | Website |
|---------|---------|
| RG&E | [myrge.com](https://www.myrge.com) |
| NYSEG | [myny.com](https://www.myny.com) |
| Central Maine Power | [cmpco.com](https://www.cmpco.com) |
| United Illuminating | [uinet.com](https://www.uinet.com) |
| Connecticut Natural Gas | [cngcorp.com](https://www.cngcorp.com) |
| Southern Connecticut Gas | [soconngas.com](https://www.soconngas.com) |
| Berkshire Gas | [berkshiregas.com](https://www.berkshiregas.com) |

> **Tip:** For initial historical backfill, download in 12-month chunks working backwards from today. Overlapping date ranges between files are handled safely.

### Importing Files

1. Open **Green Button Import** in the Home Assistant sidebar
2. Drag your electric CSV or XML onto the ⚡ **Electric Usage** zone
3. Wait for the success notification confirming the row count and usage total
4. Drag your gas CSV or XML onto the 🔥 **Gas Usage** zone
5. The Energy Dashboard will populate with historical hourly data immediately

### Success Notification

After a successful import the notification shows:

- **Rows written** — rows actually committed to the long-term statistics database
- **New usage** — total energy/gas in the imported rows
- **Running total** — cumulative sensor total since integration was set up
- **Data through** — the newest timestamp actually written to the DB
- **Rows clipped** *(only shown when > 0)* — rows beyond the last existing DB stat that were skipped to protect live sensor data

### Weekly Workflow

Avangrid utilities update smart meter data with a ~48 hour delay. A typical weekly routine:

1. Download the past week's CSV or XML from your utility website
2. Drop the electric file into the ⚡ zone
3. Drop the gas file into the 🔥 zone
4. Done — new data appears in the Energy Dashboard

Duplicate rows from overlapping date ranges are automatically skipped.

---

## Supported File Formats

### CSV (Opower Export)

| Column | Description |
|--------|-------------|
| `Start Time` | Interval start timestamp (timezone-aware ISO format) |
| `Usage` | Energy or gas usage for the interval |
| `Type` | `electric` or `gas` |

### XML (Green Button ESPI)

| Service | ServiceCategory kind | uom | Conversion |
|---------|---------------------|-----|-----------|
| Electric | `0` | `72` (Wh) | `value × 10⁻³ ÷ 1000 = kWh` |
| Gas | `1` | `169` (therms) | `value × 10⁻³ = therms` |

The parser auto-detects the service type and unit conversion from the `ReadingType` metadata in the file.

---

## How It Works

### Architecture

```
Browser (HA Frontend)          HA Backend (Python)
─────────────────────          ───────────────────
Green Button Import Panel      WebSocket Handler
  │                              │
  │  FileReader.readAsText()     │
  │  → file content (UTF-8)      │
  │                              │
  └──── WebSocket message ──────►│
         type: green_button      │
         _energy/import_file     │
                                 │
                          Write temp file
                                 │
                          parser.py
                          (CSV or XML)
                                 │
                          ParseResult
                          (hourly_readings[])
                                 │
                          _import_statistics()
                          • query last existing stat
                          • detect clean append vs overlap
                          • clip rows at db_boundary
                          • build cumulative sums
                                 │
                          recorder.async_import_statistics()
                          (writes historical stats to DB)
                                 │
                          sensor state update
                          persistent notification
```

### Why `async_import_statistics`?

Simply updating a sensor's state only records a single data point at the current time. The Energy Dashboard reads from HA's **long-term statistics** database, which stores hourly aggregates. `async_import_statistics` writes directly into this database with the correct historical timestamps, enabling full backfill of months of hourly data in a single import.

### Duplicate Prevention

Each successful import stores the timestamp of the most recently **written** stat in HA's `.storage` directory (`green_button_energy_data`). On subsequent imports, any row at or before this timestamp is skipped.

### Live Data Protection

When importing a historical file on a day when HA has already recorded live sensor stats, a naive import would overwrite those stats with incorrectly calculated cumulative sums, producing negative consumption values in the Energy Dashboard. The integration prevents this by clipping any import rows that go beyond the last existing stat in the database (`db_boundary`). Clipping is reported in the success notification.

### File Size Limit

Files larger than **10 MB** are rejected before any processing occurs. Green Button exports for a full year of hourly data are typically well under 2 MB.

---

## Resetting / Starting Fresh

If you need to wipe all data and start over:

1. **Delete long-term statistics** — Developer Tools → Statistics → find both Avangrid sensors → delete all statistics
2. **Purge entity history** — Developer Tools → Actions:
   ```yaml
   action: recorder.purge_entities
   data:
     entity_id:
       - sensor.avangrid_electric_total
       - sensor.avangrid_gas_total
     keep_days: 0
   ```
3. **Delete integration storage:**
   ```bash
   rm /config/.storage/green_button_energy_data
   ```
4. **Restart HA**
5. **Re-import your files** — oldest date range first, then newer

---

## Troubleshooting

### "No new data found" notification
The integration's stored `last_time` is already at or past the end of your file. Download a more recent date range, or delete `.storage/green_button_energy_data` and restart to reset.

### Negative consumption values in Energy Dashboard
This can happen if data imported with v1.0.0 overlaps with live sensor stats. Follow the full reset procedure above and reimport. v1.1.0 prevents this automatically via `db_boundary` clipping.

### Sensor doesn't appear in Energy Dashboard gas section
Verify in **Developer Tools → States** that `sensor.avangrid_gas_total` shows `device_class: gas` and `unit_of_measurement: CCF`.

### "Connection error" when dropping a file
Check **Settings → System → Logs** and filter for `green_button_energy`. Common causes: integration not fully loaded, file is not valid UTF-8, or HA WebSocket connection dropped — refresh the browser and try again.

### Sidebar panel doesn't appear after setup
Try **Settings → System → Restart** and then a hard browser refresh (**Cmd+Shift+R** / **Ctrl+Shift+R**). If it still doesn't appear, check the HA logs for errors from `green_button_energy`.

### Integration not found in Settings → Add Integration
The `custom_components/green_button_energy/` folder name must use **underscores** and match exactly. Verify:
```bash
ls /config/custom_components/
# Should show: green_button_energy
```

---

## Contributing

Pull requests are welcome! If you use an Avangrid utility not listed above and want to add support, please open an issue with a sample file (with personal data removed).

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-improvement`)
3. Commit your changes
4. Open a pull request

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

Built for the Home Assistant community. Avangrid, RG&E, NYSEG, Central Maine Power, United Illuminating, Connecticut Natural Gas, Southern Connecticut Gas, and Berkshire Gas are trademarks of their respective owners. This project is not affiliated with or endorsed by Avangrid or any of its subsidiaries.