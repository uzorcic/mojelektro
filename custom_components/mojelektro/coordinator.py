"""Polling coordinator for one Moj Elektro metering point."""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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
    DOMAIN,
    EIMM_PATTERN,
    READING_TYPE_EXPORT_STATE,
    READING_TYPE_IMPORT_STATE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
_EIMM_RE = re.compile(EIMM_PATTERN)


class MojElektroCoordinator(DataUpdateCoordinator[BalanceSnapshot]):
    """Fetch daily register states and compute the yearly and monthly balance.

    Each update reads the calendar date from Home Assistant's time zone, so the
    baseline moves to 1 January as soon as that date arrives. Readings are not
    cached across updates.
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
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.usage_point: str = entry.data[CONF_USAGE_POINT]
        self.meter: MeterInfo | None = None
        self._import_type = READING_TYPE_IMPORT_STATE
        self._export_type = READING_TYPE_EXPORT_STATE
        self._types_loaded = False
        self._meter_loaded = False

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
