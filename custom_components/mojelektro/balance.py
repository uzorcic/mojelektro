"""Pure parsing and balance math for Moj Elektro meter readings.

Yearly and monthly energy is the difference of cumulative 24-hour register
states (vrsta STANJE): latest reading minus the reading at the start of the
period. Values are kWh, matching the ``/reading-type`` descriptions
(``24 urno stanje, A+, kWh``). The IEC unit code 72 is Wh and the multiplier
3 is kilo, which is the same quantity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import tzinfo as dt_tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .const import (
    MAX_RANGE_DAYS,
    OZNAKA_EXPORT_STATE,
    OZNAKA_IMPORT_STATE,
)

_QUANTUM = Decimal("0.001")


@dataclass(frozen=True)
class Reading:
    """One interval reading from ``MeterReadings.intervalBlocks``."""

    timestamp: datetime
    value_kwh: Decimal
    qualities: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegisterDelta:
    """Energy between two cumulative register states."""

    reading_type: str
    kwh: Decimal | None
    baseline_date: date | None
    latest_date: date | None
    baseline_kwh: Decimal | None
    latest_kwh: Decimal | None
    register_reset: bool = False
    latest_qualities: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeriodFigures:
    """Import, export and net for one calendar period."""

    period_start: date
    consumption: RegisterDelta
    delivered: RegisterDelta
    net_kwh: Decimal | None


@dataclass(frozen=True)
class BalanceSnapshot:
    """Coordinator payload: this calendar year and this calendar month."""

    usage_point: str
    today: date
    yearly: PeriodFigures
    monthly: PeriodFigures
    meter_name: str | None = None
    meter_address: str | None = None
    meter_gsrn: str | None = None
    import_reading_type: str = ""
    export_reading_type: str = ""


def select_registers(
    catalog: Any,
    *,
    fallback_import: str,
    fallback_export: str,
) -> tuple[str, str]:
    """Pick A+_T0 and A-_T0 reading types from ``GET /reading-type``.

    Entries must be cumulative states (``vrsta`` STANJE, or omitted). Quantity
    registers such as 15-minute ``A+`` are ignored. Missing entries keep the
    built-in codes.
    """
    found: dict[str, str] = {}
    if isinstance(catalog, list):
        for item in catalog:
            if not isinstance(item, dict):
                continue
            oznaka = item.get("oznaka")
            if oznaka not in (OZNAKA_IMPORT_STATE, OZNAKA_EXPORT_STATE):
                continue
            if item.get("vrsta") not in (None, "STANJE"):
                continue
            reading_type = item.get("readingType")
            if isinstance(reading_type, str) and reading_type:
                found[oznaka] = reading_type
    return (
        found.get(OZNAKA_IMPORT_STATE, fallback_import),
        found.get(OZNAKA_EXPORT_STATE, fallback_export),
    )


def plan_query_windows(
    today: date, *, max_days: int = MAX_RANGE_DAYS
) -> list[tuple[date, date]]:
    """Return inclusive date windows that stay inside the API span limit.

    The year needs the 1 January register and the latest register. Those two
    dates are often more than 35 days apart, so the year is split into a short
    January window and the current month (always within 35 days).
    """
    year_start = date(today.year, 1, 1)
    month_start = date(today.year, today.month, 1)
    if (today - year_start).days <= max_days:
        return [(year_start, today)]

    baseline_end = min(year_start + timedelta(days=6), today)
    recent_start = month_start
    if (today - recent_start).days > max_days:
        recent_start = today - timedelta(days=max_days)

    windows: list[tuple[date, date]] = [(year_start, baseline_end)]
    if recent_start > baseline_end:
        windows.append((recent_start, today))
    elif today > baseline_end:
        windows.append((baseline_end, today))
    return windows


def parse_meter_readings(payload: Any) -> dict[str, list[Reading]]:
    """Parse a ``MeterReadings`` document into readings keyed by reading type.

    ``ReadingValue`` is a string. An empty or missing ``readingQualities`` list
    means the reading is valid; flagged readings are kept and the flags are
    stored on the reading.
    """
    if not isinstance(payload, dict):
        return {}
    blocks = payload.get("intervalBlocks") or []
    if not isinstance(blocks, list):
        return {}

    parsed: dict[str, list[Reading]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        reading_type = block.get("readingType")
        items = block.get("intervalReadings") or []
        if not isinstance(reading_type, str) or not reading_type:
            continue
        if not isinstance(items, list):
            continue
        readings = [reading for item in items if (reading := _parse_reading(item))]
        if readings:
            parsed.setdefault(reading_type, []).extend(readings)
    return {key: _dedupe(value) for key, value in parsed.items()}


def merge_readings(
    chunks: Sequence[Mapping[str, Sequence[Reading]]],
) -> dict[str, list[Reading]]:
    """Merge readings from several date windows, keeping the latest duplicate."""
    merged: dict[str, list[Reading]] = {}
    for chunk in chunks:
        for reading_type, readings in chunk.items():
            merged.setdefault(reading_type, []).extend(readings)
    return {key: _dedupe(value) for key, value in merged.items()}


def compute_balance(
    readings_by_type: Mapping[str, Sequence[Reading]],
    *,
    usage_point: str,
    import_type: str,
    export_type: str,
    today: date,
    tz: dt_tzinfo,
    meter_name: str | None = None,
    meter_address: str | None = None,
    meter_gsrn: str | None = None,
) -> BalanceSnapshot:
    """Build this year's and this month's balance in ``tz``.

    The calendar year is taken from ``today`` on every call, so 1 January
    starts a new baseline without any stored year.
    """
    imported = list(readings_by_type.get(import_type, ()))
    exported = list(readings_by_type.get(export_type, ()))
    year_start = date(today.year, 1, 1)
    month_start = date(today.year, today.month, 1)
    return BalanceSnapshot(
        usage_point=usage_point,
        today=today,
        yearly=_period(imported, exported, import_type, export_type, year_start, today, tz),
        monthly=_period(
            imported, exported, import_type, export_type, month_start, today, tz
        ),
        meter_name=meter_name,
        meter_address=meter_address,
        meter_gsrn=meter_gsrn,
        import_reading_type=import_type,
        export_reading_type=export_type,
    )


def local_date(timestamp: datetime, tz: dt_tzinfo) -> date:
    """Calendar date of a reading in the Home Assistant time zone."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=tz)
    return timestamp.astimezone(tz).date()


