"""Tests for setup, unload and the coordinator."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.current.const import (
    CONF_ACCESS_TOKEN,
    DOMAIN,
    SCAN_INTERVAL_ACTIVE,
    SCAN_INTERVAL_IDLE,
)
from custom_components.current.coordinator import SCAN_INTERVAL_FAST

from .conftest import CurrentApiMock, FakeSession, setup_entry
from .const import MOCK_CHARGE_POINT_ID, MOCK_REFRESHED_TOKEN


async def test_setup_loads_data(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setup fetches chargers, sessions and history into the coordinator."""
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    data = coordinator.data
    assert [c["FK_ChargePointID"] for c in data["chargers"]] == [MOCK_CHARGE_POINT_ID]
    assert data["ongoing"] == []
    assert len(data["history"]["List"]) == 5


async def test_poll_interval_follows_charging(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Idle polls slowly, charging faster, and just after a command fastest."""
    await setup_entry(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator.update_interval == timedelta(seconds=SCAN_INTERVAL_IDLE)

    api.charging = True
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=SCAN_INTERVAL_ACTIVE)

    coordinator.start_fast_polling(120)
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=SCAN_INTERVAL_FAST)


async def test_refreshed_token_is_persisted(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A token refreshed mid-update is saved, so a restart does not lose it."""
    api.expire_token()
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == MOCK_REFRESHED_TOKEN


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """When the refresh token is refused too, the user is asked to log in."""
    api.expire_token()
    api.refresh_status = 401
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_connection_failure_retries(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A server error leaves the entry retrying rather than asking for a login."""
    api.status = 500
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_unload(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entry unloads cleanly and forgets its coordinator."""
    await setup_entry(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert mock_config_entry.entry_id not in hass.data[DOMAIN]
