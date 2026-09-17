"""Tests for the CURRENT config flow."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.current.const import (
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

from .conftest import CurrentApiMock, FakeSession
from .const import (
    MOCK_ACCESS_TOKEN,
    MOCK_CUSTOMER_ID,
    MOCK_EMAIL,
    MOCK_PASSWORD,
    MOCK_REFRESH_TOKEN,
    MOCK_USER_ID,
)

USER_INPUT = {CONF_EMAIL: MOCK_EMAIL, CONF_PASSWORD: MOCK_PASSWORD}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Iterator[AsyncMock]:
    """Stop the flow from setting the entry up for real.

    These tests are about the flow itself; setup is covered elsewhere.
    """
    with patch(
        "custom_components.current.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def start_user_flow(hass: HomeAssistant) -> dict:
    """Open the user step and submit the default credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


async def test_user_flow_creates_entry(
    hass: HomeAssistant, patched_session: FakeSession
) -> None:
    """Valid credentials create an entry holding tokens, not the password."""
    result = await start_user_flow(hass)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MOCK_EMAIL
    assert result["data"] == {
        CONF_EMAIL: MOCK_EMAIL,
        CONF_ACCESS_TOKEN: MOCK_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN: MOCK_REFRESH_TOKEN,
        CONF_CUSTOMER_ID: MOCK_CUSTOMER_ID,
        CONF_USER_ID: MOCK_USER_ID,
    }
    assert result["result"].unique_id == MOCK_EMAIL


async def test_unique_id_ignores_email_case(
    hass: HomeAssistant, patched_session: FakeSession, mock_config_entry
) -> None:
    """The same account typed with different capitals is still a duplicate."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_EMAIL: MOCK_EMAIL.upper()}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_invalid_auth_shown_and_recoverable(
    hass: HomeAssistant, patched_session: FakeSession, api: CurrentApiMock
) -> None:
    """A rejected password re-shows the form, then succeeds when corrected."""
    api.login_status = 401

    result = await start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    api.login_status = 200
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cannot_connect(
    hass: HomeAssistant, patched_session: FakeSession, api: CurrentApiMock
) -> None:
    """A server error is reported as a connection problem."""
    api.login_status = 500

    result = await start_user_flow(hass)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unexpected_login_shape(
    hass: HomeAssistant, patched_session: FakeSession, api: CurrentApiMock
) -> None:
    """A login response missing the expected fields is an unknown error."""
    api.login_body = {"Result": {"accessToken": MOCK_ACCESS_TOKEN}}

    result = await start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_unexpected_exception(
    hass: HomeAssistant, patched_session: FakeSession
) -> None:
    """Anything else is caught and reported, not raised into the UI."""
    with patch(
        "custom_components.current.config_flow.CurrentApiClient.login",
        side_effect=ValueError("boom"),
    ):
        result = await start_user_flow(hass)
    assert result["errors"] == {"base": "unknown"}


# -- reauth ---------------------------------------------------------------


async def test_reauth_stores_new_tokens(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-authentication replaces the stored tokens on the existing entry."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_ACCESS_TOKEN: "stale",
            CONF_REFRESH_TOKEN: "stale",
        },
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: MOCK_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == MOCK_ACCESS_TOKEN
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == MOCK_REFRESH_TOKEN
    assert CONF_PASSWORD not in mock_config_entry.data
    # The stored email is used, not asked for again.
    assert api.requests[-1]["json"]["Email"] == MOCK_EMAIL


async def test_reauth_rejects_wrong_password(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A still-wrong password keeps the form open and the entry unchanged."""
    mock_config_entry.add_to_hass(hass)
    before = dict(mock_config_entry.data)
    api.login_status = 401

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert dict(mock_config_entry.data) == before


@pytest.mark.parametrize(
    ("break_login", "error"),
    [
        (lambda api: setattr(api, "login_status", 500), "cannot_connect"),
        (lambda api: setattr(api, "connection_error", True), "cannot_connect"),
        (
            lambda api: setattr(
                api, "login_body", {"Result": {"accessToken": MOCK_ACCESS_TOKEN}}
            ),
            "unknown",
        ),
    ],
    ids=["server_error", "connection_error", "unexpected_response"],
)
async def test_reauth_error_then_recovers(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    api_responses: dict,
    mock_config_entry: MockConfigEntry,
    break_login,
    error: str,
) -> None:
    """A failed attempt keeps the form open, the entry untouched, and can retry."""
    mock_config_entry.add_to_hass(hass)
    before = dict(mock_config_entry.data)
    break_login(api)

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: MOCK_PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert dict(mock_config_entry.data) == before

    api.login_status = 200
    api.connection_error = False
    api.login_body = api_responses["login"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: MOCK_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_unexpected_exception(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Anything else is caught and reported, not raised into the UI."""
    mock_config_entry.add_to_hass(hass)
    before = dict(mock_config_entry.data)

    result = await mock_config_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.current.config_flow.CurrentApiClient.login",
        side_effect=ValueError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: MOCK_PASSWORD}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert dict(mock_config_entry.data) == before


async def test_reauth_reloads_the_entry(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
) -> None:
    """Successful re-authentication sets the entry up again with the new tokens."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: MOCK_PASSWORD}
    )
    await hass.async_block_till_done()

    mock_setup_entry.assert_called_once()
    (_, entry), _ = mock_setup_entry.call_args
    assert entry.entry_id == mock_config_entry.entry_id
