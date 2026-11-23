"""Energy balance sensors for one Moj Elektro metering point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .balance import BalanceSnapshot, PeriodFigures, RegisterDelta
from .const import ATTRIBUTION, DOMAIN, MANUFACTURER, PORTAL_URL
from .coordinator import MojElektroCoordinator

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def usage_point_slug(usage_point: str) -> str:
    """Stable entity-id fragment. ``3-123456`` becomes ``3_123456``."""
    return _NON_SLUG.sub("_", usage_point.lower()).strip("_")


def sensor_entity_id(usage_point: str, object_id: str) -> str:
    """Entity id pinned at creation so it does not follow the UI language."""
    return f"sensor.moj_elektro_{usage_point_slug(usage_point)}_{object_id}"


@dataclass(frozen=True, kw_only=True)
class _SensorSpec:
    description: SensorEntityDescription
    period: str
    field: str
    object_id: str


def _description(
    key: str,
    name: str,
    *,
    state_class: SensorStateClass | None,
    icon: str,
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        name=name,
        device_class=SensorDeviceClass.ENERGY,
        state_class=state_class,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon=icon,
    )


# Net balance can be negative (more energy delivered than received). Home
# Assistant only allows state_class total / total_increasing on device_class
# energy, so the balance sensors keep the energy device class and omit
# state_class. Import and export stay non-negative and use total, which
# records the reset on 1 January and on the 1st of the month.
SENSOR_SPECS: tuple[_SensorSpec, ...] = (
    _SensorSpec(
        description=_description(
            "yearly_consumption",
            "Yearly imported energy",
            state_class=SensorStateClass.TOTAL,
            icon="mdi:home-import-outline",
        ),
        period="yearly",
        field="consumption",
        object_id="letna_prejeta_energija",
    ),
    _SensorSpec(
        description=_description(
            "yearly_delivered",
            "Yearly exported energy",
            state_class=SensorStateClass.TOTAL,
            icon="mdi:transmission-tower-export",
        ),
        period="yearly",
        field="delivered",
        object_id="letna_oddana_energija",
    ),
    _SensorSpec(
        description=_description(
            "yearly_balance",
            "Yearly energy balance",
            state_class=None,
            icon="mdi:scale-balance",
        ),
        period="yearly",
        field="net",
        object_id="letna_bilanca",
    ),
    _SensorSpec(
        description=_description(
            "monthly_consumption",
            "Monthly imported energy",
            state_class=SensorStateClass.TOTAL,
            icon="mdi:home-import-outline",
        ),
        period="monthly",
        field="consumption",
        object_id="mesecna_prejeta_energija",
    ),
    _SensorSpec(
        description=_description(
            "monthly_delivered",
            "Monthly exported energy",
            state_class=SensorStateClass.TOTAL,
            icon="mdi:transmission-tower-export",
        ),
        period="monthly",
        field="delivered",
        object_id="mesecna_oddana_energija",
    ),
    _SensorSpec(
        description=_description(
            "monthly_balance",
            "Monthly energy balance",
            state_class=None,
            icon="mdi:scale-balance",
        ),
        period="monthly",
        field="net",
        object_id="mesecna_bilanca",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the balance sensors for this metering point."""
    coordinator: MojElektroCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        MojElektroSensor(coordinator, spec) for spec in SENSOR_SPECS
    )


class MojElektroSensor(CoordinatorEntity[MojElektroCoordinator], SensorEntity):
    """One kWh figure from the latest balance snapshot."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: MojElektroCoordinator,
        spec: _SensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = spec.description
        self._period = spec.period
        self._field = spec.field
        usage_point = coordinator.usage_point
        self._attr_unique_id = f"{usage_point}_{spec.description.key}"
        self.entity_id = sensor_entity_id(usage_point, spec.object_id)
        meter = coordinator.meter
        model = meter.name if meter and meter.name else "Merilno mesto"
        device_info: DeviceInfo = {
            "identifiers": {(DOMAIN, usage_point)},
            "name": f"Moj Elektro {usage_point}",
            "manufacturer": MANUFACTURER,
            "model": model,
            "configuration_url": PORTAL_URL,
        }
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return super().available and self._kwh() is not None

    @property
    def native_value(self) -> float | None:
        kwh = self._kwh()
        if kwh is None:
            return None
        return float(kwh)

    @property
    def icon(self) -> str | None:
        if self._field != "net":
            return super().icon
        value = self.native_value
        if isinstance(value, (int, float)):
            if value < 0:
                return "mdi:solar-power"
            if value > 0:
                return "mdi:home-import-outline"
        return "mdi:scale-balance"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        usage_point = self.coordinator.usage_point
        if data is None:
            return {"usage_point": usage_point}
        period = _period(data, self._period)
        attributes: dict[str, Any] = {
            "usage_point": data.usage_point,
            "period_start": period.period_start.isoformat(),
            "as_of": data.today.isoformat(),
        }
        if data.meter_name:
            attributes["meter_name"] = data.meter_name
        if data.meter_address:
            attributes["meter_address"] = data.meter_address
        if data.meter_gsrn:
            attributes["meter_gsrn"] = data.meter_gsrn
        if self._field == "net":
            attributes["last_reading_date"] = _earlier(
                period.consumption.latest_date,
                period.delivered.latest_date,
            )
            attributes["consumption_kwh"] = _num(period.consumption.kwh)
            attributes["delivered_kwh"] = _num(period.delivered.kwh)
            attributes["consumption_last_reading_date"] = _iso(
                period.consumption.latest_date
            )
            attributes["delivered_last_reading_date"] = _iso(period.delivered.latest_date)
            attributes["consumption_baseline_date"] = _iso(period.consumption.baseline_date)
            attributes["delivered_baseline_date"] = _iso(period.delivered.baseline_date)
        else:
            register = _register(period, self._field)
            attributes["last_reading_date"] = _iso(register.latest_date)
            attributes["baseline_date"] = _iso(register.baseline_date)
            attributes["baseline_register_kwh"] = _num(register.baseline_kwh)
            attributes["latest_register_kwh"] = _num(register.latest_kwh)
            attributes["reading_type"] = register.reading_type
            if register.register_reset:
                attributes["register_reset"] = True
            if register.latest_qualities:
                attributes["last_reading_qualities"] = list(register.latest_qualities)
        return {key: value for key, value in attributes.items() if value is not None}

    def _kwh(self) -> Decimal | None:
        data = self.coordinator.data
        if data is None:
            return None
        period = _period(data, self._period)
        if self._field == "net":
            return period.net_kwh
        return _register(period, self._field).kwh


def _period(data: BalanceSnapshot, period: str) -> PeriodFigures:
    if period == "yearly":
        return data.yearly
    return data.monthly


def _register(period: PeriodFigures, field: str) -> RegisterDelta:
    if field == "delivered":
        return period.delivered
    return period.consumption


def _iso(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _num(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _earlier(left: date | None, right: date | None) -> str | None:
    if left and right:
        return min(left, right).isoformat()
    if left:
        return left.isoformat()
    if right:
        return right.isoformat()
    return None
