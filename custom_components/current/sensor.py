import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CUSTOMER_ID, DOMAIN
from .coordinator import CurrentCoordinator

_LOGGER = logging.getLogger(__name__)


def _get_session_energy(data: dict) -> float | None:
    ongoing = data.get("ongoing") or []
    if not ongoing:
        return None
    return ongoing[0].get("TotalkWh")


def _get_session_duration(data: dict) -> int | None:
    ongoing = data.get("ongoing") or []
    if not ongoing:
        return None
    val = ongoing[0].get("DurationCharging")
    return int(val) if val is not None else None


def _get_history_sessions(data: dict) -> list:
    history = data.get("history") or {}
    if isinstance(history, list):
        return history
    return history.get("List") or []


@dataclass(frozen=True, kw_only=True)
class CurrentSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


SENSOR_DESCRIPTIONS: tuple[CurrentSensorEntityDescription, ...] = (
    CurrentSensorEntityDescription(
        key="status",
        name="Charger Status",
        icon="mdi:ev-station",
        value_fn=lambda data: (
            "charging"
            if data.get("ongoing")
            else (
                "available"
                if (data.get("chargers") or [{}])[0].get("IsPointActive")
                else "unavailable"
            )
        ),
    ),
    CurrentSensorEntityDescription(
        key="session_energy",
        name="Session Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_get_session_energy,
    ),
    CurrentSensorEntityDescription(
        key="session_duration",
        name="Charging Duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer",
        value_fn=_get_session_duration,
    ),
    CurrentSensorEntityDescription(
        key="last_session_cost",
        name="Last Session Cost",
        icon="mdi:cash",
        native_unit_of_measurement="NOK",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (_get_history_sessions(data) or [{}])[0].get("TotalPrice"),
    ),
    CurrentSensorEntityDescription(
        key="last_session_energy",
        name="Last Session Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (_get_history_sessions(data) or [{}])[0].get("TotalkWH"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        CurrentSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class CurrentSensor(CoordinatorEntity[CurrentCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: CurrentSensorEntityDescription

    def __init__(
        self,
        coordinator: CurrentCoordinator,
        entry: ConfigEntry,
        description: CurrentSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"current_{entry.data[CONF_CUSTOMER_ID]}_{description.key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.data[CONF_CUSTOMER_ID]))},
            name="CURRENT EV Charger",
            manufacturer="CURRENT",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})
