"""Fixtures for the CURRENT tests.

The API is faked at the session boundary rather than by patching
CurrentApiClient, so the client's own request building, error mapping and
token refresh are all exercised by the tests that use it.
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.current.const import (
    API_BASE_URL,
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

from .const import (
    MOCK_ACCESS_TOKEN,
    MOCK_CHARGE_POINT_ID,
    MOCK_CUSTOMER_ID,
    MOCK_EMAIL,
    MOCK_REFRESH_TOKEN,
    MOCK_REFRESHED_TOKEN,
    MOCK_USER_ID,
    SECOND_BOX_ID,
    SECOND_CHARGE_POINT_ID,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
API_PREFIX = f"{API_BASE_URL}/v2/"

# pytest-homeassistant-custom-component turns SQLAlchemy's statement logging on
# at INFO, which buries the test results under the recorder's inserts.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Let every test load this integration.

    `recorder_mock` is requested first on purpose: the integration depends on
    the recorder, and the recorder's database fixture asserts that it is built
    before `hass` exists.
    """
    return


def load_responses() -> dict[str, Any]:
    """Load the API responses captured from a real account."""
    return json.loads((FIXTURES / "api_responses.json").read_text())


class FakeResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: Any) -> None:
        """Store the status and body this response will return."""
        self.status = status
        self._payload = payload

    @property
    def ok(self) -> bool:
        """Mirror aiohttp: anything below 400 is ok."""
        return self.status < 400

    async def json(self) -> Any:
        """Return the decoded body."""
        return copy.deepcopy(self._payload)

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the response context."""
        return


class CurrentApiMock:
    """Answers requests the way the CURRENT API does."""

    def __init__(self, responses: dict[str, Any]) -> None:
        """Prepare the endpoint from captured responses."""
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

        # Knobs for individual tests.
        self.status: int | None = None  # force this status on every request
        self.connection_error = False
        self.login_status = 200
        self.login_body: Any = responses["login"]
        self.refresh_status = 200
        self.refresh_body: Any = responses["refresh"]
        self.command_status: int | None = None  # fail charger commands only
        self.forbidden_paths: set[str] = set()
        self.charging = False
        # Live readings to change on the charging charger, e.g. {"LivekW": 0.0}.
        self.live_overrides: dict[str, Any] = {}
        self.chargers: list[dict[str, Any]] = responses["chargers_idle"]["Result"][
            "datas"
        ]
        self.history: dict[str, Any] = responses["history"]["Result"]
        # Largest page the endpoint hands out, whatever `number` asks for.
        self.history_page_limit: int | None = None

        # Tokens the server currently accepts.
        self.valid_tokens = {MOCK_ACCESS_TOKEN}

    # -- test helpers -----------------------------------------------------

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """Return the active sessions the endpoint reports."""
        if not self.charging:
            return self.responses["ongoing_idle"]["Result"]
        return self.responses["ongoing_charging"]["Result"]

    def chargers_response(self) -> list[dict[str, Any]]:
        """Return the chargers, with live readings on the one charging.

        CURRENT reports power and current on the charger rather than the
        session, so charging changes this response as well as the sessions.
        """
        chargers = copy.deepcopy(self.chargers)
        if not self.charging:
            return chargers
        captured = self.responses["chargers_charging"]["Result"]["datas"][0]
        live = {
            **captured["ExtraInformation"]["GenericPoint"],
            **self.live_overrides,
        }
        for charger in chargers:
            if charger["FK_ChargePointID"] == MOCK_CHARGE_POINT_ID:
                charger["ExtraInformation"]["GenericPoint"].update(live)
        return chargers

    def expire_token(self) -> None:
        """Make the server reject the access token it issued at login."""
        self.valid_tokens.discard(MOCK_ACCESS_TOKEN)

    def add_second_charger(self) -> None:
        """Give the account a second charger with a session of its own."""
        charger = copy.deepcopy(self.chargers[0])
        charger.update(
            FK_ChargePointID=SECOND_CHARGE_POINT_ID,
            FK_ChargingBoxID=SECOND_BOX_ID,
            Name="Second Charger",
            IsAuthenticationEnabled=False,
            isPermanentCableLockingEnabled=False,
        )
        self.chargers = [*self.chargers, charger]

        item = copy.deepcopy(self.history["List"][1])
        item["ChargePointID"] = SECOND_CHARGE_POINT_ID
        item["Session"].update(
            ChargingPointID=SECOND_CHARGE_POINT_ID, ChargingBoxID=SECOND_BOX_ID
        )
        self.history = {**self.history, "List": [item, *self.history["List"]]}

    def set_history_length(self, count: int) -> None:
        """Replace the history with `count` sessions, one a day, newest first.

        Each is a copy of the first captured session, moved back a day at a
        time, with its own id.
        """
        template = self.history["List"][0]
        start = datetime.fromisoformat(template["Session"]["SessionStart"])
        end = datetime.fromisoformat(template["Session"]["SessionEnd"])
        items = []
        for n in range(count):
            item = copy.deepcopy(template)
            item["Session"].update(
                PK_ServiceSessionID=100000 + n,
                SessionStart=(start - timedelta(days=n)).isoformat(),
                SessionEnd=(end - timedelta(days=n)).isoformat(),
            )
            items.append(item)
        self.history = {**self.history, "List": items, "TotalOrders": count}

    def history_requests(self) -> list[dict[str, Any]]:
        """Return the history requests that were made."""
        return [r for r in self.requests if r["path"].startswith("ChargingHistory/")]

    def history_response(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return one page of history, as `number` and `startIndex` ask."""
        number = int(params.get("number", 5))
        if self.history_page_limit is not None:
            number = min(number, self.history_page_limit)
        start = int(params.get("startIndex", 0))
        return {**self.history, "List": self.history["List"][start : start + number]}

    def commands(self) -> list[dict[str, Any]]:
        """Return the charger commands that were sent."""
        return [r for r in self.requests if r["path"].startswith("Commands/")]

    # -- request handling -------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Answer one request."""
        assert url.startswith(API_PREFIX), url
        path = url.removeprefix(API_PREFIX)
        headers = dict(kwargs.get("headers") or {})
        self.requests.append(
            {
                "method": method,
                "path": path,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
                "headers": headers,
            }
        )

        if self.connection_error:
            raise aiohttp.ClientConnectionError("fake connection failure")
        if self.status is not None:
            return FakeResponse(self.status, {})

        if path == "Users/Authenticate":
            return FakeResponse(self.login_status, self.login_body)
        if path == "Security/RefreshAccessTokenInternal":
            return self._refresh(kwargs.get("json") or {})

        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return FakeResponse(401, self.responses["unauthorized"])
        if path in self.forbidden_paths:
            return FakeResponse(403, {})

        if method == "GET" and path == "ChargePoints/my-points":
            return FakeResponse(
                200, {"Result": {"success": True, "datas": self.chargers_response()}}
            )
        if method == "GET" and path == f"sessions/user/{MOCK_USER_ID}/active":
            return FakeResponse(200, {"Result": self.sessions})
        if method == "GET" and path == f"ChargingHistory/customers/{MOCK_CUSTOMER_ID}":
            return FakeResponse(
                200, {"Result": self.history_response(kwargs.get("params") or {})}
            )
        if path.startswith("Commands/"):
            if self.command_status is not None:
                return FakeResponse(self.command_status, {})
            return FakeResponse(200, {"Result": {"success": True}})

        return FakeResponse(404, {"title": f"unhandled {method} {path}"})

    def _refresh(self, body: dict[str, Any]) -> FakeResponse:
        if self.refresh_status != 200 or body.get("rToken") != MOCK_REFRESH_TOKEN:
            return FakeResponse(self.refresh_status, {})
        self.valid_tokens.add(MOCK_REFRESHED_TOKEN)
        return FakeResponse(200, self.refresh_body)


