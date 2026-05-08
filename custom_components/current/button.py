import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CUSTOMER_ID, DOMAIN
from .coordinator import CurrentCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CurrentCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CurrentRestartButton(coordinator, entry)])


class CurrentRestartButton(CoordinatorEntity[CurrentCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Restart Charger"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: CurrentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"current_{entry.data[CONF_CUSTOMER_ID]}_restart"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.data[CONF_CUSTOMER_ID]))},
            name="CURRENT EV Charger",
            manufacturer="CURRENT",
        )

    async def async_press(self) -> None:
        chargers = (self.coordinator.data or {}).get("chargers") or []
        if not chargers:
            _LOGGER.error("No chargers found — cannot restart")
            return
        box_id = chargers[0]["FK_ChargingBoxID"]
        _LOGGER.warning("Restarting charger box_id=%s", box_id)
        await self.coordinator.client.restart_charger(box_id)
