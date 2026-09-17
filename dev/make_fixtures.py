#!/usr/bin/env python3
"""Turn a redacted probe dump into test fixtures.

Tests are worth more when they run against response shapes the real API
actually produced, rather than shapes we imagined. This reads
current-probe-scrubbed.json and writes tests/fixtures/api_responses.json with
the placeholder identifiers swapped for stable, obviously fake ones.

Placeholders are numbered by the value they replaced, so a charger id that
appears as FK_ChargePointID in one response and ChargingPointID in another
becomes the same fake id in both, and the integration's lookups still line up.

Usage:
    python3 dev/make_fixtures.py
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import sys

# Stable stand-ins for the identifiers tests refer to by name. The tests'
# constants must use the same values.
PINNED_INTS = {
    "PK_CustomerID": 1001,
    "PK_UserID": 2001,
    "FK_ChargePointID": 3001,
    "FK_ChargingBoxID": 4001,
    "StationID": 5001,
}
EMAIL = "user@example.com"
UUID = "00000000-0000-4000-8000-000000000000"

# The redacted identifiers were numbers except for these. Checked against a
# raw dump; the scrubbed one no longer says.
STRING_ID_KEYS = {"DeviceID", "RFID", "TimezoneID", "traceId"}
INT_KEYS = {"rfid_scan_type", "DeviceIDMaxLength"}
FLOAT_KEYS = {"TotalCostExcludingVAT", "TotalCostExcludingVATLastPeriod"}
ID_KEY = re.compile(r"^(PK|FK)_|I[Dd]$")

FAKE_STRINGS = {
    "accessToken": "access-token",
    "rToken": "refresh-token",
    "Email": EMAIL,
    "CustomerEmail": EMAIL,
    "FirstName": "Test",
    "LastName": "User",
    "CustomerName": "Test User",
    "CustomerFullName": "Test User",
    "Identificator": "CUST0001",
    "CustomerIdentificator": "CUST0001",
    "CustomerIdentifier": "CUST0001",
    "Name": "Test Charger",
    "ChargePointName": "Test Charger",
    "StationName": "Test Station",
    "StationAddress": "Testveien 1",
    "StationPostalCode": "0001",
    "StationState": "Testfylke",
    "ChargerCode": "TEST",
    "RFID": "RFID0001",
    "TimezoneID": "W. Europe Standard Time",
    "OperatorName": "Test Operator",
}

PLACEHOLDER = re.compile(r"^<(\w+):(\d+)>$")

# Fields the live sensors read from an ongoing session, with plausible values
# for a car mid-charge. Used only when the probe caught no active session.
CHARGING_VALUES = {
    "SessionEnd": None,
    "StopReason": None,
    "LivekW": 11.0,
    "Amps_Export": 16.0,
    "TotalkWh": 12.5,
    "Duration": 4200,
    "DurationCharging": 4080,
}


def fake_value(key: str, index: int, pinned: dict[int, int]):
    """Return a deterministic fake for one placeholder."""
    if index in pinned:
        return pinned[index]
    if key in FLOAT_KEYS:
        return float(index)
    if key in INT_KEYS or (ID_KEY.search(key) and key not in STRING_ID_KEYS):
        return 90000 + index
    return FAKE_STRINGS.get(key, f"{key}-{index}")


def pin_indexes(obj, pinned: dict[int, int]) -> None:
    """Find which placeholder numbers belong to the pinned identifiers."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                isinstance(value, str)
                and key in PINNED_INTS
                and (m := PLACEHOLDER.match(value))
            ):
                pinned[int(m.group(2))] = PINNED_INTS[key]
            pin_indexes(value, pinned)
    elif isinstance(obj, list):
        for value in obj:
            pin_indexes(value, pinned)


def defake(obj, pinned: dict[int, int], key: str | None = None):
    """Replace placeholders with fake values, keeping the response shape."""
    if isinstance(obj, dict):
        return {k: defake(v, pinned, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [defake(v, pinned, key) for v in obj]
    if not isinstance(obj, str):
        return obj
    if m := PLACEHOLDER.match(obj):
        return fake_value(m.group(1), int(m.group(2)), pinned)
    if obj == "<redacted>":
        # A whole value that matched something redacted elsewhere.
        return {"Username": EMAIL, "datas": "refreshed-access-token"}.get(
            key, "redacted"
        )
    return obj.replace("<uuid>", UUID)


def leftovers(obj, path: str = "") -> list[str]:
    """List anything that still looks like a placeholder."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in leftovers(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in leftovers(v, f"{path}[{i}]")]
    if isinstance(obj, str) and re.search(
        r"<(\w+:\d+|redacted|uuid|token|email)>", obj
    ):
        return [f"{path} = {obj}"]
    return []


def main() -> int:
    """Write the fixture file from a probe dump."""
    src = pathlib.Path("current-probe-scrubbed.json")
    if not src.exists():
        print(f"{src} not found; run dev/probe_api.py first", file=sys.stderr)
        return 1

    dump = json.loads(src.read_text())
    queries = dump["queries"]

    def body(label: str, status: int = 200):
        entry = queries.get(label)
        if not entry or entry["response"].get("status") != status:
            raise SystemExit(f"probe dump has no usable {label!r} response")
        return entry["response"]["body"]

    raw = {
        "login": body("login"),
        "refresh": body("refresh"),
        "unauthorized": body("unauthorized", 401),
        "chargers": body("chargers"),
        "history": body("history"),
        "ongoing": body("ongoing"),
    }

    pinned: dict[int, int] = {}
    pin_indexes(raw, pinned)
    fixtures = {label: defake(value, pinned) for label, value in raw.items()}

    notes = {
        "source": (
            f"derived from a redacted dev/probe_api.py run at {dump.get('probedAt')}"
        ),
    }

    # Idle and charging variants of the active-session response.
    sessions = fixtures.pop("ongoing")["Result"]
    fixtures["ongoing_idle"] = {"Result": []}
    if sessions:
        fixtures["ongoing_charging"] = {"Result": sessions}
        notes["ongoing_charging"] = "captured from a live session"
    else:
        # No car was charging during the probe. History sessions share the
        # active-session shape, so the newest one stands in, with its live
        # fields set as if mid-charge. Re-probe while charging to replace it.
        session = copy.deepcopy(fixtures["history"]["Result"]["List"][0]["Session"])
        session.update(CHARGING_VALUES)
        fixtures["ongoing_charging"] = {"Result": [session]}
        notes["ongoing_charging"] = (
            "derived from the newest history session; overridden fields: "
            + ", ".join(CHARGING_VALUES)
        )

    if problems := leftovers(fixtures):
        print("placeholders survived:", *problems, sep="\n  ", file=sys.stderr)
        return 1

    out = {"_notes": notes, **fixtures}
    dest = pathlib.Path("tests/fixtures/api_responses.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"wrote {dest}")
    for label, note in notes.items():
        print(f"  {label}: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
