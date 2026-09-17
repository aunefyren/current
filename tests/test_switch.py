"""Tests for the CURRENT switches and restart button."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.current.const import DOMAIN

from .conftest import CurrentApiMock, FakeSession, entity_id, setup_entry
from .const import (
    MOCK_BOX_ID,
    MOCK_CHARGE_POINT_ID,
    MOCK_CUSTOMER_ID,
    SECOND_BOX_ID,
    SECOND_CHARGE_POINT_ID,
)


async def call(hass: HomeAssistant, domain: str, service: str, entity: str) -> None:
    """Call a service on one entity and wait for it to finish."""
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity}, blocking=True
    )
    await hass.async_block_till_done()


async def run_timers(hass: HomeAssistant, seconds: int) -> None:
    """Advance time so scheduled refreshes and pending-state timeouts fire."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# -- charging -------------------------------------------------------------


async def test_charging_switch_reflects_session(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The switch is on exactly when the charger has an active session."""
    api.charging = True
    await setup_entry(hass, mock_config_entry)

    assert hass.states.get(entity_id(hass, "switch", "charging")).state == STATE_ON


async def test_turn_on_starts_charging(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Turning on sends RemoteStart and shows on while CURRENT catches up."""
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")
    assert hass.states.get(switch).state == STATE_OFF

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)

    (command,) = api.commands()
    assert command["path"] == "Commands/RemoteStart"
    assert command["json"]["FK_ChargingPointID"] == MOCK_CHARGE_POINT_ID
    assert command["json"]["FK_CustomerID"] == MOCK_CUSTOMER_ID
    # No session yet, but the switch does not snap back off.
    assert hass.states.get(switch).state == STATE_ON

    # The session appears on the follow-up poll.
    api.charging = True
    await run_timers(hass, 6)
    assert hass.states.get(switch).state == STATE_ON

    await run_timers(hass, 31)
    assert hass.states.get(switch).state == STATE_ON


async def test_start_that_never_happens_reverts(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If no session ever appears, the switch falls back to off."""
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)
    await run_timers(hass, 6)
    assert hass.states.get(switch).state == STATE_ON

    await run_timers(hass, 31)
    assert hass.states.get(switch).state == STATE_OFF


async def test_turn_off_stops_the_active_session(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    api_responses: dict,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Turning off stops the charger's session by box and session id."""
    api.charging = True
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")
    session_id = api_responses["ongoing_charging"]["Result"][0]["PK_ServiceSessionID"]

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, switch)

    (command,) = api.commands()
    assert command["path"] == f"Commands/RemoteStop/{MOCK_BOX_ID}/{session_id}"
    assert hass.states.get(switch).state == STATE_OFF

    api.charging = False
    await run_timers(hass, 31)
    assert hass.states.get(switch).state == STATE_OFF


async def test_turn_off_without_session_sends_nothing(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """There is nothing to stop when nothing is charging."""
    await setup_entry(hass, mock_config_entry)

    await call(
        hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, entity_id(hass, "switch", "charging")
    )
    assert api.commands() == []


async def test_turn_off_stops_the_right_charger(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    api_responses: dict,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With two chargers, only the one with the session can be stopped."""
    api.add_second_charger()
    api.charging = True
    await setup_entry(hass, mock_config_entry)

    second = entity_id(hass, "switch", "charging", SECOND_CHARGE_POINT_ID)
    assert hass.states.get(second).state == STATE_OFF
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, second)
    assert api.commands() == []

    await call(
        hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, entity_id(hass, "switch", "charging")
    )
    (command,) = api.commands()
    assert command["path"].startswith(f"Commands/RemoteStop/{MOCK_BOX_ID}/")

    await run_timers(hass, 31)


# -- settings -------------------------------------------------------------


async def test_authentication_switch(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The auth switch mirrors the charger and sets it by box id."""
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "auth")
    assert hass.states.get(switch).state == STATE_ON

    api.chargers[0]["IsAuthenticationEnabled"] = False
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, switch)

    (command,) = api.commands()
    assert command["path"] == f"Commands/SetDefaultAuthentication/{MOCK_BOX_ID}/false"
    assert hass.states.get(switch).state == STATE_OFF


