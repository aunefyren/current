import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnectError, CurrentApiClient
from .const import DOMAIN, SCAN_INTERVAL_ACTIVE, SCAN_INTERVAL_IDLE

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_FAST = 5  # seconds — used briefly after start/stop


class CurrentCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, client: CurrentApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )
        self.client = client
        self._fast_poll_until: float = 0

    def start_fast_polling(self, duration: int = 120) -> None:
        self._fast_poll_until = time.monotonic() + duration

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            ongoing = await self.client.get_ongoing_session()
            chargers = await self.client.get_chargers()
            history = await self.client.get_history()
        except CannotConnectError as err:
            raise UpdateFailed(f"Error communicating with CURRENT API: {err}") from err

        if time.monotonic() < self._fast_poll_until:
            self.update_interval = timedelta(seconds=SCAN_INTERVAL_FAST)
        else:
            self.update_interval = timedelta(
                seconds=SCAN_INTERVAL_ACTIVE if ongoing else SCAN_INTERVAL_IDLE
            )

        return {
            "ongoing": ongoing,
            "chargers": chargers,
            "history": history,
        }
