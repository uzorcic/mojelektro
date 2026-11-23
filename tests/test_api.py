"""HTTP client: auth header, reading-type options, and error payloads."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import aiohttp
import pytest

from custom_components.mojelektro.api import (
    MojElektroApiError,
    MojElektroAuthError,
    MojElektroClient,
    MojElektroConnectionError,
    MojElektroNotFoundError,
    MojElektroTimeoutError,
    error_message,
    extract_usage_points,
    meter_info_from_merilno_mesto,
)
from custom_components.mojelektro.const import (
    API_TOKEN_HEADER,
    READING_TYPE_EXPORT_STATE,
    READING_TYPE_IMPORT_STATE,
)

from helpers import load_fixture

TOKEN = "test-token"


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class RecordingSession:
    """Stand-in for aiohttp.ClientSession that records the outgoing call."""

    def __init__(self, status: int = 200, body: str = "{}") -> None:
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.status, self.body)


class TimeoutSession:
    def get(self, *args: object, **kwargs: object) -> _Response:
        raise asyncio.TimeoutError()


class DownSession:
    def get(self, *args: object, **kwargs: object) -> _Response:
        raise aiohttp.ClientConnectionError("connection refused")


@pytest.mark.asyncio
async def test_meter_readings_request_uses_token_and_reading_types() -> None:
    body = json.dumps(load_fixture("meter_readings.json"))
    session = RecordingSession(status=200, body=body)
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/mojelektro/v1")

    parsed = await client.get_meter_readings(
        "3-123456",
        date(2026, 1, 1),
        date(2026, 1, 7),
        [READING_TYPE_IMPORT_STATE, READING_TYPE_EXPORT_STATE],
    )

    call = session.calls[0]
    assert call["url"] == "https://api.example.test/mojelektro/v1/meter-readings"
    headers = call["headers"]
    assert headers[API_TOKEN_HEADER] == TOKEN
    assert headers["Accept"] == "application/json"
    assert TOKEN not in call["url"]
    params = call["params"]
    assert ("usagePoint", "3-123456") in params
    assert ("startTime", "2026-01-01") in params
    assert ("endTime", "2026-01-07") in params
    assert ("option", f"ReadingType={READING_TYPE_IMPORT_STATE}") in params
    assert ("option", f"ReadingType={READING_TYPE_EXPORT_STATE}") in params
    assert str(parsed[READING_TYPE_IMPORT_STATE][0].value_kwh) == "1000.000"
    assert str(parsed[READING_TYPE_EXPORT_STATE][-1].value_kwh) == "1800.250"


@pytest.mark.asyncio
async def test_auth_error_uses_napaka_schema() -> None:
    session = RecordingSession(
        status=401,
        body=json.dumps(
            {"koda": "E_1_000", "opis": "Manjkajo avtorizacijski podatki"}
        ),
    )
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroAuthError, match="Manjkajo avtorizacijski podatki") as caught:
        await client.get_reading_types()
    assert caught.value.status == 401


@pytest.mark.asyncio
async def test_forbidden_is_auth_error_and_souporaba_403_is_empty() -> None:
    session = RecordingSession(status=403, body=json.dumps({"koda": "E_1_003", "opis": "Ni dostopa"}))
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroAuthError):
        await client.get_reading_types()

    denied = RecordingSession(status=403, body="{}")
    client = MojElektroClient(denied, TOKEN, base_url="https://api.example.test/v1")
    assert await client.discover_usage_points() == []
    assert denied.calls[0]["url"].endswith("/souporaba")


@pytest.mark.asyncio
async def test_interval_error_payload_and_not_found() -> None:
    session = RecordingSession(
        status=400,
        body=json.dumps(load_fixture("error_interval.json")),
    )
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroApiError, match="35 dni") as caught:
        await client.get_reading_types()
    assert caught.value.status == 400

    missing = RecordingSession(status=404, body=json.dumps({"koda": "E_4_404", "opis": "Ni najdeno"}))
    client = MojElektroClient(missing, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroNotFoundError, match="Ni najdeno"):
        await client.get_merilno_mesto("3-123456")


@pytest.mark.asyncio
async def test_timeout_and_connection_errors() -> None:
    client = MojElektroClient(TimeoutSession(), TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroTimeoutError):
        await client.get_reading_types()

    client = MojElektroClient(DownSession(), TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroConnectionError):
        await client.get_reading_types()


@pytest.mark.asyncio
async def test_client_refuses_a_span_longer_than_35_days_without_calling() -> None:
    session = RecordingSession()
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroApiError, match="35 days"):
        await client.get_meter_readings(
            "3-123456",
            date(2026, 1, 1),
            date(2026, 2, 6),
            [READING_TYPE_IMPORT_STATE],
        )
    assert session.calls == []


def test_error_message_shapes() -> None:
    wrapped = load_fixture("error_interval.json")
    assert error_message(wrapped).startswith("E_4_000:")
    assert error_message({"opis": "Manjkajo avtorizacijski podatki"}) == (
        "Manjkajo avtorizacijski podatki"
    )
    assert error_message(["nope"]) is None


def test_extract_usage_points_and_meter_info() -> None:
    assert extract_usage_points(load_fixture("souporaba.json")) == [
        "3-123456",
        "4-654321",
    ]
    nested = [
        {
            "eiMmOddajnika": {"enotniIdentifikatorMerilnegaMesta": "3-123456"},
            "eiMmPrejemnika": "3-123456",
        }
    ]
    assert extract_usage_points(nested) == ["3-123456"]

    info = meter_info_from_merilno_mesto(load_fixture("merilno_mesto.json"), "3-123456")
    assert info.name == "Hiša"
    assert info.address == "Slovenska ulica 1, Ljubljana"
    assert info.gsrn == "383000000000000000"


@pytest.mark.asyncio
async def test_invalid_json_and_invalid_usage_point() -> None:
    session = RecordingSession(status=200, body="not-json")
    client = MojElektroClient(session, TOKEN, base_url="https://api.example.test/v1")
    with pytest.raises(MojElektroApiError, match="Invalid JSON"):
        await client.get_reading_types()

    with pytest.raises(MojElektroNotFoundError):
        await client.validate_usage_point("bad id", today=date(2026, 6, 15))
    assert len(session.calls) == 1
