#!/usr/bin/env python3
"""One-off, read-only probe of the CURRENT API.

Logs in, then calls every read endpoint the integration uses (plus a few
variations of them) and dumps the responses, so the integration and its tests
can be built against real response shapes instead of guessed ones.

Credentials never leave your machine. Two files are written:

  current-probe-raw.json        full responses, NOT redacted -- keep private
  current-probe-scrubbed.json   same, with identifiers redacted -- safe to share

Only the scrubbed file is meant to be shared. Look it over before you do.

The probe never controls a charger. CURRENT exposes its commands (start, stop,
reset, authentication, cable lock) as plain GETs under Commands/, so requests
are checked against that before they are sent, not merely left out.

Logging in and refreshing issue new tokens. If CURRENT only allows one session
per account, the running Home Assistant integration may be asked to
re-authenticate afterwards.

Usage:
    CURRENT_EMAIL=you@example.com python3 dev/probe_api.py
    (password is prompted for, never taken from argv)

Optional:
    CURRENT_SKIP_REFRESH=1        do not exercise the token refresh endpoint
    CURRENT_EXTRA_PATHS=a,b       extra read-only GET paths under /v2/ to try;
                                  {customer_id} and {user_id} are filled in

    python3 dev/probe_api.py --rescrub
        rebuild current-probe-scrubbed.json from the raw dump, without logging
        in again (after changing the redaction rules)
"""

from __future__ import annotations

import base64
import datetime as dt
import getpass
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Reuse the integration's constants so the probe presents itself exactly like
# the integration does. const.py has no Home Assistant imports, so it loads
# standalone.
_CONST_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "current"
    / "const.py"
)
_spec = importlib.util.spec_from_file_location("current_const", _CONST_PATH)
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)

API = f"{const.API_BASE_URL}/v2"
UA = "ha-current-probe/0.1 (+https://github.com/aunefyren/current)"

# The only POSTs the probe may make. Everything else must be a GET, and no GET
# may touch Commands/.
ALLOWED_POSTS = {"Users/Authenticate", "Security/RefreshAccessTokenInternal"}
FORBIDDEN_PATH = re.compile(r"(^|/)commands(/|$)", re.IGNORECASE)

# Key fragments whose values identify you, your home, or your chargers. Keys
# are split on underscores and camelCase, so "PK_CustomerID" yields "id" and
# "rToken" yields "token", without "calculateTotalPrice" matching "lat".
# Energy, prices, durations and timestamps are deliberately kept.
SENSITIVE_WORDS = {
    "id",
    "guid",
    "uuid",
    "token",
    "email",
    "mail",
    "name",
    "address",
    "street",
    "city",
    "zip",
    "zipcode",
    "postal",
    "postcode",
    "phone",
    "mobile",
    "serial",
    "imei",
    "mac",
    "ip",
    "lat",
    "latitude",
    "lng",
    "lon",
    "longitude",
    "iban",
    "vat",
    "vin",
    "plate",
    "rfid",
    "card",
    "pin",
    "password",
    "identifier",
    "identificator",
}

# Identifying fields whose names give no hint of it.
SENSITIVE_KEYS = {
    "InvoiceNumber",
    "CompanyNumberInvoicing",
    "ChargerCode",
    "StationState",
}

_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

_redactions: dict[str, str] = {}
_seen_values: set[str] = set()


def key_is_sensitive(key: str | None) -> bool:
    """Return whether a field name looks like it holds an identifier."""
    if not key:
        return False
    if key in SENSITIVE_KEYS:
        return True
    words = {word.lower() for word in _KEY_WORD.findall(key)}
    return bool(words & SENSITIVE_WORDS)


def redact(obj, key: str | None = None, forced: bool = False):
    """Replace identifying values with stable placeholders, keeping shape.

    Everything beneath a sensitive key is redacted too, so an "address" object
    loses its lines even though "Line1" is not itself a sensitive name.
    """
    forced = forced or key_is_sensitive(key)
    if isinstance(obj, dict):
        return {k: redact(v, k, forced) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key, forced) for v in obj]
    if (
        forced
        and isinstance(obj, (str, int, float))
        and not isinstance(obj, bool)
        and obj != ""
    ):
        _seen_values.add(str(obj))
        # Numbered by value alone, so a charger id keeps the same placeholder
        # under FK_ChargePointID in one response and ChargingPointID in
        # another. The integration joins on those, and so will the fixtures.
        index = _redactions.setdefault(str(obj), str(len(_redactions)))
        return f"<{key}:{index}>"
    return obj


# Identifiers that turn up inside free text rather than in a field of their
# own: error messages quoting a value, URLs, tokens.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Tokens the probe knows about are already scrubbed by value; this catches any
# other JWT. A looser "long opaque string" pattern also ate URL paths.
JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")