def _period(
    imported: Sequence[Reading],
    exported: Sequence[Reading],
    import_type: str,
    export_type: str,
    period_start: date,
    today: date,
    tz: dt_tzinfo,
) -> PeriodFigures:
    consumption = _delta(imported, import_type, period_start, today, tz)
    delivered = _delta(exported, export_type, period_start, today, tz)
    net: Decimal | None = None
    if consumption.kwh is not None and delivered.kwh is not None:
        net = _q(consumption.kwh - delivered.kwh)
    return PeriodFigures(
        period_start=period_start,
        consumption=consumption,
        delivered=delivered,
        net_kwh=net,
    )


def _delta(
    readings: Sequence[Reading],
    reading_type: str,
    period_start: date,
    today: date,
    tz: dt_tzinfo,
) -> RegisterDelta:
    window = [
        reading
        for reading in readings
        if period_start <= local_date(reading.timestamp, tz) <= today
    ]
    if not window:
        return _empty(reading_type)

    opening = min(window, key=lambda reading: reading.timestamp)
    closing = max(window, key=lambda reading: reading.timestamp)
    open_day = local_date(opening.timestamp, tz)
    close_day = local_date(closing.timestamp, tz)
    base = opening.value_kwh
    last = closing.value_kwh
    reset = last < base
    if reset:
        # A backwards register is a replacement or a glitch. Leave the sensor
        # unavailable instead of publishing a negative import or export.
        kwh: Decimal | None = None
    elif open_day == close_day and open_day != period_start:
        # A single snapshot after the period opened is not a delta yet.
        kwh = None
    else:
        kwh = _q(last - base)
    return RegisterDelta(
        reading_type=reading_type,
        kwh=kwh,
        baseline_date=open_day,
        latest_date=close_day,
        baseline_kwh=base,
        latest_kwh=last,
        register_reset=reset,
        latest_qualities=closing.qualities,
    )


def _empty(reading_type: str) -> RegisterDelta:
    return RegisterDelta(
        reading_type=reading_type,
        kwh=None,
        baseline_date=None,
        latest_date=None,
        baseline_kwh=None,
        latest_kwh=None,
    )


def _parse_reading(item: Any) -> Reading | None:
    if not isinstance(item, dict):
        return None
    raw_value = item.get("value")
    raw_timestamp = item.get("timestamp")
    if raw_value is None or not isinstance(raw_timestamp, str):
        return None
    try:
        value = _parse_value(raw_value)
        timestamp = datetime.fromisoformat(raw_timestamp)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if value is None:
        return None
    return Reading(
        timestamp=timestamp,
        value_kwh=value,
        qualities=_qualities(item.get("readingQualities")),
    )


def _parse_value(raw_value: Any) -> Decimal | None:
    if isinstance(raw_value, str):
        if not raw_value.strip():
            return None
        return Decimal(raw_value)
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return Decimal(raw_value)
    if isinstance(raw_value, float):
        return Decimal(str(raw_value))
    return None


def _qualities(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    flags: list[str] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("readingQualityType"), str):
            flags.append(item["readingQualityType"])
    return tuple(flags)


def _dedupe(readings: Sequence[Reading]) -> list[Reading]:
    by_timestamp: dict[datetime, Reading] = {}
    for reading in readings:
        by_timestamp[reading.timestamp] = reading
    return sorted(by_timestamp.values(), key=lambda reading: reading.timestamp)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
