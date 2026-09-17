# CURRENT EV Charging

![GitHub Release](https://img.shields.io/github/v/release/aunefyren/current?style=for-the-badge)
![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/aunefyren/current/total?style=for-the-badge)
![GitHub issues](https://img.shields.io/github/issues/aunefyren/current?style=for-the-badge)
![GitHub Repo stars](https://img.shields.io/github/stars/aunefyren/current?style=for-the-badge)
![GitHub forks](https://img.shields.io/github/forks/aunefyren/current?style=for-the-badge)

> [!NOTE]
> Parts of this integration were co-written with Claude (Anthropic AI). The code has been reviewed and tested against real CURRENT hardware.

A Home Assistant integration for the [CURRENT EV charging platform](https://current.eco). Targets apartment and residential tenants who have a Zaptec charger managed by CURRENT, and want to start/stop charging and monitor session status from Home Assistant.

<br>

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aunefyren&repository=current)  
Must be added as a custom repository.

<br>

## Features

- UI-based setup — no `configuration.yaml` editing required
- Start and stop charging via a switch entity
- Live session monitoring (power, current, energy, charging duration)
- Charger status: `Available`, `Charging`, `Standby` (car full/paused), `Unavailable`
- Last session summary (energy and cost in account currency)
- Charger controls: require authentication, permanent cable lock, restart
- Multiple chargers supported — each appears as a separate device

<br>

## Entities

Each charger appears as its own device. The following entities are created per charger:

### Sensors
| Entity | Description |
|---|---|
| Charger Status | `Available`, `Charging`, `Standby`, or `Unavailable`. CURRENT's own status is in the `current_status` attribute |
| Session Energy | kWh delivered in the current session |
| Charging Duration | Minutes of active power delivery this session |
| Live Power | Current power draw in kW (0 when idle) |
| Live Current | Current draw in amps, with per-phase values as attributes (0 when idle) |
| State of Charge | Battery % (if reported by the car over OCPP) |
| Last Session Energy | kWh delivered in the last completed session |
| Last Session Cost | Cost of the last completed session (account currency) |

### Controls
| Entity | Type | Description |
|---|---|---|
| EV Charging | Switch | Start/stop the charging session |
| Require Authentication | Switch | Toggle RFID/app authentication requirement |
| Cable Lock | Switch | Toggle permanent cable locking |
| Restart Charger | Button | Send a reset command to the charger |

<br>

## Installation

1. Add this repo to HACS as a custom repository
2. Install **CURRENT** in HACS
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for **CURRENT**
5. Enter your CURRENT account email and password

<br>

## Known limitations

- The CURRENT API caches session data server-side and typically updates once per minute, so sensor values may lag up to ~60 seconds after charging starts or stops.
- This integration uses the same API endpoints as the CURRENT mobile app. These are not officially documented as a third-party API and may change without notice.
- Chargers added to your CURRENT account after the integration is set up will not appear automatically — reload the integration from Settings → Integrations to pick them up.

<br>

## Development

Tests run against [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) and need Python 3.13:

```sh
pip install -r requirements_test.txt
pytest tests
ruff check custom_components tests dev
```

The API is faked in `tests/conftest.py` using responses captured from a real account, stored in `tests/fixtures/api_responses.json`. To refresh them after CURRENT changes its API:

1. `CURRENT_EMAIL=you@example.com python3 dev/probe_api.py` logs in, calls the read-only endpoints the integration uses, and writes `current-probe-raw.json` (private) and `current-probe-scrubbed.json` (identifiers redacted). It never sends charger commands. Logging in issues new tokens, so Home Assistant may ask you to re-authenticate afterwards.
2. Check the scrubbed file for anything personal, then run `python3 dev/make_fixtures.py` to rebuild the fixtures with fake identifiers.

The probe captures whichever state the charger is in, and the other state is derived from it: an idle capture gets a charging session built from the newest history session, and a charging capture gets an idle charger with its live readings zeroed. The notes at the top of the fixture file say which parts are derived.

<br>

## Ideas for further development

- Automatic re-registration when charger list changes (no reload required)
- Charging schedule / smart charging controls