def scrub_text(obj):
    """Second pass: strip identifiers embedded in free text.

    Runs after `redact` so it knows every value worth hunting for. Matches
    whole values only, so a customer id of 2026 does not eat into a date.
    """
    if isinstance(obj, dict):
        return {k: scrub_text(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_text(v) for v in obj]
    if not isinstance(obj, str) or re.fullmatch(r"<\w+:\d+>", obj):
        return obj

    text = obj
    for value in sorted(_seen_values, key=len, reverse=True):
        if len(value) < 3:
            continue
        pattern = rf"(?<![\w.-]){re.escape(value)}(?![\w.-])"
        text = re.sub(pattern, "<redacted>", text)
    text = EMAIL.sub("<email>", text)
    text = UUID.sub("<uuid>", text)
    text = JWT.sub("<token>", text)
    return text


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def token_summary(token: object) -> dict:
    """Describe a token without keeping anything that identifies the user."""
    if not isinstance(token, str):
        return {"_notAString": type(token).__name__}
    summary: dict = {"length": len(token), "looksLikeJwt": token.count(".") == 2}
    if summary["looksLikeJwt"]:
        try:
            claims = jwt_claims(token)
        except Exception as err:
            summary["_error"] = repr(err)
        else:
            summary["claimKeys"] = sorted(claims)
            if isinstance(claims.get("exp"), int) and isinstance(
                claims.get("iat"), int
            ):
                summary["lifetimeSeconds"] = claims["exp"] - claims["iat"]
    return summary


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    params: dict | None = None,
    body: dict | None = None,
    timeout: int = 60,
) -> dict:
    """Send one request and return its status and decoded body."""
    if FORBIDDEN_PATH.search(path):
        raise SystemExit(f"refusing to call {path}: commands control the charger")
    if method != "GET" and path not in ALLOWED_POSTS:
        raise SystemExit(f"refusing to {method} {path}: the probe is read-only")

    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "Origin": const.APP_ORIGIN,
        "X-App-Version": const.APP_VERSION,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, content_type, raw = (
                resp.status,
                resp.headers.get("Content-Type"),
                resp.read().decode(),
            )
    except urllib.error.HTTPError as err:
        status, content_type, raw = (
            err.code,
            err.headers.get("Content-Type"),
            err.read().decode()[:2000],
        )
    except Exception as err:
        return {"_error": repr(err)}

    try:
        decoded = json.loads(raw) if raw else None
    except ValueError:
        decoded = {"_text": raw[:2000]}
    return {"status": status, "contentType": content_type, "body": decoded}


def unwrap(response: dict):
    """Return the payload the integration would see, as api.py unwraps it."""
    body = response.get("body")
    return body.get("Result", body) if isinstance(body, dict) else body


