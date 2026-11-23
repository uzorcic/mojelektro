"""Sensor states produced from mocked Moj Elektro responses."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mojelektro.api import MojElektroAuthError, MojElektroClient, MojElektroTimeoutError
from custom_components.mojelektro.balance import Reading, local_date, parse_meter_readings
from custom_components.mojelektro.const import (
    CONF_TOKEN,
    CONF_USAGE_POINT,
    DOMAIN,
    READING_TYPE_EXPORT_STATE,
    READING_TYPE_IMPORT_STATE,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState

from helpers import load_fixture

TZ = ZoneInfo("Europe/Ljubljana")
TOKEN = "test-token"
USAGE = "3-123456"
FIXED = datetime(2026, 6, 15, 9, 0, tzinfo=TZ)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TOKEN: TOKEN, CONF_USAGE_POINT: USAGE},
        unique_id=USAGE,
        title=f"Moj Elektro {USAGE}",
    )


def _filter(readings: dict[str, list[Reading]], start: date, end: date) -> dict[str, list[Reading]]:
    filtered: dict[str, list[Reading]] = {}
    for reading_type, items in readings.items():
        kept = [
            item
            for item in items
            if start <= local_date(item.timestamp, TZ) <= end
        ]
        if kept:
            filtered[reading_type] = kept
    return filtered


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_sensors_show_yearly_and_monthly_balance(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    catalog = load_fixture("reading_types.json")
    meter = load_fixture("merilno_mesto.json")
    parsed = parse_meter_readings(load_fixture("meter_readings.json"))
    calls: list[tuple[date, date, list[str]]] = []

    async def fake_readings(self, usage_point, start, end, reading_types):
        assert usage_point == USAGE
        calls.append((start, end, list(reading_types)))
        return _filter(parsed, start, end)

    with (
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=catalog)),
        patch.object(MojElektroClient, "get_merilno_mesto", new=AsyncMock(return_value=meter)),
        patch.object(MojElektroClient, "get_meter_readings", new=fake_readings),
        patch("custom_components.mojelektro.coordinator.dt_util.now", return_value=FIXED),
        patch(
            "custom_components.mojelektro.coordinator.dt_util.get_default_time_zone",
            return_value=TZ,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert calls == [
        (date(2026, 1, 1), date(2026, 1, 7), [READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE]),
        (date(2026, 6, 1), date(2026, 6, 15), [READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE]),
    ]

    balance = hass.states.get("sensor.moj_elektro_3_123456_letna_bilanca")
    assert balance is not None, hass.states.async_entity_ids()
    assert float(balance.state) == pytest.approx(-149.75)
    assert balance.attributes["device_class"] == "energy"
    assert balance.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in balance.attributes
    assert balance.attributes["last_reading_date"] == "2026-06-14"
    assert balance.attributes["meter_name"] == "Hiša"
    assert balance.attributes["consumption_kwh"] == pytest.approx(1450.5)
    assert balance.attributes["delivered_kwh"] == pytest.approx(1600.25)
    assert balance.attributes["icon"] == "mdi:solar-power"

    imported = hass.states.get("sensor.moj_elektro_3_123456_letna_prejeta_energija")
    exported = hass.states.get("sensor.moj_elektro_3_123456_letna_oddana_energija")
    assert float(imported.state) == pytest.approx(1450.5)
    assert imported.attributes["state_class"] == "total"
    assert imported.attributes["baseline_date"] == "2026-01-01"
    assert imported.attributes["reading_type"] == READING_TYPE_IMPORT_STATE
    assert float(exported.state) == pytest.approx(1600.25)
    assert exported.attributes["reading_type"] == READING_TYPE_EXPORT_STATE

    monthly = hass.states.get("sensor.moj_elektro_3_123456_mesecna_bilanca")
    assert float(monthly.state) == pytest.approx(-49.75)
    assert monthly.attributes["period_start"] == "2026-06-01"
    assert hass.states.get("sensor.moj_elektro_3_123456_mesecna_prejeta_energija")
    assert hass.states.get("sensor.moj_elektro_3_123456_mesecna_oddana_energija")


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_auth_failure_starts_reauth(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(
        MojElektroClient,
        "get_reading_types",
        new=AsyncMock(side_effect=MojElektroAuthError("rejected", status=401)),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_timeout_leaves_the_entry_retrying(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch.object(
            MojElektroClient,
            "get_reading_types",
            new=AsyncMock(return_value=load_fixture("reading_types.json")),
        ),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(return_value=load_fixture("merilno_mesto.json")),
        ),
        patch.object(
            MojElektroClient,
            "get_meter_readings",
            new=AsyncMock(side_effect=MojElektroTimeoutError("timed out")),
        ),
        patch("custom_components.mojelektro.coordinator.dt_util.now", return_value=FIXED),
        patch(
            "custom_components.mojelektro.coordinator.dt_util.get_default_time_zone",
            return_value=TZ,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
