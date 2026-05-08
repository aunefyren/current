import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CUSTOMER_ID, DOMAIN
from .coordinator import CurrentCoordinator

_LOGGER = logging.getLogger(__name__)

_PENDING_TIMEOUT = 30  # seconds before pending state is abandoned


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CurrentChargingSwitch(coordinator, entry),
        CurrentAuthSwitch(coordinator, entry),
        CurrentCableLockSwitch(coordinator, entry),
    ])


class CurrentChargingSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "EV Charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: CurrentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._pending_state: bool | None = None
        self._attr_unique_id = f"current_{entry.data[CONF_CUSTOMER_ID]}_charging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.data[CONF_CUSTOMER_ID]))},
            name="CURRENT EV Charger",
            manufacturer="CURRENT",
        )

    @property
    def is_on(self) -> bool:
        if self._pending_state is not None:
            return self._pending_state
        return bool(self.coordinator.data and self.coordinator.data.get("ongoing"))

    def _handle_coordinator_update(self) -> None:
        # Clear pending state once the API confirms the expected state
        if self._pending_state is not None:
            actual = bool(self.coordinator.data and self.coordinator.data.get("ongoing"))
            if actual == self._pending_state:
                self._pending_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if not chargers:
            _LOGGER.error("No chargers found — cannot start charging")
            return
        charge_point_id = chargers[0].get("FK_ChargePointID")
        self._pending_state = True
        self.async_write_ha_state()
        await self.coordinator.client.start_charging(charge_point_id)
        self.coordinator.start_fast_polling(120)
        async_call_later(self.hass, 5, self._async_refresh)
        async_call_later(self.hass, _PENDING_TIMEOUT, self._async_clear_pending)

    async def async_turn_off(self, **kwargs: Any) -> None:
        ongoing = (self.coordinator.data or {}).get("ongoing") or []
        if not ongoing:
            _LOGGER.warning("No active session to stop")
            return
        session = ongoing[0]
        box_id = session.get("ChargingBoxID")
        session_id = session.get("PK_ServiceSessionID")
        self._pending_state = False
        self.async_write_ha_state()
        await self.coordinator.client.stop_charging(box_id, session_id)
        self.coordinator.start_fast_polling(120)
        async_call_later(self.hass, 5, self._async_refresh)
        async_call_later(self.hass, _PENDING_TIMEOUT, self._async_clear_pending)

    async def _async_refresh(self, _now: Any) -> None:
        await self.coordinator.async_request_refresh()

    async def _async_clear_pending(self, _now: Any) -> None:
        """Abandon pending state after timeout so the real state takes over."""
        if self._pending_state is not None:
            self._pending_state = None
            self.async_write_ha_state()


class CurrentAuthSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Require Authentication"
    _attr_icon = "mdi:shield-key"

    def __init__(self, coordinator: CurrentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"current_{entry.data[CONF_CUSTOMER_ID]}_auth"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.data[CONF_CUSTOMER_ID]))},
            name="CURRENT EV Charger",
            manufacturer="CURRENT",
        )

    @property
    def is_on(self) -> bool:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        return bool(chargers and chargers[0].get("IsAuthenticationEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if chargers:
            await self.coordinator.client.set_authentication(
                chargers[0]["FK_ChargingBoxID"], True
            )
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if chargers:
            await self.coordinator.client.set_authentication(
                chargers[0]["FK_ChargingBoxID"], False
            )
            await self.coordinator.async_request_refresh()


class CurrentCableLockSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Cable Lock"
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: CurrentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"current_{entry.data[CONF_CUSTOMER_ID]}_cable_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.data[CONF_CUSTOMER_ID]))},
            name="CURRENT EV Charger",
            manufacturer="CURRENT",
        )

    @property
    def is_on(self) -> bool:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        return bool(chargers and chargers[0].get("isPermanentCableLockingEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if chargers:
            await self.coordinator.client.set_cable_lock(
                chargers[0]["FK_ChargePointID"], True
            )
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if chargers:
            await self.coordinator.client.set_cable_lock(
                chargers[0]["FK_ChargePointID"], False
            )
            await self.coordinator.async_request_refresh()
