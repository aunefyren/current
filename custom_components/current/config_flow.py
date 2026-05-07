import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import aiohttp_client

from .api import AuthError, CannotConnectError, CurrentApiClient
from .const import CONF_ACCESS_TOKEN, CONF_CUSTOMER_ID, CONF_REFRESH_TOKEN, CONF_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class CurrentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                        list(login_data.keys()) if isinstance(login_data, dict) else login_data,
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
