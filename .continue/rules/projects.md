---
name: ha-green-button-energy
globs: "**/*.py"
alwaysApply: false
description: Rules for the ha-green-button-energy HA integration
---

# ha-green-button-energy

- Domain: `green_button_energy`
- Parses Green Button XML (ESPI) via lxml or stdlib xml.etree
- Energy sensors MUST use: device_class=ENERGY, state_class=TOTAL_INCREASING, unit=kWh
- Do NOT use last_reset — TOTAL_INCREASING is the current HA standard
- unique_id pattern: `{domain}_{account_id}_{meter_id}`