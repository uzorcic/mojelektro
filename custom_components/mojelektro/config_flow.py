"""Config flow for Moj Elektro."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import (
    MeterInfo,
    MojElektroAuthError,
    MojElektroClient,
    MojElektroConnectionError,
    MojElektroError,
    MojElektroNotFoundError,
    MojElektroTimeoutError,
)
from .const import CONF_MANUAL, CONF_TOKEN, CONF_USAGE_POINT, DOMAIN

_LOGGER = logging.getLogger(__name__)

_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)
_METER_SCHEMA = vol.Schema({vol.Required(CONF_USAGE_POINT): str})


class MojElektroConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for an API token and a metering point (EIMM or GSRN)."""

    VERSION = 1

    def __init__(self) -> None:
        self._token: str | None = None
        self._discovered: list[str] = []
        self._reauth_entry: ConfigEntry | None = None
        self._usage_point: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and check the API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input[CONF_TOKEN]).strip()
            if not token:
                errors["base"] = "invalid_auth"
            else:
                client = self._client(token)
                try:
                    await client.get_reading_types()
                except MojElektroAuthError:
                    errors["base"] = "invalid_auth"
                except (MojElektroTimeoutError, MojElektroConnectionError):
                    errors["base"] = "cannot_connect"
                except MojElektroError:
                    errors["base"] = "unknown"
                else:
                    self._token = token
                    try:
                        self._discovered = await client.discover_usage_points()
                    except MojElektroAuthError:
                        errors["base"] = "invalid_auth"
                    except MojElektroError:
                        _LOGGER.debug(
                            "Souporaba discovery failed; asking for the usage point",
                            exc_info=True,
                        )
                        self._discovered = []
                    if not errors:
                        if self._discovered:
                            return await self.async_step_select_meter()
                        return await self.async_step_meter()
        return self.async_show_form(
            step_id="user",
            data_schema=_TOKEN_SCHEMA,
            errors=errors,
        )

    async def async_step_select_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer metering points found via GET /souporaba, or manual entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = str(user_input[CONF_USAGE_POINT]).strip()
            if choice == CONF_MANUAL:
                return await self.async_step_meter()
            info, error = await self._validate(choice)
            if info is not None and error is None:
                return await self._async_create(choice, info)
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="select_meter",
            data_schema=self._select_schema(),
            errors=errors,
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for an EIMM or GSRN and validate it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            usage_point = str(user_input[CONF_USAGE_POINT]).strip()
            info, error = await self._validate(usage_point)
            if info is not None and error is None:
                return await self._async_create(usage_point, info)
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="meter",
            data_schema=_METER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start a token refresh after HTTP 401/403."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")
        self._reauth_entry = entry
        stored = entry_data.get(CONF_USAGE_POINT, entry.data[CONF_USAGE_POINT])
        self._usage_point = str(stored)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the rejected token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input[CONF_TOKEN]).strip()
            client = self._client(token)
            try:
                await client.get_reading_types()
            except MojElektroAuthError:
                errors["base"] = "invalid_auth"
            except (MojElektroTimeoutError, MojElektroConnectionError):
                errors["base"] = "cannot_connect"
            except MojElektroError:
                errors["base"] = "unknown"
            else:
                entry = self._reauth_entry
                if entry is None:
                    return self.async_abort(reason="unknown")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_TOKEN: token},
                    reason="reauth_successful",
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"usage_point": self._usage_point or ""},
        )

    async def _validate(self, usage_point: str) -> tuple[MeterInfo | None, str | None]:
        token = self._token
        if not token:
            return None, "invalid_auth"
        client = self._client(token)
        try:
            info = await client.validate_usage_point(
                usage_point,
                today=dt_util.now().date(),
            )
        except MojElektroAuthError:
            return None, "invalid_auth"
        except MojElektroNotFoundError:
            return None, "invalid_usage_point"
        except (MojElektroTimeoutError, MojElektroConnectionError):
            return None, "cannot_connect"
        except MojElektroError:
            return None, "unknown"
        return info, None

    async def _async_create(self, usage_point: str, info: MeterInfo) -> ConfigFlowResult:
        await self.async_set_unique_id(usage_point)
        self._abort_if_unique_id_configured()
        title = f"Moj Elektro {info.name}" if info.name else f"Moj Elektro {usage_point}"
        return self.async_create_entry(
            title=title,
            data={
                CONF_TOKEN: self._token,
                CONF_USAGE_POINT: usage_point,
            },
        )

    @callback
    def _select_schema(self) -> vol.Schema:
        options = [
            SelectOptionDict(value=item, label=item) for item in self._discovered
        ]
        options.append(SelectOptionDict(value=CONF_MANUAL, label="Enter manually"))
        return vol.Schema(
            {
                vol.Required(CONF_USAGE_POINT): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="meter_choice",
                    )
                )
            }
        )

    def _client(self, token: str) -> MojElektroClient:
        return MojElektroClient(async_get_clientsession(self.hass), token)
