"""Tests for the CURRENT API client."""

from __future__ import annotations

import pytest

from custom_components.current.api import (
    AuthError,
    CannotConnectError,
    CurrentApiClient,
)
from custom_components.current.const import APP_ID, APP_ORIGIN, APP_VERSION

from .conftest import CurrentApiMock, FakeSession
from .const import (
    MOCK_ACCESS_TOKEN,
    MOCK_BOX_ID,
    MOCK_CHARGE_POINT_ID,
    MOCK_CUSTOMER_ID,
    MOCK_EMAIL,
    MOCK_PASSWORD,
    MOCK_REFRESH_TOKEN,
    MOCK_REFRESHED_TOKEN,
    MOCK_USER_ID,
)


def build(session: FakeSession, refreshed: list[str] | None = None) -> CurrentApiClient:
    """Build a client against the fake session."""
    return CurrentApiClient(
        session,
        access_token=MOCK_ACCESS_TOKEN,
        refresh_token=MOCK_REFRESH_TOKEN,
        customer_id=MOCK_CUSTOMER_ID,
        user_id=MOCK_USER_ID,
        on_token_refresh=refreshed.append if refreshed is not None else None,
    )


# -- login ----------------------------------------------------------------


async def test_login_unwraps_result(session: FakeSession, api: CurrentApiMock) -> None:
    """Login returns the fields the config flow reads, without the wrapper."""
    data = await CurrentApiClient.login(session, MOCK_EMAIL, MOCK_PASSWORD)

    assert data["accessToken"] == MOCK_ACCESS_TOKEN
    assert data["rToken"] == MOCK_REFRESH_TOKEN
    assert data["customer"]["PK_CustomerID"] == MOCK_CUSTOMER_ID
    assert data["customer"]["FK_UserID"] == MOCK_USER_ID

    body = api.requests[0]["json"]
    assert body["Email"] == MOCK_EMAIL
    assert body["Password"] == MOCK_PASSWORD
    assert body["appID"] == APP_ID


@pytest.mark.parametrize("status", [401, 403])
async def test_login_rejected_is_auth_error(
    session: FakeSession, api: CurrentApiMock, status: int
) -> None:
    """A refused login means bad credentials."""
    api.login_status = status
    with pytest.raises(AuthError):
        await CurrentApiClient.login(session, MOCK_EMAIL, MOCK_PASSWORD)


async def test_login_server_error_is_connection_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """A server error is not the user's password's fault."""
    api.login_status = 500
    with pytest.raises(CannotConnectError):
        await CurrentApiClient.login(session, MOCK_EMAIL, MOCK_PASSWORD)


async def test_login_network_failure_is_connection_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """Aiohttp errors surface as connection errors."""
    api.connection_error = True
    with pytest.raises(CannotConnectError):
        await CurrentApiClient.login(session, MOCK_EMAIL, MOCK_PASSWORD)


# -- reads ----------------------------------------------------------------


async def test_get_chargers(session: FakeSession, api: CurrentApiMock) -> None:
    """Chargers are read for the customer and unwrapped from Result.datas."""
    chargers = await build(session).get_chargers()

    assert [c["FK_ChargePointID"] for c in chargers] == [MOCK_CHARGE_POINT_ID]
    assert api.requests[0]["params"] == {"customerID": MOCK_CUSTOMER_ID}


async def test_request_headers(session: FakeSession, api: CurrentApiMock) -> None:
    """Requests carry the token and identify as the app."""
    await build(session).get_chargers()

    headers = api.requests[0]["headers"]
    assert headers["Authorization"] == f"Bearer {MOCK_ACCESS_TOKEN}"
    assert headers["Origin"] == APP_ORIGIN
    assert headers["X-App-Version"] == APP_VERSION


async def test_ongoing_session_idle(session: FakeSession) -> None:
    """No active session is an empty list, not None."""
    assert await build(session).get_ongoing_session() == []


