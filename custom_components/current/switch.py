"""Switches for CURRENT chargers."""

import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    """Set up the charging, authentication and cable lock switches."""
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    chargers = coordinator.data.get("chargers") or []
    entities: list = []
    for charger in chargers:
        entities.extend(
            [
                CurrentChargingSwitch(coordinator, charger),
                CurrentAuthSwitch(coordinator, charger),
                CurrentCableLockSwitch(coordinator, charger),
            ]
        )
    async_add_entities(entities)


def _device_info(charger: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, str(charger["FK_ChargePointID"]))},
        name=charger.get("Name", "CURRENT EV Charger"),
        manufacturer="CURRENT",
    )


class CurrentChargingSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    """Starts and stops charging."""

    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        """Initialise the switch for one charger."""
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._pending_state: bool | None = None
        self._attr_unique_id = f"current_{self._charge_point_id}_charging"
        self._attr_device_info = _device_info(charger)

    @property
    def available(self) -> bool:
        """Return whether the charger is still on the account."""
        return super().available and any(
            c["FK_ChargePointID"] == self._charge_point_id
            for c in (self.coordinator.data or {}).get("chargers") or []
        )

    def _get_session(self) -> dict | None:
        return next(
            (
                s
                for s in (self.coordinator.data or {}).get("ongoing") or []
                if s.get("ChargingPointID") == self._charge_point_id
            ),
            None,
        )

    @property
    def is_on(self) -> bool:
        """Return whether charging, or the state just requested."""
        if self._pending_state is not None:
            return self._pending_state
        return self._get_session() is not None

    def _handle_coordinator_update(self) -> None:
        if (
            self._pending_state is not None
            and (self._get_session() is not None) == self._pending_state
        ):
            self._pending_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start charging."""
        await self._async_send_and_expect(
            True, self.coordinator.client.start_charging(self._charge_point_id)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the active session, if there is one."""
        session = self._get_session()
        if not session:
            _LOGGER.warning("No active session to stop")
            return
        await self._async_send_and_expect(
            False,
            self.coordinator.client.stop_charging(
                session["ChargingBoxID"], session["PK_ServiceSessionID"]
            ),
        )

    async def _async_send_and_expect(
        self, state: bool, command: Coroutine[Any, Any, Any]
    ) -> None:
        """Show the requested state at once, then send the command.

        CURRENT takes a few seconds to report a session starting or stopping,
        so the switch holds the requested state until a poll confirms it or
        the timeout passes. If the command itself fails, nothing is coming, so
        the switch goes straight back to what the charger last reported.
        """
        self._pending_state = state
        self.async_write_ha_state()
        try:
            await self.coordinator.async_send_command(command)
        except HomeAssistantError:
            self._pending_state = None
            self.async_write_ha_state()
            raise
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
    """Turns required authentication on or off."""

    _attr_has_entity_name = True
    _attr_translation_key = "require_authentication"
    _attr_icon = "mdi:shield-key"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        """Initialise the switch for one charger."""
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._box_id: int = charger["FK_ChargingBoxID"]
        self._attr_unique_id = f"current_{self._charge_point_id}_auth"
        self._attr_device_info = _device_info(charger)

    def _get_charger(self) -> dict:
        return next(
            (
                c
                for c in (self.coordinator.data or {}).get("chargers") or []
                if c["FK_ChargePointID"] == self._charge_point_id
            ),
            {},
        )

    @property
    def available(self) -> bool:
        """Return whether the charger is still on the account."""
        return super().available and bool(self._get_charger())

    @property
    def is_on(self) -> bool:
        """Return whether authentication is required."""
        return bool(self._get_charger().get("IsAuthenticationEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Require authentication."""
        await self.coordinator.async_send_command(
            self.coordinator.client.set_authentication(self._box_id, True)
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop requiring authentication."""
        await self.coordinator.async_send_command(
            self.coordinator.client.set_authentication(self._box_id, False)
        )
        await self.coordinator.async_request_refresh()


class CurrentCableLockSwitch(CoordinatorEntity[CurrentCoordinator], SwitchEntity):
    """Turns permanent cable locking on or off."""

    _attr_has_entity_name = True
    _attr_translation_key = "cable_lock"
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        """Initialise the switch for one charger."""
        super().__init__(coordinator)
        self._charge_point_id: int = charger["FK_ChargePointID"]
        self._attr_unique_id = f"current_{self._charge_point_id}_cable_lock"
        self._attr_device_info = _device_info(charger)

    def _get_charger(self) -> dict:
        return next(
            (
                c
                for c in (self.coordinator.data or {}).get("chargers") or []
                if c["FK_ChargePointID"] == self._charge_point_id
            ),
            {},
        )

    @property
    def available(self) -> bool:
        """Return whether the charger is still on the account."""
        return super().available and bool(self._get_charger())

    @property
    def is_on(self) -> bool:
        """Return whether the cable stays locked."""
        return bool(self._get_charger().get("isPermanentCableLockingEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the cable permanently."""
        await self.coordinator.async_send_command(
            self.coordinator.client.set_cable_lock(self._charge_point_id, True)
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop locking the cable permanently."""
        await self.coordinator.async_send_command(
            self.coordinator.client.set_cable_lock(self._charge_point_id, False)
        )
        await self.coordinator.async_request_refresh()