async def test_cable_lock_switch(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The cable lock switch sets the lock by charge point id."""
    api.add_second_charger()
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "cable_lock", SECOND_CHARGE_POINT_ID)
    assert hass.states.get(switch).state == STATE_OFF

    api.chargers[1]["isPermanentCableLockingEnabled"] = True
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)

    (command,) = api.commands()
    assert (
        command["path"]
        == f"Commands/SetDefaultPermanentCableLocking/{SECOND_CHARGE_POINT_ID}/true"
    )
    assert hass.states.get(switch).state == STATE_ON


async def test_switches_unavailable_when_charger_removed(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A charger no longer on the account cannot be controlled."""
    api.add_second_charger()
    await setup_entry(hass, mock_config_entry)

    api.chargers = api.chargers[:1]
    await hass.data["current"][mock_config_entry.entry_id].async_refresh()
    await hass.async_block_till_done()

    for key in ("charging", "auth", "cable_lock"):
        found = hass.states.get(entity_id(hass, "switch", key, SECOND_CHARGE_POINT_ID))
        assert found.state == STATE_UNAVAILABLE, key
    restart = entity_id(hass, "button", "restart", SECOND_CHARGE_POINT_ID)
    assert hass.states.get(restart).state == STATE_UNAVAILABLE


# -- restart --------------------------------------------------------------


async def test_restart_button(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Pressing restart resets the right box."""
    api.add_second_charger()
    await setup_entry(hass, mock_config_entry)

    await call(
        hass,
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        entity_id(hass, "button", "restart", SECOND_CHARGE_POINT_ID),
    )

    (command,) = api.commands()
    assert command["path"] == f"Commands/Reset/{SECOND_BOX_ID}/1"


# -- settings in both directions ------------------------------------------


@pytest.mark.parametrize(
    ("key", "field", "service", "expected_path"),
    [
        (
            "auth",
            "IsAuthenticationEnabled",
            SERVICE_TURN_ON,
            f"Commands/SetDefaultAuthentication/{MOCK_BOX_ID}/true",
        ),
        (
            "cable_lock",
            "isPermanentCableLockingEnabled",
            SERVICE_TURN_OFF,
            f"Commands/SetDefaultPermanentCableLocking/{MOCK_CHARGE_POINT_ID}/false",
        ),
    ],
)
async def test_setting_switch_other_direction(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
    key: str,
    field: str,
    service: str,
    expected_path: str,
) -> None:
    """The directions the tests above do not exercise send the right value."""
    api.chargers[0][field] = service == SERVICE_TURN_OFF
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", key)

    api.chargers[0][field] = service == SERVICE_TURN_ON
    await call(hass, SWITCH_DOMAIN, service, switch)

    (command,) = api.commands()
    assert command["path"] == expected_path
    assert hass.states.get(switch).state == (
        STATE_ON if service == SERVICE_TURN_ON else STATE_OFF
    )


# -- failing commands -----------------------------------------------------


async def test_failed_start_does_not_stick(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If RemoteStart fails, the switch goes back off at once and stays off."""
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")
    api.command_status = 500

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)

    assert err.value.translation_key == "command_failed"
    assert hass.states.get(switch).state == STATE_OFF
    await run_timers(hass, 120)
    assert hass.states.get(switch).state == STATE_OFF


async def test_failed_stop_does_not_stick(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If RemoteStop fails, the switch shows the car is still charging."""
    api.charging = True
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")
    api.command_status = 500

    with pytest.raises(HomeAssistantError):
        await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, switch)

    assert hass.states.get(switch).state == STATE_ON


@pytest.mark.parametrize(
    ("platform", "key", "service"),
    [
        ("switch", "auth", SERVICE_TURN_OFF),
        ("switch", "cable_lock", SERVICE_TURN_OFF),
        ("button", "restart", SERVICE_PRESS),
    ],
)
async def test_failed_command_raises_home_assistant_error(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
    platform: str,
    key: str,
    service: str,
) -> None:
    """Every control reports a failed command as a readable error.

    A bare CannotConnectError would reach the UI as an unexpected exception.
    """
    await setup_entry(hass, mock_config_entry)
    api.command_status = 500
    domain = BUTTON_DOMAIN if platform == "button" else SWITCH_DOMAIN

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, domain, service, entity_id(hass, platform, key))

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "command_failed"
    assert api.commands(), "the command should have been attempted"


async def test_command_auth_failure_starts_reauth(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A command refused for auth reasons asks the user to log in again."""
    await setup_entry(hass, mock_config_entry)
    switch = entity_id(hass, "switch", "charging")
    api.expire_token()
    api.refresh_status = 401

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)

    assert err.value.translation_key == "command_auth_failed"
    assert hass.states.get(switch).state == STATE_OFF
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
