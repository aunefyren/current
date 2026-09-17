"""Write completed charging sessions into Home Assistant long-term statistics.

The live session sensors only know about a session while Home Assistant is
watching it, and record energy at the moment it was polled. CURRENT's charging
history knows every session the account ever had, with its start, end, energy
and cost. External statistics let us write those against the hours they
happened in, including sessions from before the integration was installed or
while Home Assistant was down.

The history only reports a total per session, not how it was spread over the
session, so each session's energy and cost are divided evenly over the time
from its start to its end.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import CurrentApiClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# CURRENT hands out at most 50 sessions a page, however many are asked for.
HISTORY_PAGE_SIZE = 50
# A safety stop, should CURRENT ignore startIndex and keep returning new ids.
MAX_HISTORY_PAGES = 100

ENERGY = "energy"
COST = "cost"

ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True, kw_only=True)
class CompletedSession:
    """The parts of a finished session that statistics are built from."""

    session_id: Any
    charge_point_id: int
    start: datetime
    end: datetime
    energy: float | None
    cost: float | None
    currency: str | None


def statistic_id_for(charge_point_id: int | str, kind: str) -> str:
    """Build the external statistic id for one charger's energy or cost.

    Recorder's VALID_STATISTIC_ID rejects anything outside [a-z0-9_], any
    double underscore, and leading or trailing underscores.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(charge_point_id).lower()).strip("_")
    return f"{DOMAIN}:charger_{slug or 'unknown'}_{kind}"


def _as_float(value: object) -> float | None:
    """Coerce an API number, tolerating nulls and unexpected types."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_time(value: object) -> datetime | None:
    """Parse a CURRENT timestamp into UTC.

    The history marks its timestamps as UTC. The active sessions endpoint
    leaves the zone off and means local time: a session probed at 13:08 CEST
    reported starting at 11:53 after charging for 74 minutes. Timestamps
    without a zone are therefore read in Home Assistant's time zone.
    """
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(parsed)


def parse_session(item: dict[str, Any]) -> CompletedSession | None:
    """Read one history item, or return None if it is not a finished session."""
    session = item.get("Session") or {}
    charge_point_id = item.get("ChargePointID") or session.get("ChargingPointID")
    start = parse_time(session.get("SessionStart"))
    end = parse_time(session.get("SessionEnd"))
    if charge_point_id is None or start is None or end is None:
        return None

    energy = _as_float(item.get("TotalkWH"))
    if energy is None:
        energy = _as_float(session.get("TotalkWh"))

    return CompletedSession(
        session_id=session.get("PK_ServiceSessionID") or (charge_point_id, start),
        charge_point_id=charge_point_id,
        start=start,
        end=end,
        energy=energy,
        cost=_as_float(item.get("TotalPrice")),
        currency=item.get("Currency"),
    )


def spread_over_hours(
    start: datetime, end: datetime, amount: float
) -> dict[datetime, float]:
    """Divide an amount over the hours between start and end.

    Each hour gets the share of the amount that its overlap with the session
    is of the whole session. A session with no length lands in its start hour.
    """
    first_hour = start.replace(minute=0, second=0, microsecond=0)
    if end <= start:
        return {first_hour: amount}

    length = (end - start).total_seconds()
    shares: dict[datetime, float] = {}
    hour = first_hour
    while hour < end:
        overlap = (min(hour + ONE_HOUR, end) - max(hour, start)).total_seconds()
        shares[hour] = amount * overlap / length
        hour += ONE_HOUR
    return shares


async def async_fetch_all_sessions(client: CurrentApiClient) -> list[CompletedSession]:
    """Page through the whole charging history, oldest session first."""
    sessions: dict[Any, CompletedSession] = {}
    offset = 0

    for _ in range(MAX_HISTORY_PAGES):
        result = await client.get_history(count=HISTORY_PAGE_SIZE, start_index=offset)
        result = result or {}
        items = result.get("List") or []
        offset += len(items)

        added = 0
        for item in items:
            parsed = parse_session(item)
            if parsed is not None and parsed.session_id not in sessions:
                sessions[parsed.session_id] = parsed
                added += 1

        if not added:
            break
        # TotalOrders counts every session, while the other totals only cover
        # the page returned. Without it, a short page is taken as the last,
        # though CURRENT may also cap how many sessions a page holds.
        total = result.get("TotalOrders")
        if isinstance(total, int) and not isinstance(total, bool):
            if offset >= total:
                break
        elif len(items) < HISTORY_PAGE_SIZE:
            break
    else:
        _LOGGER.warning(
            "Stopped reading charging history after %d pages", MAX_HISTORY_PAGES
        )

    return sorted(sessions.values(), key=lambda s: s.start)


def _build_statistics(
    sessions: list[CompletedSession], value_of: str
) -> list[StatisticData]:
    """Turn sessions into hourly buckets carrying a running total."""
    per_hour: dict[datetime, float] = defaultdict(float)
    for session in sessions:
        amount = getattr(session, value_of)
        if amount is None:
            continue
        for hour, share in spread_over_hours(
            session.start, session.end, amount
        ).items():
            per_hour[hour] += share

    total = 0.0
    statistics: list[StatisticData] = []
    for hour in sorted(per_hour):
        total += per_hour[hour]
        statistics.append(StatisticData(start=hour, state=total, sum=total))
    return statistics


def async_import_statistics(
    hass: HomeAssistant,
    sessions: list[CompletedSession],
    charger_names: dict[int, str],
) -> None:
    """Write energy and cost statistics for every charger in the history.

    The whole history is rewritten each time. Writing a bucket again replaces
    it, so a revised price or energy reading is corrected instead of counted
    twice.
    """
    by_charger: dict[int, list[CompletedSession]] = defaultdict(list)
    for session in sessions:
        by_charger[session.charge_point_id].append(session)

    for charge_point_id, charger_sessions in by_charger.items():
        name = charger_names.get(charge_point_id) or f"Charger {charge_point_id}"

        energy = _build_statistics(charger_sessions, ENERGY)
        if energy:
            async_add_external_statistics(
                hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} energy",
                    source=DOMAIN,
                    statistic_id=statistic_id_for(charge_point_id, ENERGY),
                    unit_class="energy",
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                energy,
            )

        # Costs are in the account's currency; take the latest one given.
        currency = next(
            (s.currency for s in reversed(charger_sessions) if s.currency), None
        )
        cost = _build_statistics(charger_sessions, COST)
        if cost:
            async_add_external_statistics(
                hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} cost",
                    source=DOMAIN,
                    statistic_id=statistic_id_for(charge_point_id, COST),
                    unit_class=None,
                    unit_of_measurement=currency,
                ),
                cost,
            )

        _LOGGER.debug(
            "Imported %d sessions for charger %s into %d hourly statistics",
            len(charger_sessions),
            charge_point_id,
            len(energy),
        )
