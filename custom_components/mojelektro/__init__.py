"""Moj Elektro yearly energy balance for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MojElektroClient
from .const import CONF_TOKEN, DOMAIN
from .coordinator import MojElektroCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration from YAML. Configuration is done in the UI."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one metering point."""
    client = MojElektroClient(async_get_clientsession(hass), entry.data[CONF_TOKEN])
    coordinator = MojElektroCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    # Arm the daily 00:05 refresh only after the startup fetch succeeds.
    # A failed startup raises ConfigEntryNotReady and must not leave a listener
    # behind for the retry that Home Assistant will run later.
    coordinator.async_schedule_daily_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a metering point."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
