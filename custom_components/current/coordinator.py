"""Data update coordinator for CURRENT."""

import logging
import time
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, CannotConnectError, CurrentApiClient
from .const import DOMAIN, SCAN_INTERVAL_ACTIVE, SCAN_INTERVAL_IDLE

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_FAST = 5  # seconds — used briefly after start/stop


class CurrentCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls chargers, active sessions and history for one account."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: CurrentApiClient
    ) -> None:
        """Initialise the coordinator with the idle poll interval."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )
        self.client = client
        self._fast_poll_until: float = 0

    def start_fast_polling(self, duration: int = 120) -> None:
        """Poll faster for a while, so a start or stop shows up quickly."""
        self._fast_poll_until = time.monotonic() + duration

    async def async_send_command(self, command: Awaitable[Any]) -> None:
        """Send a charger command, reporting failures the way Home Assistant expects.

        A rejected login also starts re-authentication, as a failed poll would.
        """
        try:
            await command
        except AuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_auth_failed"
            ) from err
        except CannotConnectError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            ongoing = await self.client.get_ongoing_session()
            chargers = await self.client.get_chargers()
            history = await self.client.get_history()
        except AuthError as err:
            raise ConfigEntryAuthFailed(
                f"CURRENT authentication failed: {err}"
            ) from err
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
