"""Shared pytest fixtures for the Green Button Energy Import test suite.

All HA integration tests use ``pytest-homeassistant-custom-component`` which
re-exports the full set of fixtures from ``homeassistant/tests/``.  The two
fixtures that every test here needs are:

- ``hass``                     — a live, in-process HA instance
- ``enable_custom_integrations`` — must be requested so HA will load code
                                    from ``custom_components/``

The ``recorder_mock`` fixture is requested *before* ``enable_custom_integrations``
(see fixture ordering in conftest) so the recorder DB is ready before the
integration tries to import statistics.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.green_button_energy.const import DOMAIN

# ---------------------------------------------------------------------------
# Module-level pytest-asyncio configuration
# ---------------------------------------------------------------------------
# pytest-asyncio ≥ 0.21 requires an explicit asyncio_mode; "auto" means every
# async test function is treated as an asyncio coroutine automatically.
pytest_plugins = ["pytest_homeassistant_custom_component"]


# ---------------------------------------------------------------------------
# Config-entry factory
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a minimal MockConfigEntry for green_button_energy."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Green Button Energy Import",
        data={},
        unique_id=DOMAIN,
    )


# ---------------------------------------------------------------------------
# CSV / XML sample content helpers
# ---------------------------------------------------------------------------

# Minimal valid CSV with both electric and gas rows.
SAMPLE_CSV_ELECTRIC = textwrap.dedent(
    """\
    Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,1.234,kWh,$0.15,45
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 01:00:00-05:00,2026-01-01 02:00:00-05:00,0.987,kWh,$0.12,45
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 02:00:00-05:00,2026-01-01 03:00:00-05:00,1.100,kWh,$0.13,45
    """
)

SAMPLE_CSV_GAS = textwrap.dedent(
    """\
    Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather
    Test User,123 Main St,1234567890,Gas,gas,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,0.045,therms,$0.05,45
    Test User,123 Main St,1234567890,Gas,gas,2026-01-01,2026-01-01 01:00:00-05:00,2026-01-01 02:00:00-05:00,0.032,therms,$0.04,45
    """
)

# CSV with mixed electric + gas rows in one file.
SAMPLE_CSV_MIXED = textwrap.dedent(
    """\
    Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,1.234,kWh,$0.15,45
    Test User,123 Main St,1234567890,Gas,gas,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,0.045,therms,$0.05,45
    """
)

# CSV with a zero-usage row (should be skipped) and a negative row (should be skipped).
SAMPLE_CSV_WITH_CORRECTIONS = textwrap.dedent(
    """\
    Name,Address,Account Number,Service,Type,Date,Start Time,End Time,Usage,Units,Costs,Weather
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 00:00:00-05:00,2026-01-01 01:00:00-05:00,1.000,kWh,$0.12,45
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 01:00:00-05:00,2026-01-01 02:00:00-05:00,0.000,kWh,$0.00,45
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 02:00:00-05:00,2026-01-01 03:00:00-05:00,-0.500,kWh,-$0.06,45
    Test User,123 Main St,1234567890,Electric,electric,2026-01-01,2026-01-01 03:00:00-05:00,2026-01-01 04:00:00-05:00,2.000,kWh,$0.24,45
    """
)

# Minimal valid ESPI XML — electric, powerOfTenMultiplier=-3, uom=72 (Wh).
# Two readings: 938000 Wh → 0.938 kWh each.
SAMPLE_XML_ELECTRIC = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://naesb.org/espi">
      <entry>
        <content>
          <UsagePoint>
            <ServiceCategory>
              <kind>0</kind>
            </ServiceCategory>
          </UsagePoint>
        </content>
      </entry>
      <entry>
        <content>
          <ReadingType>
            <powerOfTenMultiplier>-3</powerOfTenMultiplier>
            <uom>72</uom>
          </ReadingType>
        </content>
      </entry>
      <entry>
        <content>
          <IntervalBlock>
            <IntervalReading>
              <timePeriod>
                <duration>3600</duration>
                <start>1751328000</start>
              </timePeriod>
              <value>938000</value>
            </IntervalReading>
            <IntervalReading>
              <timePeriod>
                <duration>3600</duration>
                <start>1751331600</start>
              </timePeriod>
              <value>1056000</value>
            </IntervalReading>
          </IntervalBlock>
        </content>
      </entry>
    </feed>
    """
)

# Minimal valid ESPI XML — gas, powerOfTenMultiplier=-3, uom=169 (therms).
SAMPLE_XML_GAS = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://naesb.org/espi">
      <entry>
        <content>
          <UsagePoint>
            <ServiceCategory>
              <kind>1</kind>
            </ServiceCategory>
          </UsagePoint>
        </content>
      </entry>
      <entry>
        <content>
          <ReadingType>
            <powerOfTenMultiplier>-3</powerOfTenMultiplier>
            <uom>169</uom>
          </ReadingType>
        </content>
      </entry>
      <entry>
        <content>
          <IntervalBlock>
            <IntervalReading>
              <timePeriod>
                <duration>3600</duration>
                <start>1751328000</start>
              </timePeriod>
              <value>702</value>
            </IntervalReading>
            <IntervalReading>
              <timePeriod>
                <duration>3600</duration>
                <start>1751331600</start>
              </timePeriod>
              <value>450</value>
            </IntervalReading>
          </IntervalBlock>
        </content>
      </entry>
    </feed>
    """
)


@pytest.fixture
def csv_electric_file(tmp_path: Path) -> Path:
    """Write the sample electric CSV to a temp file and return its path."""
    f = tmp_path / "electric.csv"
    f.write_text(SAMPLE_CSV_ELECTRIC, encoding="utf-8")
    return f


@pytest.fixture
def csv_gas_file(tmp_path: Path) -> Path:
    """Write the sample gas CSV to a temp file and return its path."""
    f = tmp_path / "gas.csv"
    f.write_text(SAMPLE_CSV_GAS, encoding="utf-8")
    return f


@pytest.fixture
def csv_mixed_file(tmp_path: Path) -> Path:
    """Write the mixed-commodity CSV to a temp file and return its path."""
    f = tmp_path / "mixed.csv"
    f.write_text(SAMPLE_CSV_MIXED, encoding="utf-8")
    return f


@pytest.fixture
def csv_corrections_file(tmp_path: Path) -> Path:
    """Write the CSV-with-corrections file to a temp file and return its path."""
    f = tmp_path / "corrections.csv"
    f.write_text(SAMPLE_CSV_WITH_CORRECTIONS, encoding="utf-8")
    return f


@pytest.fixture
def xml_electric_file(tmp_path: Path) -> Path:
    """Write the sample electric XML to a temp file and return its path."""
    f = tmp_path / "electric.xml"
    f.write_text(SAMPLE_XML_ELECTRIC, encoding="utf-8")
    return f


@pytest.fixture
def xml_gas_file(tmp_path: Path) -> Path:
    """Write the sample gas XML to a temp file and return its path."""
    f = tmp_path / "gas.xml"
    f.write_text(SAMPLE_XML_GAS, encoding="utf-8")
    return f