def main() -> int:
    """Run the probe and write the dumps."""
    email = os.environ.get("CURRENT_EMAIL") or input("CURRENT email: ").strip()
    password = os.environ.get("CURRENT_PASSWORD") or getpass.getpass(
        "CURRENT password: "
    )
    _seen_values.add(email)

    out: dict = {
        "probedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "api": API,
        "appVersion": const.APP_VERSION,
        "queries": {},
    }

    def call(label: str, method: str, path: str, **kwargs) -> dict:
        print(f"  {label}: {method} {path}", file=sys.stderr)
        time.sleep(0.5)  # be a polite client
        res = request(method, path, **kwargs)
        out["queries"][label] = {
            "method": method,
            "path": path,
            "params": kwargs.get("params"),
            "response": res,
        }
        return res

    print("logging in...", file=sys.stderr)
    login = request(
        "POST",
        "Users/Authenticate",
        body={
            "appID": const.APP_ID,
            "Email": email,
            "Password": password,
            "pushState": "unknown",
            "appToken": None,
            "TimeZone": "UTC",
        },
    )
    out["queries"]["login"] = {
        "method": "POST",
        "path": "Users/Authenticate",
        "response": login,
    }
    result = unwrap(login)
    try:
        access_token = result["accessToken"]
        refresh_token = result["rToken"]
        customer_id = result["customer"]["PK_CustomerID"]
        user_id = result["customer"]["FK_UserID"]
    except (KeyError, TypeError):
        print(
            f"login failed (status {login.get('status')}); nothing else probed.",
            file=sys.stderr,
        )
        # Still worth keeping: a changed login shape is exactly what the probe
        # is for. The scrubbed dump is safe to share.
        write_dumps(out)
        return 1

    _seen_values.update({access_token, refresh_token})
    out["accessToken"] = token_summary(access_token)
    out["refreshToken"] = token_summary(refresh_token)

    # The calls the coordinator makes on every update, as it makes them.
    call(
        "chargers",
        "GET",
        "ChargePoints/my-points",
        token=access_token,
        params={"customerID": customer_id},
    )
    call("ongoing", "GET", f"sessions/user/{user_id}/active", token=access_token)
    history_params = {
        "number": 5,
        "startIndex": 0,
        "fromDateTimestamp": 0,
        "toDateTimestamp": 0,
        "calculateTotalPrice": "true",
    }
    call(
        "history",
        "GET",
        f"ChargingHistory/customers/{customer_id}",
        token=access_token,
        params=history_params,
    )
    # More history, to see sessions from more than one charger and the paging
    # fields.
    call(
        "history.20",
        "GET",
        f"ChargingHistory/customers/{customer_id}",
        token=access_token,
        params={**history_params, "number": 20},
    )
    # The statistics import pages through the whole history. startIndex should
    # skip that many sessions, so this page should repeat sessions 6-10 of
    # history.20; and a large page shows whether CURRENT caps its size.
    call(
        "history.page2",
        "GET",
        f"ChargingHistory/customers/{customer_id}",
        token=access_token,
        params={**history_params, "startIndex": 5},
    )
    call(
        "history.100",
        "GET",
        f"ChargingHistory/customers/{customer_id}",
        token=access_token,
        params={**history_params, "number": 100},
    )

    # What a rejected token looks like: 401 or 403, and with what body. This
    # decides when the client refreshes versus gives up.
    call(
        "unauthorized",
        "GET",
        "ChargePoints/my-points",
        token="not-a-valid-token",
        params={"customerID": customer_id},
    )

    if os.environ.get("CURRENT_SKIP_REFRESH"):
        print("  skipping token refresh", file=sys.stderr)
    else:
        refreshed = call(
            "refresh",
            "POST",
            "Security/RefreshAccessTokenInternal",
            body={
                "appID": const.APP_ID,
                "rToken": refresh_token,
                "pushToken": None,
                "TimeZone": "UTC",
            },
        )
        body = refreshed.get("body")
        new_token = None
        if isinstance(body, dict):
            new_token = (body.get("Result") or {}).get("datas") or body.get("datas")
        if isinstance(new_token, str):
            _seen_values.add(new_token)
            out["refreshedToken"] = token_summary(new_token)
            # Does the new token work, and does the old one keep working?
            call(
                "chargers.newToken",
                "GET",
                "ChargePoints/my-points",
                token=new_token,
                params={"customerID": customer_id},
            )
            call(
                "chargers.oldToken",
                "GET",
                "ChargePoints/my-points",
                token=access_token,
                params={"customerID": customer_id},
            )

    extra = os.environ.get("CURRENT_EXTRA_PATHS", "")
    for raw_path in filter(None, (p.strip() for p in extra.split(","))):
        path = raw_path.format(customer_id=customer_id, user_id=user_id)
        call(f"extra.{raw_path}", "GET", path, token=access_token)

    write_dumps(out)
    return 0


def write_dumps(out: dict) -> None:
    """Write the private and the shareable dump."""
    with open("current-probe-raw.json", "w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    write_scrubbed(out)


def write_scrubbed(out: dict) -> None:
    """Write the shareable dump from a raw one."""
    # The refreshed token sits under "datas", which is not a sensitive name.
    refresh = out.get("queries", {}).get("refresh", {}).get("response", {})
    new_token = unwrap(refresh) if refresh else None
    if isinstance(new_token, dict) and isinstance(new_token.get("datas"), str):
        _seen_values.add(new_token["datas"])

    # Redact each query's parts on their own. Handing the whole dump to
    # `redact` lets a label such as "chargers.newToken" mark everything
    # beneath it sensitive, and feed every value there to `scrub_text`.
    scrubbed = {k: v for k, v in out.items() if k != "queries"}
    scrubbed["queries"] = {
        label: {
            "method": entry.get("method"),
            "path": entry.get("path"),
            "params": redact(entry.get("params")),
            "response": redact(entry.get("response")),
        }
        for label, entry in out.get("queries", {}).items()
    }
    scrubbed = scrub_text(scrubbed)

    with open("current-probe-scrubbed.json", "w") as fh:
        json.dump(scrubbed, fh, indent=2, ensure_ascii=False)
    print("wrote current-probe-scrubbed.json (shareable)", file=sys.stderr)


def rescrub() -> int:
    """Rebuild the shareable dump from the raw one, without calling the API."""
    raw = pathlib.Path("current-probe-raw.json")
    if not raw.exists():
        print(f"{raw} not found; run the probe first", file=sys.stderr)
        return 1
    write_scrubbed(json.loads(raw.read_text()))
    return 0


if __name__ == "__main__":
    if "--rescrub" in sys.argv[1:]:
        raise SystemExit(rescrub())
    raise SystemExit(main())
