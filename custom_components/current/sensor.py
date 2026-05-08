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
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
    unit_fn: Callable[[dict[str, Any]], str | None] | None = None


SENSOR_DESCRIPTIONS: tuple[CurrentSensorEntityDescription, ...] = (
    CurrentSensorEntityDescription(
        key="status",
        name="Charger Status",
        icon="mdi:ev-station",
        value_fn=lambda data: (
            "Charging"
            if data.get("ongoing") and (data["ongoing"][0].get("LivekW") or 0) > 0
            else "Standby"
            if data.get("ongoing")
            else "Available"
            if (data.get("chargers") or [{}])[0].get("IsPointActive")
            else "Unavailable"
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
        key="live_power",
        name="Live Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data["ongoing"][0].get("LivekW") if data.get("ongoing") else None
        ),
    ),
    CurrentSensorEntityDescription(
        key="live_current",
        name="Live Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data["ongoing"][0].get("Amps_Export") if data.get("ongoing") else None
        ),
    ),
    CurrentSensorEntityDescription(
        key="state_of_charge",
        name="State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data["ongoing"][0].get("Last_SoC") or None if data.get("ongoing") else None
        ),
    ),
    CurrentSensorEntityDescription(
        key="last_session_cost",
        name="Last Session Cost",
        icon="mdi:cash",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (_get_history_sessions(data) or [{}])[0].get("TotalPrice"),
        unit_fn=lambda data: (data.get("chargers") or [{}])[0].get("Currency"),
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
    chargers = coordinator.data.get("chargers") or []
    async_add_entities(
        CurrentSensor(coordinator, description, charger)
        for charger in chargers
        for description in SENSOR_DESCRIPTIONS
    )


class CurrentSensor(CoordinatorEntity[CurrentCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: CurrentSensorEntityDescription

    def __init__(
        self,
        coordinator: CurrentCoordinator,
        description: CurrentSensorEntityDescription,
        charger: dict,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._attr_unique_id = f"current_{self._charge_point_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self._charge_point_id))},
            name=charger.get("Name", "CURRENT EV Charger"),
            manufacturer="CURRENT",
        )

    def _filtered_data(self) -> dict:
        data = self.coordinator.data or {}
        cp_id = self._charge_point_id
        return {
            "chargers": [
                c for c in data.get("chargers") or []
                if c["FK_ChargePointID"] == cp_id
            ],
            "ongoing": [
                s for s in data.get("ongoing") or []
                if s.get("ChargingPointID") == cp_id
            ],
            "history": {
                "List": [
                    h for h in (data.get("history") or {}).get("List") or []
                    if h.get("ChargePointID") == cp_id
                ]
            },
        }

    @property
    def available(self) -> bool:
        return super().available and any(
            c["FK_ChargePointID"] == self._charge_point_id
            for c in (self.coordinator.data or {}).get("chargers") or []
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self._filtered_data())
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._filtered_data())
