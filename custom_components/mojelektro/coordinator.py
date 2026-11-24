"""Scheduled refresh for one Moj Elektro metering point."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MeterInfo,
    MojElektroAuthError,
    MojElektroClient,
    MojElektroError,
    meter_info_from_merilno_mesto,
)
from .balance import (
    BalanceSnapshot,
    compute_balance,
    merge_readings,
    plan_query_windows,
    select_registers,
)
from .const import (
    CONF_USAGE_POINT,
    DAILY_REFRESH_HOUR,
    DAILY_REFRESH_MINUTE,
    DAILY_REFRESH_SECOND,
    DAILY_RETRY_ATTEMPTS,
    DAILY_RETRY_DELAY,
    DOMAIN,
    EIMM_PATTERN,
    READING_TYPE_EXPORT_STATE,
    READING_TYPE_IMPORT_STATE,
)

_LOGGER = logging.getLogger(__name__)
_EIMM_RE = re.compile(EIMM_PATTERN)


class MojElektroCoordinator(DataUpdateCoordinator[BalanceSnapshot]):
    """Fetch daily register states and compute the yearly and monthly balance.

    ``update_interval`` stays ``None``. Home Assistant loads data once when the
    entry is set up, then ``async_track_time_change`` fetches again at 06:05 in
    the Home Assistant time zone. A failed daily fetch is retried a limited
    number of times and then waits until the next 06:05.

    Each update reads the calendar date from that time zone, so the baseline
    moves to 1 January as soon as that date arrives. Readings are not cached
    across updates.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MojElektroClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        self.usage_point: str = entry.data[CONF_USAGE_POINT]
        self.meter: MeterInfo | None = None
        self._import_type = READING_TYPE_IMPORT_STATE
        self._export_type = READING_TYPE_EXPORT_STATE
        self._types_loaded = False
        self._meter_loaded = False
        self._unsub_daily: CALLBACK_TYPE | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None
        self._retries_left = DAILY_RETRY_ATTEMPTS
        self._refresh_lock = asyncio.Lock()

    def async_schedule_daily_refresh(self) -> None:
        """Listen for 06:05 local time. Safe to call again after a reload."""
        self._cancel_daily()
        self._unsub_daily = async_track_time_change(
            self.hass,
            self._handle_daily_refresh,
            hour=DAILY_REFRESH_HOUR,
            minute=DAILY_REFRESH_MINUTE,
            second=DAILY_REFRESH_SECOND,
        )

    async def async_shutdown(self) -> None:
        """Cancel the daily fetch and any pending retry."""
        self._cancel_daily()
        self._cancel_retry()
        await super().async_shutdown()

    async def _handle_daily_refresh(self, _now: datetime) -> None:
        """Fetch at 06:05 and give this day a fresh retry budget."""
        async with self._refresh_lock:
            self._cancel_retry()
            self._retries_left = DAILY_RETRY_ATTEMPTS
        _LOGGER.debug("Daily Moj Elektro refresh for %s", self.usage_point)
        await self._refresh_or_retry()

    async def _handle_retry(self, _now: datetime) -> None:
        """One delayed attempt after a failed daily fetch."""
        self._unsub_retry = None
        await self._refresh_or_retry()

    async def _refresh_or_retry(self) -> None:
        """Refresh once. On failure, schedule a short retry while attempts remain."""
        async with self._refresh_lock:
            if self._shutdown_requested:
                return
            await self.async_refresh()
            if self.last_update_success or self._shutdown_requested:
                return
            if isinstance(self.last_exception, ConfigEntryAuthFailed):
                _LOGGER.debug(
                    "Moj Elektro rejected the token for %s; waiting for reauth",
                    self.usage_point,
                )
                return
            if self._retries_left <= 0:
                _LOGGER.warning(
                    "Moj Elektro refresh failed for %s; next attempt at %02d:%02d",
                    self.usage_point,
                    DAILY_REFRESH_HOUR,
                    DAILY_REFRESH_MINUTE,
                )
                return
            self._retries_left -= 1
            delay_minutes = int(DAILY_RETRY_DELAY.total_seconds() // 60)
            _LOGGER.warning(
                "Moj Elektro refresh failed for %s; retrying in %s minutes",
                self.usage_point,
                delay_minutes,
            )
            self._cancel_retry()
            self._unsub_retry = async_call_later(
                self.hass,
                DAILY_RETRY_DELAY,
                self._handle_retry,
            )

    def _cancel_daily(self) -> None:
        if self._unsub_daily is not None:
            self._unsub_daily()
            self._unsub_daily = None

    def _cancel_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    async def _async_update_data(self) -> BalanceSnapshot:
        try:
            await self._ensure_types()
            await self._ensure_meter()
            today = dt_util.now().date()
            tz = dt_util.get_default_time_zone()
            windows = plan_query_windows(today)
            reading_types = [self._import_type, self._export_type]
            chunks = []
            for start, end in windows:
                chunks.append(
                    await self.client.get_meter_readings(
                        self.usage_point,
                        start,
                        end,
                        reading_types,
                    )
                )
            readings = merge_readings(chunks)
        except MojElektroAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MojElektroError as err:
            raise UpdateFailed(str(err)) from err

        if not readings.get(self._import_type) and not readings.get(self._export_type):
            _LOGGER.warning(
                "Moj Elektro returned no A+_T0/A-_T0 states for %s between %s and %s",
                self.usage_point,
                windows[0][0].isoformat(),
                windows[-1][1].isoformat(),
            )

        meter = self.meter
        return compute_balance(
            readings,
            usage_point=self.usage_point,
            import_type=self._import_type,
            export_type=self._export_type,
            today=today,
            tz=tz,
            meter_name=meter.name if meter else None,
            meter_address=meter.address if meter else None,
            meter_gsrn=meter.gsrn if meter else None,
        )

    async def _ensure_types(self) -> None:
        if self._types_loaded:
            return
        try:
            catalog = await self.client.get_reading_types()
        except MojElektroAuthError:
            raise
        except MojElektroError as err:
            _LOGGER.warning(
                "Using built-in A+_T0/A-_T0 codes, reading-type catalog failed: %s",
                err,
            )
            return
        self._import_type, self._export_type = select_registers(
            catalog,
            fallback_import=READING_TYPE_IMPORT_STATE,
            fallback_export=READING_TYPE_EXPORT_STATE,
        )
        self._types_loaded = True

    async def _ensure_meter(self) -> None:
        if self._meter_loaded:
            return
        try:
            if _EIMM_RE.fullmatch(self.usage_point):
                payload = await self.client.get_merilno_mesto(self.usage_point)
                self.meter = meter_info_from_merilno_mesto(payload, self.usage_point)
            elif self.usage_point.isdigit():
                payload = await self.client.get_merilna_tocka(self.usage_point)
                name = payload.get("naziv") if isinstance(payload, dict) else None
                self.meter = MeterInfo(
                    usage_point=self.usage_point,
                    name=name.strip() if isinstance(name, str) and name.strip() else None,
                    address=None,
                    gsrn=self.usage_point,
                )
            else:
                self.meter = MeterInfo(
                    usage_point=self.usage_point,
                    name=None,
                    address=None,
                    gsrn=None,
                )
        except MojElektroAuthError:
            raise
        except MojElektroError as err:
            _LOGGER.debug("Meter metadata unavailable for %s: %s", self.usage_point, err)
            return
        self._meter_loaded = True
