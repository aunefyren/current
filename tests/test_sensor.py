"""Tests for the CURRENT sensors."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.current.const import DOMAIN

from .conftest import CurrentApiMock, FakeSession, entity_id, setup_entry
from .const import SECOND_CHARGE_POINT_ID


def state(hass: HomeAssistant, key: str, charge_point_id: int | None = None):
    """Return the state object of one sensor."""
    args = (charge_point_id,) if charge_point_id is not None else ()
    found = hass.states.get(entity_id(hass, "sensor", key, *args))
    assert found is not None
    return found


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Poll the API again and let the entities update."""
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def test_idle(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With nothing charging, session sensors are unknown and live ones zero."""
    await setup_entry(hass, mock_config_entry)

    status = state(hass, "status")
    assert status.state == "Available"
    assert status.attributes["current_status"] == "Available"
    for key in ("session_energy", "session_duration", "state_of_charge"):
        assert state(hass, key).state == STATE_UNKNOWN, key
    assert float(state(hass, "live_power").state) == 0.0
    assert float(state(hass, "live_current").state) == 0.0

    cost = state(hass, "last_session_cost")
    assert float(cost.state) == 82.18
    assert cost.attributes["unit_of_measurement"] == "NOK"
    assert float(state(hass, "last_session_energy").state) == 58.702


async def test_charging(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A live session drives the session and live sensors.

    The values are from a real charge. The session itself reported 0 kW and no
    current throughout; the live readings come from the charger.
    """
    api.charging = True
    await setup_entry(hass, mock_config_entry)

    status = state(hass, "status")
    assert status.state == "Charging"
    assert status.attributes["current_status"] == "Charging"
    assert float(state(hass, "live_power").state) == 11.177

    current = state(hass, "live_current")
    assert float(current.state) == 15.54
    assert current.attributes["current_l1"] == 15.52
    assert current.attributes["current_l2"] == 15.55
    assert current.attributes["current_l3"] == 15.55

    assert float(state(hass, "session_energy").state) == 6.882
    # 2256 seconds, shown in the suggested unit.
    duration = state(hass, "session_duration")
    assert float(duration.state) == 37.6
    assert duration.attributes["unit_of_measurement"] == "min"
    # This charger does not report battery level.
    assert state(hass, "state_of_charge").state == STATE_UNKNOWN


async def test_session_without_power_is_standby(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A session that draws nothing, such as a full battery, is standby."""
    api.charging = True
    api.live_overrides = {"LivekW": 0.0, "LiveAmps": 0.0}
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "status").state == "Standby"


async def test_inactive_point_is_unavailable_status(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A charge point CURRENT marks inactive reports as such."""
    api.chargers[0]["IsPointActive"] = False
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "status").state == "Unavailable"


async def test_state_follows_polling(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Starting and finishing a charge shows up on the next poll."""
    await setup_entry(hass, mock_config_entry)
    assert state(hass, "status").state == "Available"

    api.charging = True
    await refresh(hass, mock_config_entry)
    assert state(hass, "status").state == "Charging"

    api.charging = False
    await refresh(hass, mock_config_entry)
    assert state(hass, "status").state == "Available"
    assert float(state(hass, "live_power").state) == 0.0


async def test_each_charger_sees_only_its_own_data(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With two chargers, sessions and history are split by charge point."""
    api.add_second_charger()
    api.charging = True
    await setup_entry(hass, mock_config_entry)

    # The first charger has the live session; the second does not.
    assert state(hass, "status").state == "Charging"
    assert state(hass, "status", SECOND_CHARGE_POINT_ID).state == "Available"
    assert float(state(hass, "live_power").state) == 11.177
    assert float(state(hass, "live_power", SECOND_CHARGE_POINT_ID).state) == 0.0
    assert state(hass, "session_energy", SECOND_CHARGE_POINT_ID).state == STATE_UNKNOWN

    # The second charger's newest session is its own, not the account's newest.
    assert float(state(hass, "last_session_cost").state) == 82.18
    assert (
        float(state(hass, "last_session_cost", SECOND_CHARGE_POINT_ID).state) == 19.91
    )


async def test_removed_charger_goes_unavailable(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A charger no longer on the account makes its entities unavailable."""
    api.add_second_charger()
    await setup_entry(hass, mock_config_entry)

    api.chargers = api.chargers[:1]
    await refresh(hass, mock_config_entry)

    assert state(hass, "status").state == "Available"
    assert state(hass, "status", SECOND_CHARGE_POINT_ID).state == STATE_UNAVAILABLE
