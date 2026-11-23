"""Pytest fixtures for the Moj Elektro integration."""

from __future__ import annotations

import aiohttp.resolver
import pytest

from helpers import load_fixture

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _avoid_aiodns_shutdown_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Home Assistant's client session uses aiodns, whose shutdown thread
    outlives the test. The threaded resolver does not start that thread.
    """
    from homeassistant.helpers import aiohttp_client

    monkeypatch.setattr(aiohttp.resolver, "AsyncResolver", aiohttp.resolver.ThreadedResolver)
    monkeypatch.setattr(aiohttp_client, "AsyncResolver", aiohttp.resolver.ThreadedResolver)


@pytest.fixture
def meter_readings():
    """MeterReadings document with A+_T0 and A-_T0 daily states."""
    return load_fixture("meter_readings.json")


@pytest.fixture
def reading_types():
    """Subset of GET /reading-type, including the OpenAPI 15-minute example."""
    return load_fixture("reading_types.json")


@pytest.fixture
def merilno_mesto():
    """MerilnoMestoPolno document for a fictional metering point."""
    return load_fixture("merilno_mesto.json")
