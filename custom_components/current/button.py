"""Buttons for CURRENT chargers."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CurrentCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a restart button for each charger."""
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    chargers = coordinator.data.get("chargers") or []
    async_add_entities(
        CurrentRestartButton(coordinator, charger) for charger in chargers
    )


class CurrentRestartButton(CoordinatorEntity[CurrentCoordinator], ButtonEntity):
    """Restarts a charger."""

    _attr_has_entity_name = True
    _attr_translation_key = "restart"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: CurrentCoordinator, charger: dict) -> None:
        """Initialise the button for one charger."""
        super().__init__(coordinator)
        self._box_id: int = charger["FK_ChargingBoxID"]
        self._attr_unique_id = f"current_{charger['FK_ChargePointID']}_restart"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(charger["FK_ChargePointID"]))},
            name=charger.get("Name", "CURRENT EV Charger"),
            manufacturer="CURRENT",
        )

    @property
    def available(self) -> bool:
        """Return whether the charger is still on the account."""
        return super().available and any(
            c["FK_ChargingBoxID"] == self._box_id
            for c in (self.coordinator.data or {}).get("chargers") or []
        )

    async def async_press(self) -> None:
        """Restart the charger."""
        _LOGGER.warning("Restarting charger box_id=%s", self._box_id)
        await self.coordinator.async_send_command(
            self.coordinator.client.restart_charger(self._box_id)
        )