class FakeSession:
    """aiohttp session stand-in that routes everything to CurrentApiMock."""

    def __init__(self, api: CurrentApiMock) -> None:
        """Wrap a fake endpoint in a session-shaped object."""
        self.api = api

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Route a request to the fake endpoint."""
        return self.api.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """Route a GET to the fake endpoint."""
        return self.api.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Route a POST to the fake endpoint."""
        return self.api.request("POST", url, **kwargs)


@pytest.fixture
def api_responses() -> dict[str, Any]:
    """Return the API responses captured from a real account."""
    return load_responses()


@pytest.fixture
def api(api_responses: dict[str, Any]) -> CurrentApiMock:
    """Return a configurable fake CURRENT endpoint."""
    return CurrentApiMock(api_responses)


@pytest.fixture
def session(api: CurrentApiMock) -> FakeSession:
    """Return a fake aiohttp session backed by the fake endpoint."""
    return FakeSession(api)


@pytest.fixture
def patched_session(session: FakeSession) -> Iterator[FakeSession]:
    """Hand the fake session to everything that asks Home Assistant for one."""
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        yield session


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry as the config flow creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_EMAIL,
        unique_id=MOCK_EMAIL,
        data={
            CONF_EMAIL: MOCK_EMAIL,
            CONF_ACCESS_TOKEN: MOCK_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: MOCK_REFRESH_TOKEN,
            CONF_CUSTOMER_ID: MOCK_CUSTOMER_ID,
            CONF_USER_ID: MOCK_USER_ID,
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry to hass and set it up."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id(
    hass: HomeAssistant,
    platform: str,
    key: str,
    charge_point_id: int = MOCK_CHARGE_POINT_ID,
) -> str:
    """Look an entity up by its unique id, so tests survive renames."""
    registry = er.async_get(hass)
    found = registry.async_get_entity_id(
        platform, DOMAIN, f"current_{charge_point_id}_{key}"
    )
    assert found, f"no {platform} entity current_{charge_point_id}_{key}"
    return found
