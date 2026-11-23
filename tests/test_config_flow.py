"""Config flow: token, discovered metering points, and manual EIMM."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mojelektro.api import (
    MojElektroAuthError,
    MojElektroClient,
    MojElektroConnectionError,
    MojElektroNotFoundError,
)
from custom_components.mojelektro.const import CONF_MANUAL, CONF_TOKEN, CONF_USAGE_POINT, DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER

from helpers import load_fixture

TOKEN = "test-token"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_manual_eimm_creates_entry(hass) -> None:
    meter = load_fixture("merilno_mesto.json")
    with (
        patch(
            "custom_components.mojelektro.async_setup_entry",
            return_value=True,
        ),
        patch.object(
            MojElektroClient,
            "get_reading_types",
            new=AsyncMock(return_value=load_fixture("reading_types.json")),
        ),
        patch.object(MojElektroClient, "discover_usage_points", new=AsyncMock(return_value=[])),
        patch.object(MojElektroClient, "get_merilno_mesto", new=AsyncMock(return_value=meter)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: f"  {TOKEN}  "}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "meter"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USAGE_POINT: "3-123456"}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Moj Elektro Hiša"
    assert result["data"] == {CONF_TOKEN: TOKEN, CONF_USAGE_POINT: "3-123456"}
    assert result["result"].unique_id == "3-123456"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_invalid_token_and_connection_error(hass) -> None:
    with patch.object(
        MojElektroClient,
        "get_reading_types",
        new=AsyncMock(side_effect=MojElektroAuthError("no", status=401)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"

    with patch.object(
        MojElektroClient,
        "get_reading_types",
        new=AsyncMock(side_effect=MojElektroConnectionError("down")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_discovered_meter_and_manual_fallback(hass) -> None:
    meter = load_fixture("merilno_mesto.json")
    with (
        patch("custom_components.mojelektro.async_setup_entry", return_value=True),
        patch.object(
            MojElektroClient,
            "get_reading_types",
            new=AsyncMock(return_value=load_fixture("reading_types.json")),
        ),
        patch.object(
            MojElektroClient,
            "discover_usage_points",
            new=AsyncMock(return_value=["3-123456", "4-654321"]),
        ),
        patch.object(MojElektroClient, "get_merilno_mesto", new=AsyncMock(return_value=meter)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
        assert result["step_id"] == "select_meter"

        manual = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USAGE_POINT: CONF_MANUAL}
        )
        assert manual["step_id"] == "meter"

        created = await hass.config_entries.flow.async_configure(
            manual["flow_id"], {CONF_USAGE_POINT: "3-123456"}
        )
    assert created["type"] == "create_entry"
    assert created["data"][CONF_USAGE_POINT] == "3-123456"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_unknown_meter_and_duplicate(hass) -> None:
    with (
        patch.object(
            MojElektroClient,
            "get_reading_types",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(MojElektroClient, "discover_usage_points", new=AsyncMock(return_value=[])),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(side_effect=MojElektroNotFoundError("missing", status=404)),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USAGE_POINT: "3-999999"}
        )
    assert result["errors"]["base"] == "invalid_usage_point"

    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TOKEN: TOKEN, CONF_USAGE_POINT: "3-123456"},
        unique_id="3-123456",
    ).add_to_hass(hass)
    with (
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
        patch.object(MojElektroClient, "discover_usage_points", new=AsyncMock(return_value=[])),
        patch.object(
            MojElektroClient,
            "get_merilno_mesto",
            new=AsyncMock(return_value=load_fixture("merilno_mesto.json")),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USAGE_POINT: "3-123456"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_reauth_updates_token(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TOKEN: "old-token", CONF_USAGE_POINT: "3-123456"},
        unique_id="3-123456",
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.mojelektro.async_setup_entry", return_value=True),
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "new-token"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-token"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_gsrn_is_probed_with_meter_readings(hass) -> None:
    gsrn = "383000000000000000"
    with (
        patch("custom_components.mojelektro.async_setup_entry", return_value=True),
        patch.object(MojElektroClient, "get_reading_types", new=AsyncMock(return_value=[])),
        patch.object(MojElektroClient, "discover_usage_points", new=AsyncMock(return_value=[])),
        patch.object(MojElektroClient, "get_meter_readings", new=AsyncMock(return_value={})),
        patch.object(
            MojElektroClient,
            "get_merilna_tocka",
            new=AsyncMock(return_value={"gsrn": gsrn, "naziv": "Odjem", "vrsta": "OMTO"}),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USAGE_POINT: gsrn}
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_USAGE_POINT] == gsrn
    assert result["title"] == "Moj Elektro Odjem"
