"""Config flow for CURRENT EV Charging."""

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import aiohttp_client

from .api import AuthError, CannotConnectError, CurrentApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class CurrentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setting up and re-authenticating a CURRENT account."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                session = aiohttp_client.async_get_clientsession(self.hass)
                login_data = await CurrentApiClient.login(session, email, password)
            except AuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during CURRENT login")
                errors["base"] = "unknown"
            else:
                try:
                    access_token = login_data["accessToken"]
                    refresh_token = login_data["rToken"]
                    customer = login_data["customer"]
                    customer_id = customer["PK_CustomerID"]
                    user_id = customer["FK_UserID"]
                except KeyError:
                    _LOGGER.error(
                        "Unexpected login response structure. Got keys: %s",
                        list(login_data.keys())
                        if isinstance(login_data, dict)
                        else login_data,
                    )
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(email.lower())
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=email,
                        data={
                            CONF_EMAIL: email,
                            CONF_ACCESS_TOKEN: access_token,
                            CONF_REFRESH_TOKEN: refresh_token,
                            CONF_CUSTOMER_ID: customer_id,
                            CONF_USER_ID: user_id,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the stored tokens stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and store fresh tokens."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                session = aiohttp_client.async_get_clientsession(self.hass)
                login_data = await CurrentApiClient.login(
                    session, reauth_entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except AuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during CURRENT reauth")
                errors["base"] = "unknown"
            else:
                try:
                    access_token = login_data["accessToken"]
                    refresh_token = login_data["rToken"]
                    customer = login_data["customer"]
                    customer_id = customer["PK_CustomerID"]
                    user_id = customer["FK_UserID"]
                except KeyError:
                    _LOGGER.error(
                        "Unexpected login response during reauth. Got keys: %s",
                        list(login_data.keys())
                        if isinstance(login_data, dict)
                        else login_data,
                    )
                    errors["base"] = "unknown"
                else:
                    self.hass.config_entries.async_update_entry(
                        reauth_entry,
                        data={
                            **reauth_entry.data,
                            CONF_ACCESS_TOKEN: access_token,
                            CONF_REFRESH_TOKEN: refresh_token,
                            CONF_CUSTOMER_ID: customer_id,
                            CONF_USER_ID: user_id,
                        },
                    )
                    await self.hass.config_entries.async_reload(reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": reauth_entry.data[CONF_EMAIL]},
            errors=errors,
        )
