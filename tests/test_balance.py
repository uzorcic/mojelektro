"""Balance math and MeterReadings parsing."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.mojelektro.balance import (
    Reading,
    compute_balance,
    parse_meter_readings,
    plan_query_windows,
    select_registers,
)
from custom_components.mojelektro.const import (
    READING_TYPE_EXPORT_15MIN,
    READING_TYPE_EXPORT_STATE,
    READING_TYPE_IMPORT_15MIN,
    READING_TYPE_IMPORT_STATE,
)

from helpers import load_fixture

TZ = ZoneInfo("Europe/Ljubljana")
USAGE = "3-123456"


def _reading(timestamp: str, value: str, qualities: tuple[str, ...] = ()) -> Reading:
    return Reading(
        timestamp=datetime.fromisoformat(timestamp),
        value_kwh=Decimal(value),
        qualities=qualities,
    )


def test_parses_spec_meter_readings_and_yearly_balance(meter_readings) -> None:
    """A+_T0 / A-_T0 states from a MeterReadings document become kWh deltas."""
    parsed = parse_meter_readings(meter_readings)
    assert set(parsed) == {READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE}
    assert parsed[READING_TYPE_IMPORT_STATE][0].value_kwh == Decimal("1000.000")
    assert parsed[READING_TYPE_IMPORT_STATE][0].qualities == ()

    snapshot = compute_balance(
        parsed,
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 6, 15),
        tz=TZ,
    )

    assert snapshot.yearly.consumption.kwh == Decimal("1450.500")
    assert snapshot.yearly.delivered.kwh == Decimal("1600.250")
    assert snapshot.yearly.net_kwh == Decimal("-149.750")
    assert snapshot.yearly.consumption.baseline_date == date(2026, 1, 1)
    assert snapshot.yearly.consumption.latest_date == date(2026, 6, 14)
    assert snapshot.monthly.consumption.kwh == Decimal("250.500")
    assert snapshot.monthly.delivered.kwh == Decimal("300.250")
    assert snapshot.monthly.net_kwh == Decimal("-49.750")
    assert snapshot.monthly.period_start == date(2026, 6, 1)


def test_parses_openapi_datetime_example() -> None:
    """DateTime example from the OpenAPI schema is a valid reading timestamp."""
    payload = {
        "usagePoint": USAGE,
        "messageCreated": "2023-12-19T00:00:15+02:00",
        "intervalBlocks": [
            {
                "readingType": READING_TYPE_IMPORT_15MIN,
                "intervalReadings": [
                    {
                        "timestamp": "2023-12-19T00:00:15+02:00",
                        "value": "1.250",
                        "readingQualities": [],
                    }
                ],
            }
        ],
    }
    parsed = parse_meter_readings(payload)
    reading = parsed[READING_TYPE_IMPORT_15MIN][0]
    assert reading.timestamp == datetime.fromisoformat("2023-12-19T00:00:15+02:00")
    assert reading.value_kwh == Decimal("1.250")
    assert reading.qualities == ()


def test_missing_qualities_stay_valid_and_bad_values_are_skipped() -> None:
    payload = {
        "intervalBlocks": [
            {
                "readingType": READING_TYPE_IMPORT_STATE,
                "intervalReadings": [
                    {"timestamp": "2026-01-01T00:00:00+01:00", "value": "10.000"},
                    {"timestamp": "2026-01-02T00:00:00+01:00", "value": "abc"},
                    {
                        "timestamp": "2026-01-03T00:00:00+01:00",
                        "value": "12.500",
                        "readingQualities": [{"readingQualityType": "3.8.0"}],
                    },
                ],
            }
        ]
    }
    parsed = parse_meter_readings(payload)
    readings = parsed[READING_TYPE_IMPORT_STATE]
    assert [reading.value_kwh for reading in readings] == [
        Decimal("10.000"),
        Decimal("12.500"),
    ]
    assert readings[0].qualities == ()
    assert readings[1].qualities == ("3.8.0",)


def test_year_rollover_ignores_previous_calendar_year() -> None:
    imported = [
        _reading("2026-01-01T00:00:00+01:00", "1000.000"),
        _reading("2026-12-31T00:00:00+01:00", "5000.000"),
        _reading("2027-01-01T00:00:00+01:00", "5100.000"),
        _reading("2027-01-10T00:00:00+01:00", "5300.000"),
    ]
    exported = [
        _reading("2026-12-31T00:00:00+01:00", "800.000"),
        _reading("2027-01-01T00:00:00+01:00", "900.000"),
        _reading("2027-01-10T00:00:00+01:00", "950.000"),
    ]
    snapshot = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: imported,
            READING_TYPE_EXPORT_STATE: exported,
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2027, 1, 10),
        tz=TZ,
    )
    assert snapshot.yearly.consumption.kwh == Decimal("200.000")
    assert snapshot.yearly.delivered.kwh == Decimal("50.000")
    assert snapshot.yearly.net_kwh == Decimal("150.000")
    assert snapshot.yearly.consumption.baseline_date == date(2027, 1, 1)
    assert snapshot.monthly.consumption.kwh == Decimal("200.000")


def test_december_timestamp_is_not_the_new_year_baseline() -> None:
    imported = [
        _reading("2025-12-31T23:30:00+01:00", "900.000"),
        _reading("2026-01-01T00:00:00+01:00", "1000.000"),
        _reading("2026-01-20T00:00:00+01:00", "1100.000"),
    ]
    exported = [
        _reading("2026-01-01T00:00:00+01:00", "10.000"),
        _reading("2026-01-20T00:00:00+01:00", "40.000"),
    ]
    snapshot = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: imported,
            READING_TYPE_EXPORT_STATE: exported,
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 1, 20),
        tz=TZ,
    )
    assert snapshot.yearly.consumption.kwh == Decimal("100.000")
    assert snapshot.yearly.consumption.baseline_kwh == Decimal("1000.000")


def test_single_opening_reading_is_zero_and_mid_period_snapshot_is_unknown() -> None:
    opening_only = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: [_reading("2026-01-01T00:00:00+01:00", "1000")],
            READING_TYPE_EXPORT_STATE: [_reading("2026-01-01T00:00:00+01:00", "200")],
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 1, 20),
        tz=TZ,
    )
    assert opening_only.yearly.consumption.kwh == Decimal("0.000")
    assert opening_only.yearly.net_kwh == Decimal("0.000")

    mid_period = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: [_reading("2026-06-14T00:00:00+02:00", "2450.500")],
            READING_TYPE_EXPORT_STATE: [_reading("2026-06-14T00:00:00+02:00", "1800.250")],
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 6, 15),
        tz=TZ,
    )
    assert mid_period.yearly.consumption.kwh is None
    assert mid_period.yearly.net_kwh is None


def test_register_reset_and_missing_export() -> None:
    reset = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: [
                _reading("2026-01-01T00:00:00+01:00", "500"),
                _reading("2026-06-14T00:00:00+02:00", "10"),
            ],
            READING_TYPE_EXPORT_STATE: [
                _reading("2026-01-01T00:00:00+01:00", "20"),
                _reading("2026-06-14T00:00:00+02:00", "80"),
            ],
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 6, 15),
        tz=TZ,
    )
    assert reset.yearly.consumption.kwh is None
    assert reset.yearly.consumption.register_reset is True
    assert reset.yearly.delivered.kwh == Decimal("60.000")
    assert reset.yearly.net_kwh is None

    missing_export = compute_balance(
        {
            READING_TYPE_IMPORT_STATE: [
                _reading("2026-01-01T00:00:00+01:00", "1000"),
                _reading("2026-06-14T00:00:00+02:00", "1100"),
            ]
        },
        usage_point=USAGE,
        import_type=READING_TYPE_IMPORT_STATE,
        export_type=READING_TYPE_EXPORT_STATE,
        today=date(2026, 6, 15),
        tz=TZ,
    )
    assert missing_export.yearly.consumption.kwh == Decimal("100.000")
    assert missing_export.yearly.delivered.kwh is None
    assert missing_export.yearly.net_kwh is None


def test_query_windows_stay_within_35_days() -> None:
    assert plan_query_windows(date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    assert plan_query_windows(date(2026, 1, 20)) == [
        (date(2026, 1, 1), date(2026, 1, 20))
    ]
    assert plan_query_windows(date(2026, 2, 5)) == [
        (date(2026, 1, 1), date(2026, 2, 5))
    ]
    assert plan_query_windows(date(2026, 2, 6)) == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 2, 1), date(2026, 2, 6)),
    ]
    assert plan_query_windows(date(2026, 6, 15)) == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 6, 1), date(2026, 6, 15)),
    ]
    assert plan_query_windows(date(2024, 2, 6)) == [
        (date(2024, 1, 1), date(2024, 1, 7)),
        (date(2024, 2, 1), date(2024, 2, 6)),
    ]
    for today in (
        date(2026, 1, 20),
        date(2026, 6, 15),
        date(2026, 11, 23),
        date(2027, 1, 10),
    ):
        for start, end in plan_query_windows(today):
            assert 0 <= (end - start).days <= 35


def test_select_registers_prefers_daily_states(reading_types) -> None:
    selected = select_registers(
        reading_types,
        fallback_import="fallback-import",
        fallback_export="fallback-export",
    )
    assert selected == (READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE)
    assert READING_TYPE_IMPORT_15MIN not in selected
    assert READING_TYPE_EXPORT_15MIN not in selected


def test_select_registers_falls_back_when_catalog_has_no_states() -> None:
    catalog = load_fixture("reading_types.json")
    quantities = [item for item in catalog if item["vrsta"] == "KOLICINA"]
    assert select_registers(
        quantities,
        fallback_import=READING_TYPE_IMPORT_STATE,
        fallback_export=READING_TYPE_EXPORT_STATE,
    ) == (READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE)
    assert select_registers(
        {"not": "a list"},
        fallback_import="a",
        fallback_export="b",
    ) == ("a", "b")
