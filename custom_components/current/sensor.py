"""Sensors for CURRENT chargers."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from .statistics_import import parse_time

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
    return (data.get("history") or {}).get("List") or []


def _get_last_session_end(data: dict) -> datetime | None:
    """Return when the last completed session ended.

    The last session sensors are totals that start over with every session, so
    this is their last_reset. Without it, the recorder would read a smaller
    session following a bigger one as negative energy.
    """
    sessions = _get_history_sessions(data)
    if not sessions:
        return None
    return parse_time((sessions[0].get("Session") or {}).get("SessionEnd"))


def _get_live(data: dict) -> dict:
    """Return the charger's live readings.

    CURRENT reports power and current on the charger, not the session: while
    charging, the session's LivekW stays 0 and Amps_Export stays null.
    """
    charger = (data.get("chargers") or [{}])[0]
    return (charger.get("ExtraInformation") or {}).get("GenericPoint") or {}


def _get_status(data: dict) -> str:
    if data.get("ongoing"):
        return "Charging" if (_get_live(data).get("LivekW") or 0) > 0 else "Standby"
    if (data.get("chargers") or [{}])[0].get("IsPointActive"):
        return "Available"
    return "Unavailable"


@dataclass(frozen=True, kw_only=True)
class CurrentSensorEntityDescription(SensorEntityDescription):
    """Describes a CURRENT sensor and how to read its value."""

    value_fn: Callable[[dict[str, Any]], Any]
    unit_fn: Callable[[dict[str, Any]], str | None] | None = None
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    last_reset_fn: Callable[[dict[str, Any]], datetime | None] | None = None


SENSOR_DESCRIPTIONS: tuple[CurrentSensorEntityDescription, ...] = (
    CurrentSensorEntityDescription(
        key="status",
        translation_key="status",
        icon="mdi:ev-station",
        value_fn=_get_status,
        # CURRENT's own status, which has more states than the four above.
        attributes_fn=lambda data: {
            "current_status": _get_live(data).get("CurrentStatus"),
        },
    ),
    CurrentSensorEntityDescription(
        key="session_energy",
        translation_key="session_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_get_session_energy,
    ),
    CurrentSensorEntityDescription(
        key="session_duration",
        translation_key="session_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer",
        value_fn=_get_session_duration,
    ),
    CurrentSensorEntityDescription(
        key="live_power",
        translation_key="live_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_live(data).get("LivekW"),
    ),
    CurrentSensorEntityDescription(
        key="live_current",
        translation_key="live_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_live(data).get("LiveAmps"),
        attributes_fn=lambda data: {
            f"current_{phase.lower()}": _get_live(data).get(f"LiveAmps_{phase}")
            for phase in ("L1", "L2", "L3")
        },
    ),
    CurrentSensorEntityDescription(
        key="state_of_charge",
        translation_key="state_of_charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data["ongoing"][0].get("Last_SoC") or None if data.get("ongoing") else None
        ),
    ),
    CurrentSensorEntityDescription(
        key="last_session_cost",
        translation_key="last_session_cost",
        icon="mdi:cash",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (_get_history_sessions(data) or [{}])[0].get(
            "TotalPrice"
        ),
        unit_fn=lambda data: (data.get("chargers") or [{}])[0].get("Currency"),
        last_reset_fn=_get_last_session_end,
    ),
    CurrentSensorEntityDescription(
        key="last_session_energy",
        translation_key="last_session_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: (_get_history_sessions(data) or [{}])[0].get("TotalkWH"),
        last_reset_fn=_get_last_session_end,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for each charger."""
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    chargers = coordinator.data.get("chargers") or []
    async_add_entities(
        CurrentSensor(coordinator, description, charger)
        for charger in chargers
        for description in SENSOR_DESCRIPTIONS
    )


class CurrentSensor(CoordinatorEntity[CurrentCoordinator], SensorEntity):
    """A sensor for one charger."""

    _attr_has_entity_name = True
    entity_description: CurrentSensorEntityDescription

    def __init__(
        self,
        coordinator: CurrentCoordinator,
        description: CurrentSensorEntityDescription,
        charger: dict,
    ) -> None:
        """Initialise the sensor for one charger."""
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
                c for c in data.get("chargers") or [] if c["FK_ChargePointID"] == cp_id
            ],
            "ongoing": [
                s
                for s in data.get("ongoing") or []
                if s.get("ChargingPointID") == cp_id
            ],
            "history": {
                "List": [
                    h
                    for h in (data.get("history") or {}).get("List") or []
                    if h.get("ChargePointID") == cp_id
                ]
            },
        }

    @property
    def available(self) -> bool:
        """Return whether the charger is still on the account."""
        return super().available and any(
            c["FK_ChargePointID"] == self._charge_point_id
            for c in (self.coordinator.data or {}).get("chargers") or []
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, which for costs is the charger's currency."""
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self._filtered_data())
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> Any:
        """Return the value for this charger."""
        return self.entity_description.value_fn(self._filtered_data())

    @property
    def last_reset(self) -> datetime | None:
        """Return when a per-session total last started over."""
        if self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn(self._filtered_data())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra detail for this charger, where the sensor has any."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self._filtered_data())
