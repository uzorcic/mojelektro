"""Daily 00:05 refresh, with a short retry and no interval polling."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mojelektro.api import (
    MojElektroAuthError,
    MojElektroClient,
    MojElektroTimeoutError,
)
from custom_components.mojelektro.const import (
    CONF_TOKEN,
    CONF_USAGE_POINT,
    DAILY_REFRESH_HOUR,
    DAILY_REFRESH_MINUTE,
    DAILY_RETRY_ATTEMPTS,
    DAILY_RETRY_DELAY,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.util import dt as dt_util

from helpers import load_fixture

TOKEN = "test-token"
USAGE = "3-123456"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TOKEN: TOKEN, CONF_USAGE_POINT: USAGE},
        unique_id=USAGE,
    )


def _next_daily(after: datetime) -> datetime:
    """Next 00:05 strictly after ``after`` in the Home Assistant time zone."""
    local = dt_util.as_local(after)
    target = local.replace(
        hour=DAILY_REFRESH_HOUR,
        minute=DAILY_REFRESH_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= local:
        target += timedelta(days=1)
    return target


async def _flush(hass) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_fetches_at_startup_and_at_0005_without_interval_polling(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    calls: list[tuple] = []

    async def fake_readings(self, usage_point, start, end, reading_types):
        calls.append((start, end))
        return {}

    with (
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(return_value=load_fixture("merilno_mesto.json")),
        ),
        patch.object(MojElektroClient, "get_meter_readings", new=fake_readings),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _flush(hass)

        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.update_interval is None
        startup_calls = len(calls)
        assert startup_calls >= 1

        daily = _next_daily(dt_util.now())
        async_fire_time_changed(hass, daily - timedelta(minutes=1))
        await _flush(hass)
        assert len(calls) == startup_calls

        async_fire_time_changed(hass, daily)
        await _flush(hass)
        assert len(calls) == startup_calls * 2

        assert await hass.config_entries.async_unload(entry.entry_id)
        await _flush(hass)
        async_fire_time_changed(hass, daily + timedelta(days=1))
        await _flush(hass)
        assert len(calls) == startup_calls * 2


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_failed_nightly_fetch_retries_then_stops(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    calls = 0
    armed = False

    async def fake_readings(self, usage_point, start, end, reading_types):
        nonlocal calls
        calls += 1
        if not armed:
            return {}
        raise MojElektroTimeoutError("timed out")

    with (
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(return_value=load_fixture("merilno_mesto.json")),
        ),
        patch.object(MojElektroClient, "get_meter_readings", new=fake_readings),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _flush(hass)
        after_startup = calls
        armed = True

        moment = _next_daily(dt_util.now())
        for _attempt in range(1 + DAILY_RETRY_ATTEMPTS):
            async_fire_time_changed(hass, moment)
            await _flush(hass)
            moment += DAILY_RETRY_DELAY + timedelta(minutes=2)

        attempted = calls - after_startup
        assert attempted == 1 + DAILY_RETRY_ATTEMPTS

        async_fire_time_changed(hass, moment)
        await _flush(hass)
        assert calls - after_startup == attempted


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_auth_failure_at_0005_does_not_schedule_a_retry(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    calls = 0
    armed = False

    async def fake_readings(self, usage_point, start, end, reading_types):
        nonlocal calls
        calls += 1
        if not armed:
            return {}
        raise MojElektroAuthError("rejected", status=401)

    with (
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(return_value=load_fixture("merilno_mesto.json")),
        ),
        patch.object(MojElektroClient, "get_meter_readings", new=fake_readings),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _flush(hass)
        after_startup = calls
        armed = True

        daily = _next_daily(dt_util.now())
        async_fire_time_changed(hass, daily)
        await _flush(hass)
        assert calls == after_startup + 1

        flows = hass.config_entries.flow.async_progress()
        assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)

        async_fire_time_changed(hass, daily + DAILY_RETRY_DELAY + timedelta(minutes=2))
        await _flush(hass)
        assert calls == after_startup + 1