async def test_ongoing_session_charging(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """An active session is returned as the API lists it."""
    api.charging = True
    sessions = await build(session).get_ongoing_session()

    assert len(sessions) == 1
    assert sessions[0]["ChargingPointID"] == MOCK_CHARGE_POINT_ID


async def test_get_history(session: FakeSession, api: CurrentApiMock) -> None:
    """History is unwrapped from Result and asked for with prices included."""
    history = await build(session).get_history()

    assert len(history["List"]) == 5
    assert api.requests[0]["params"]["calculateTotalPrice"] == "true"
    assert api.requests[0]["params"]["number"] == 5


async def test_request_network_failure_is_connection_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """Network errors on a normal request surface as connection errors."""
    api.connection_error = True
    with pytest.raises(CannotConnectError):
        await build(session).get_chargers()


async def test_ongoing_session_unexpected_shape(
    session: FakeSession, api_responses: dict
) -> None:
    """Anything other than a list of sessions is treated as none."""
    api_responses["ongoing_idle"]["Result"] = None
    assert await build(session).get_ongoing_session() == []


# -- token refresh --------------------------------------------------------


async def test_expired_token_is_refreshed_and_retried(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """A 401 refreshes the token once, retries, and reports the new token."""
    refreshed: list[str] = []
    client = build(session, refreshed)
    api.expire_token()

    chargers = await client.get_chargers()

    assert chargers
    assert [r["path"] for r in api.requests] == [
        "ChargePoints/my-points",
        "Security/RefreshAccessTokenInternal",
        "ChargePoints/my-points",
    ]
    assert api.requests[1]["json"]["rToken"] == MOCK_REFRESH_TOKEN
    assert client.access_token == MOCK_REFRESHED_TOKEN
    assert refreshed == [MOCK_REFRESHED_TOKEN]
    assert (
        api.requests[2]["headers"]["Authorization"] == f"Bearer {MOCK_REFRESHED_TOKEN}"
    )


async def test_refresh_rejected_is_auth_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """If the refresh token is refused too, the user must log in again."""
    api.expire_token()
    api.refresh_status = 401

    with pytest.raises(AuthError):
        await build(session).get_chargers()


async def test_refresh_without_token_is_auth_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """A refresh answer with no token in it cannot be used to carry on."""
    api.expire_token()
    api.refresh_body = {"Result": {"success": False, "datas": None}}
    refreshed: list[str] = []

    with pytest.raises(AuthError):
        await build(session, refreshed).get_chargers()
    assert refreshed == []


async def test_forbidden_after_refresh_is_not_auth_error(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """A 403 that survives a fresh token is not a credentials problem.

    Reporting it as one would send the user to re-authenticate for nothing.
    """
    api.forbidden_paths.add("ChargePoints/my-points")

    with pytest.raises(CannotConnectError):
        await build(session).get_chargers()


async def test_server_error_does_not_refresh(
    session: FakeSession, api: CurrentApiMock
) -> None:
    """Only auth failures trigger a refresh."""
    api.status = 500

    with pytest.raises(CannotConnectError):
        await build(session).get_chargers()
    assert len(api.requests) == 1


# -- commands -------------------------------------------------------------


async def test_start_charging(session: FakeSession, api: CurrentApiMock) -> None:
    """Starting is a POST naming the charge point and customer."""
    await build(session).start_charging(MOCK_CHARGE_POINT_ID)

    (command,) = api.commands()
    assert command["method"] == "POST"
    assert command["path"] == "Commands/RemoteStart"
    assert command["json"] == {
        "FK_ChargingPointID": MOCK_CHARGE_POINT_ID,
        "FK_CustomerID": MOCK_CUSTOMER_ID,
        "Origin": "App",
    }


@pytest.mark.parametrize(
    ("call", "expected_path"),
    [
        (
            lambda c: c.stop_charging(MOCK_BOX_ID, 90035),
            f"Commands/RemoteStop/{MOCK_BOX_ID}/90035",
        ),
        (
            lambda c: c.set_authentication(MOCK_BOX_ID, True),
            f"Commands/SetDefaultAuthentication/{MOCK_BOX_ID}/true",
        ),
        (
            lambda c: c.set_authentication(MOCK_BOX_ID, False),
            f"Commands/SetDefaultAuthentication/{MOCK_BOX_ID}/false",
        ),
        (
            lambda c: c.set_cable_lock(MOCK_CHARGE_POINT_ID, True),
            f"Commands/SetDefaultPermanentCableLocking/{MOCK_CHARGE_POINT_ID}/true",
        ),
        (lambda c: c.restart_charger(MOCK_BOX_ID), f"Commands/Reset/{MOCK_BOX_ID}/1"),
    ],
)
async def test_get_commands(
    session: FakeSession, api: CurrentApiMock, call, expected_path: str
) -> None:
    """The remaining commands are GETs with their arguments in the path."""
    await call(build(session))

    (command,) = api.commands()
    assert command["method"] == "GET"
    assert command["path"] == expected_path
