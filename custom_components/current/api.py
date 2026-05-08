import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL, APP_ID

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class CannotConnectError(Exception):
    pass


class CurrentApiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        customer_id: int,
        user_id: int,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._customer_id = customer_id
        self._user_id = user_id

    @staticmethod
    async def login(
        session: aiohttp.ClientSession, email: str, password: str
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}/v2/Users/Authenticate"
        body = {
            "appID": APP_ID,
            "Email": email,
            "Password": password,
            "pushState": "unknown",
            "appToken": None,
            "TimeZone": "UTC",
        }
        try:
            async with session.post(url, json=body) as resp:
                if resp.status in (401, 403):
                    raise AuthError("Invalid credentials")
                if not resp.ok:
                    raise CannotConnectError(f"Login failed with status {resp.status}")
                data = await resp.json()
                unwrapped = data.get("Result", data) if isinstance(data, dict) else data
                _LOGGER.debug("Login response keys: %s", list(unwrapped.keys()) if isinstance(unwrapped, dict) else type(unwrapped))
                return unwrapped
        except aiohttp.ClientError as err:
            raise CannotConnectError(f"Connection error: {err}") from err

    async def _refresh_access_token(self) -> None:
        url = f"{API_BASE_URL}/v2/Security/RefreshAccessTokenInternal"
        body = {
            "appID": APP_ID,
            "rToken": self._refresh_token,
            "pushToken": None,
            "TimeZone": "UTC",
        }
        try:
            async with self._session.post(url, json=body) as resp:
                if not resp.ok:
                    raise AuthError("Token refresh failed")
                data = await resp.json()
                # Response structure: {"Result": {"datas": "<new_access_token>"}}
                new_token = data.get("Result", {}).get("datas") or data.get("datas") or data
                if not isinstance(new_token, str):
                    raise AuthError(f"Unexpected refresh response: {data}")
                self._access_token = new_token
        except aiohttp.ClientError as err:
            raise CannotConnectError(f"Connection error during refresh: {err}") from err

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{API_BASE_URL}/v2/{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with self._session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status == 401:
                    raise AuthError("Unauthorized")
                if not resp.ok:
                    raise CannotConnectError(f"Request to {path} failed: {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise CannotConnectError(f"Connection error: {err}") from err

    async def _request_with_refresh(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await self._request(method, path, **kwargs)
        except AuthError:
            _LOGGER.debug("Access token expired, refreshing")
            await self._refresh_access_token()
            return await self._request(method, path, **kwargs)

    async def get_chargers(self) -> list[dict]:
        data = await self._request_with_refresh(
            "GET", "ChargePoints/my-points", params={"customerID": self._customer_id}
        )
        # Response: {"Result": {"success": true, "datas": [...]}}
        return (data.get("Result") or {}).get("datas") or []

    async def get_ongoing_session(self) -> list[dict]:
        data = await self._request_with_refresh(
            "GET", f"sessions/user/{self._user_id}/active"
        )
        # Response: {"Result": [...]}
        result = data.get("Result")
        if isinstance(result, list):
            return result
        return []

    async def start_charging(self, charge_point_id: int) -> dict:
        return await self._request_with_refresh(
            "POST",
            "Commands/RemoteStart",
            json={
                "FK_ChargingPointID": charge_point_id,
                "FK_CustomerID": self._customer_id,
                "Origin": "App",
            },
        )

    async def stop_charging(self, box_id: str | int, session_id: str | int) -> dict:
        return await self._request_with_refresh(
            "GET", f"Commands/RemoteStop/{box_id}/{session_id}"
        )

    async def get_history(self, count: int = 5) -> dict:
        data = await self._request_with_refresh(
            "GET",
            f"ChargingHistory/customers/{self._customer_id}",
            params={
                "number": count,
                "startIndex": 0,
                "fromDateTimestamp": 0,
                "toDateTimestamp": 0,
                "calculateTotalPrice": "true",
            },
        )
        return data.get("Result", data) if isinstance(data, dict) else data

    async def set_authentication(self, box_id: int, enabled: bool) -> dict:
        return await self._request_with_refresh(
            "GET", f"Commands/SetDefaultAuthentication/{box_id}/{str(enabled).lower()}"
        )

    async def set_cable_lock(self, charge_point_id: int, enabled: bool) -> dict:
        return await self._request_with_refresh(
            "GET", f"Commands/SetDefaultPermanentCableLocking/{charge_point_id}/{str(enabled).lower()}"
        )

    async def restart_charger(self, box_id: int) -> dict:
        return await self._request_with_refresh(
            "GET", f"Commands/Reset/{box_id}/1"
        )

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token
