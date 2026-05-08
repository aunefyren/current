import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CurrentCoordinator

_LOGGER = logging.getLogger(__name__)

_PENDING_TIMEOUT = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    chargers = coordinator.data.get("chargers") or []
    entities: list = []
    for charger in chargers:
        entities.extend([
            CurrentChargingSwitch(coordinator, charger),
            CurrentAuthSwitch(coordinator, charger),
            CurrentCableLockSwitch(coordinator, charger),
        ])
    async_add_entities(entities)


def _device_info(charger: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, str(charger["FK_ChargePointID"]))},
        name=charger.get("Name", "CURRENT EV Charger"),
        manufacturer="CURRENT",
    )


class CurrentChargingSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "EV Charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._pending_state: bool | None = None
        self._attr_unique_id = f"current_{self._charge_point_id}_charging"
        self._attr_device_info = _device_info(charger)

    @property
    def available(self) -> bool:
        return super().available and any(
            c["FK_ChargePointID"] == self._charge_point_id
            for c in (self.coordinator.data or {}).get("chargers") or []
        )

    def _get_session(self) -> dict | None:
        return next(
            (s for s in (self.coordinator.data or {}).get("ongoing") or []
             if s.get("ChargingPointID") == self._charge_point_id),
            None,
        )

    @property
    def is_on(self) -> bool:
        if self._pending_state is not None:
            return self._pending_state
        return self._get_session() is not None

    def _handle_coordinator_update(self) -> None:
        if self._pending_state is not None:
            if (self._get_session() is not None) == self._pending_state:
                self._pending_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._pending_state = True
        self.async_write_ha_state()
        await self.coordinator.client.start_charging(self._charge_point_id)
        self.coordinator.start_fast_polling(120)
        async_call_later(self.hass, 5, self._async_refresh)
        async_call_later(self.hass, _PENDING_TIMEOUT, self._async_clear_pending)

    async def async_turn_off(self, **kwargs: Any) -> None:
        session = self._get_session()
        if not session:
            _LOGGER.warning("No active session to stop")
            return
        self._pending_state = False
        self.async_write_ha_state()
        await self.coordinator.client.stop_charging(
            session["ChargingBoxID"], session["PK_ServiceSessionID"]
        )
        self.coordinator.start_fast_polling(120)
        async_call_later(self.hass, 5, self._async_refresh)
        async_call_later(self.hass, _PENDING_TIMEOUT, self._async_clear_pending)

    async def _async_refresh(self, _now: Any) -> None:
        await self.coordinator.async_request_refresh()

    async def _async_clear_pending(self, _now: Any) -> None:
        if self._pending_state is not None:
            self._pending_state = None
            self.async_write_ha_state()


class CurrentAuthSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Require Authentication"
    _attr_icon = "mdi:shield-key"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._box_id: int = charger["FK_ChargingBoxID"]
        self._attr_unique_id = f"current_{self._charge_point_id}_auth"
        self._attr_device_info = _device_info(charger)

    def _get_charger(self) -> dict:
        return next(
            (c for c in (self.coordinator.data or {}).get("chargers") or []
             if c["FK_ChargePointID"] == self._charge_point_id),
            {},
        )

    @property
    def available(self) -> bool:
        return super().available and bool(self._get_charger())

    @property
    def is_on(self) -> bool:
        return bool(self._get_charger().get("IsAuthenticationEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_authentication(self._box_id, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_authentication(self._box_id, False)
        await self.coordinator.async_request_refresh()


class CurrentCableLockSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Cable Lock"
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._attr_unique_id = f"current_{self._charge_point_id}_cable_lock"
        self._attr_device_info = _device_info(charger)

    def _get_charger(self) -> dict:
        return next(
            (c for c in (self.coordinator.data or {}).get("chargers") or []
             if c["FK_ChargePointID"] == self._charge_point_id),
            {},
        )

    @property
    def available(self) -> bool:
        return super().available and bool(self._get_charger())

    @property
    def is_on(self) -> bool:
        return bool(self._get_charger().get("isPermanentCableLockingEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_cable_lock(self._charge_point_id, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_cable_lock(self._charge_point_id, False)
        await self.coordinator.async_request_refresh()
