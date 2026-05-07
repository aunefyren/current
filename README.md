# CURRENT EV Charging — Home Assistant Integration

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
- Live session monitoring (energy delivered, charging duration)
- Last session summary (total energy and cost)
- Charger status sensor (`available` / `charging` / `unavailable`)

<br>

## Entities

| Entity | Type | Description |
|---|---|---|
| EV Charging | Switch | Start/stop the charging session |
| Charger Status | Sensor | `available`, `charging`, or `unavailable` |
| Session Energy | Sensor | kWh delivered in the current session |
| Charging Duration | Sensor | Minutes of active power delivery this session |
| Last Session Energy | Sensor | kWh delivered in the last completed session |
| Last Session Cost | Sensor | Cost of the last completed session (NOK) |

<br>

## Installation

1. Add this repo to HACS as a custom repository
2. Install **CURRENT** in HACS
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for **CURRENT**
5. Enter your CURRENT account email and password

<br>

## Known limitations

- The CURRENT API caches session data and typically updates once per minute, so sensor values may lag up to ~60 seconds after charging starts or stops.
- This integration uses the same API endpoints as the CURRENT mobile app. These are not officially documented as a third-party API and may change without notice.
- Only the first linked charger is used. Multiple chargers are not currently supported.

<br>

## Ideas for further development

- Support for multiple chargers
- Currency unit from account preferences (currently hardcoded to NOK)
- Charging schedule / smart charging controls
