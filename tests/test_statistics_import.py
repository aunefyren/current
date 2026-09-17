"""Tests for the long-term statistics import."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.current.api import CannotConnectError, CurrentApiClient
from custom_components.current.const import DOMAIN
from custom_components.current.statistics_import import (
    COST,
    ENERGY,
    HISTORY_PAGE_SIZE,
    async_fetch_all_sessions,
    async_import_statistics,
    parse_session,
    parse_time,
    spread_over_hours,
    statistic_id_for,
)

from .conftest import CurrentApiMock, FakeSession, setup_entry
from .const import (
    MOCK_ACCESS_TOKEN,
    MOCK_CHARGE_POINT_ID,
    MOCK_CUSTOMER_ID,
    MOCK_REFRESH_TOKEN,
    MOCK_USER_ID,
    SECOND_CHARGE_POINT_ID,
)

# The recorder's own rule for external statistic ids.
VALID_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")

ENERGY_ID = statistic_id_for(MOCK_CHARGE_POINT_ID, ENERGY)
COST_ID = statistic_id_for(MOCK_CHARGE_POINT_ID, COST)


def utc(*args: int) -> datetime:
    """Build a UTC datetime."""
    return datetime(*args, tzinfo=UTC)


# -- ids and parsing ------------------------------------------------------


@pytest.mark.parametrize("charge_point_id", [3001, "3001", "AB-12", "--", ""])
@pytest.mark.parametrize("kind", [ENERGY, COST])
def test_statistic_ids_are_accepted_by_recorder(
    charge_point_id: int | str, kind: str
) -> None:
    """Every id we can generate must satisfy the recorder's pattern."""
    statistic_id = statistic_id_for(charge_point_id, kind)
    assert VALID_STATISTIC_ID.match(statistic_id), statistic_id
    assert statistic_id.startswith(f"{DOMAIN}:")


def test_statistic_ids_are_distinct() -> None:
    """Energy and cost, on different chargers, must not collide."""
    ids = {
        statistic_id_for(charger, kind)
        for charger in (MOCK_CHARGE_POINT_ID, SECOND_CHARGE_POINT_ID)
        for kind in (ENERGY, COST)
    }
    assert len(ids) == 4


def test_parse_session(api: CurrentApiMock) -> None:
    """A captured history item is read into a completed session."""
    session = parse_session(api.history["List"][0])

    assert session is not None
    assert session.charge_point_id == MOCK_CHARGE_POINT_ID
    assert session.start == utc(2026, 9, 14, 9, 9, 15, 103000)
    assert session.end == utc(2026, 9, 15, 5, 29, 39, 780000)
    assert session.energy == 58.702
    assert session.cost == 82.18
    assert session.currency == "NOK"


async def test_parse_time_without_zone_is_local(hass: HomeAssistant) -> None:
    """A timestamp without a zone is local time, one with a zone is kept."""
    await hass.config.async_set_time_zone("Europe/Oslo")
    assert parse_time("2026-09-17T11:53:41.903") == utc(2026, 9, 17, 9, 53, 41, 903000)
    assert parse_time("2026-09-17T11:53:41.903Z") == utc(
        2026, 9, 17, 11, 53, 41, 903000
    )
    assert parse_time(None) is None
    assert parse_time("not a time") is None


def test_parse_session_skips_unfinished(api: CurrentApiMock) -> None:
    """A session without an end is still going, and is left out."""
    item = api.history["List"][0]
    item["Session"]["SessionEnd"] = None
    assert parse_session(item) is None


def test_parse_session_falls_back_to_session_energy(api: CurrentApiMock) -> None:
    """The session's own energy is used when the item has none."""
    item = api.history["List"][0]
    item["TotalkWH"] = None
    session = parse_session(item)
    assert session is not None
    assert session.energy == item["Session"]["TotalkWh"]


# -- spreading over hours -------------------------------------------------


def test_spread_within_one_hour() -> None:
    """A session inside one hour puts everything in that hour."""
    assert spread_over_hours(utc(2026, 9, 1, 10, 5), utc(2026, 9, 1, 10, 50), 7.0) == {
        utc(2026, 9, 1, 10): 7.0
    }


def test_spread_by_overlap() -> None:
    """Each hour gets its share of the time the session covered."""
    # 30 minutes, 60 minutes, 30 minutes.
    shares = spread_over_hours(utc(2026, 9, 1, 10, 30), utc(2026, 9, 1, 12, 30), 20.0)
    assert shares == pytest.approx(
        {
            utc(2026, 9, 1, 10): 5.0,
            utc(2026, 9, 1, 11): 10.0,
            utc(2026, 9, 1, 12): 5.0,
        }
    )


def test_spread_ending_on_the_hour() -> None:
    """A session ending exactly on the hour does not touch the next hour."""
    shares = spread_over_hours(utc(2026, 9, 1, 10), utc(2026, 9, 1, 12), 4.0)
    assert shares == pytest.approx({utc(2026, 9, 1, 10): 2.0, utc(2026, 9, 1, 11): 2.0})


def test_spread_without_length() -> None:
    """A session that ends when it starts lands in its start hour."""
    moment = utc(2026, 9, 1, 10, 15)
    assert spread_over_hours(moment, moment, 3.0) == {utc(2026, 9, 1, 10): 3.0}


def test_spread_keeps_the_total(api: CurrentApiMock) -> None:
    """Spreading never adds or loses energy."""
    for item in api.history["List"]:
        session = parse_session(item)
        assert session is not None
        shares = spread_over_hours(session.start, session.end, session.energy)
        assert sum(shares.values()) == pytest.approx(session.energy)


# -- reading the history --------------------------------------------------


@pytest.fixture
def client(session: FakeSession) -> CurrentApiClient:
    """Return a client over the fake endpoint."""
    return CurrentApiClient(
        session=session,
        access_token=MOCK_ACCESS_TOKEN,
        refresh_token=MOCK_REFRESH_TOKEN,
        customer_id=MOCK_CUSTOMER_ID,
        user_id=MOCK_USER_ID,
    )


async def test_fetch_all_pages(client: CurrentApiClient, api: CurrentApiMock) -> None:
    """The history is paged through until every session has been read."""
    api.set_history_length(2 * HISTORY_PAGE_SIZE + 10)

    sessions = await async_fetch_all_sessions(client)

    assert len(sessions) == 2 * HISTORY_PAGE_SIZE + 10
    assert [r["params"]["startIndex"] for r in api.history_requests()] == [
        0,
        HISTORY_PAGE_SIZE,
        2 * HISTORY_PAGE_SIZE,
    ]
    # Oldest first, which is what running totals are built in.
    assert [s.start for s in sessions] == sorted(s.start for s in sessions)


async def test_fetch_with_capped_pages(
    client: CurrentApiClient, api: CurrentApiMock
) -> None:
    """A server returning fewer sessions than asked for is paged by what it gave."""
    api.set_history_length(45)
    api.history_page_limit = 20

    sessions = await async_fetch_all_sessions(client)

    assert len(sessions) == 45
    assert [r["params"]["startIndex"] for r in api.history_requests()] == [0, 20, 40]


async def test_fetch_like_the_real_account(
    client: CurrentApiClient, api: CurrentApiMock
) -> None:
    """123 sessions, 50 to a page as CURRENT caps it, take three requests."""
    api.set_history_length(123)
    api.history_page_limit = 50

    sessions = await async_fetch_all_sessions(client)

    assert len(sessions) == 123
    assert [r["params"]["startIndex"] for r in api.history_requests()] == [0, 50, 100]


async def test_fetch_stops_when_paging_is_ignored(
    client: CurrentApiClient, api: CurrentApiMock
) -> None:
    """If every page is the same, reading stops instead of looping."""
    api.set_history_length(HISTORY_PAGE_SIZE)
    api.history["TotalOrders"] = 10 * HISTORY_PAGE_SIZE
    api.history_response = lambda params: api.history  # ignores startIndex

    sessions = await async_fetch_all_sessions(client)

    assert len(sessions) == HISTORY_PAGE_SIZE
    assert len(api.history_requests()) == 2


# -- writing statistics ---------------------------------------------------


async def read_rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    """Return every stored hourly bucket, oldest first."""
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        utc(2020, 1, 1),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


async def read_metadata(hass: HomeAssistant, statistic_id: str) -> dict:
    """Return the stored metadata of one statistic."""
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={statistic_id})
    )
    return found[statistic_id][1]


def history_sessions(api: CurrentApiMock):
    """Parse the fake endpoint's history, oldest first."""
    parsed = [parse_session(item) for item in api.history["List"]]
    return sorted((s for s in parsed if s is not None), key=lambda s: s.start)


async def test_import_writes_energy_and_cost(
    hass: HomeAssistant, api: CurrentApiMock
) -> None:
    """Energy and cost end on the history's totals, in the right units."""
    sessions = history_sessions(api)

    async_import_statistics(hass, sessions, {MOCK_CHARGE_POINT_ID: "Test Charger"})

    energy = await read_rows(hass, ENERGY_ID)
    cost = await read_rows(hass, COST_ID)
    assert energy[-1]["sum"] == pytest.approx(sum(s.energy for s in sessions))
    assert cost[-1]["sum"] == pytest.approx(sum(s.cost for s in sessions))

    # Buckets start with the first session's hour and never run backwards.
    assert (
        energy[0]["start"]
        == sessions[0].start.replace(minute=0, second=0, microsecond=0).timestamp()
    )
    sums = [row["sum"] for row in energy]
    assert sums == sorted(sums)

    energy_meta = await read_metadata(hass, ENERGY_ID)
    assert energy_meta["unit_of_measurement"] == "kWh"
    assert energy_meta["name"] == "Test Charger energy"
    assert energy_meta["source"] == DOMAIN
    cost_meta = await read_metadata(hass, COST_ID)
    assert cost_meta["unit_of_measurement"] == "NOK"
    assert cost_meta["name"] == "Test Charger cost"


async def test_import_again_does_not_double_count(
    hass: HomeAssistant, api: CurrentApiMock
) -> None:
    """Importing the same history again leaves the totals as they were."""
    sessions = history_sessions(api)

    async_import_statistics(hass, sessions, {})
    first = await read_rows(hass, ENERGY_ID)
    async_import_statistics(hass, sessions, {})
    second = await read_rows(hass, ENERGY_ID)

    assert [r["sum"] for r in second] == pytest.approx([r["sum"] for r in first])


async def test_import_picks_up_revisions(
    hass: HomeAssistant, api: CurrentApiMock
) -> None:
    """A revised session replaces its old value rather than adding to it."""
    async_import_statistics(hass, history_sessions(api), {})
    before = (await read_rows(hass, COST_ID))[-1]["sum"]

    api.history["List"][2]["TotalPrice"] += 10.0
    async_import_statistics(hass, history_sessions(api), {})

    assert (await read_rows(hass, COST_ID))[-1]["sum"] == pytest.approx(before + 10.0)


async def test_import_per_charger(hass: HomeAssistant, api: CurrentApiMock) -> None:
    """Each charger gets statistics of its own sessions only."""
    api.add_second_charger()
    sessions = history_sessions(api)

    async_import_statistics(hass, sessions, {})

    for charger in (MOCK_CHARGE_POINT_ID, SECOND_CHARGE_POINT_ID):
        rows = await read_rows(hass, statistic_id_for(charger, ENERGY))
        expected = sum(s.energy for s in sessions if s.charge_point_id == charger)
        assert rows[-1]["sum"] == pytest.approx(expected)
    # A charger with no name given still gets a readable one.
    meta = await read_metadata(hass, statistic_id_for(SECOND_CHARGE_POINT_ID, ENERGY))
    assert meta["name"] == f"Charger {SECOND_CHARGE_POINT_ID} energy"


# -- the coordinator ------------------------------------------------------


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Poll again and wait for any import that starts."""
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_imports_statistics(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setting up reads the whole history into statistics."""
    await setup_entry(hass, mock_config_entry)

    rows = await read_rows(hass, ENERGY_ID)
    assert rows[-1]["sum"] == pytest.approx(
        sum(item["TotalkWH"] for item in api.history["List"])
    )
    meta = await read_metadata(hass, ENERGY_ID)
    assert meta["name"] == "Test Charger energy"


async def test_history_only_reread_when_it_changes(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Polls fetch the latest sessions, but only a change reads them all."""
    await setup_entry(hass, mock_config_entry)
    after_setup = len(api.history_requests())
    # The poll's own request, and at least one page for the import.
    assert after_setup >= 2

    await refresh(hass, mock_config_entry)
    assert len(api.history_requests()) == after_setup + 1

    # A new session finishes.
    item = api.history["List"][0]
    new = {**item, "Session": {**item["Session"], "PK_ServiceSessionID": 99999}}
    new["Session"]["SessionStart"] = "2026-09-16T10:00:00Z"
    new["Session"]["SessionEnd"] = "2026-09-16T12:00:00Z"
    api.history = {**api.history, "List": [new, *api.history["List"]]}

    await refresh(hass, mock_config_entry)
    assert len(api.history_requests()) > after_setup + 2

    rows = await read_rows(hass, ENERGY_ID)
    assert rows[-1]["start"] == utc(2026, 9, 16, 11).timestamp()


async def test_failed_import_is_retried(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If the history cannot be read, the next poll tries again."""
    with patch(
        "custom_components.current.coordinator.async_fetch_all_sessions",
        side_effect=CannotConnectError("fake failure"),
    ):
        await setup_entry(hass, mock_config_entry)
    assert await read_rows(hass, ENERGY_ID) == []

    await refresh(hass, mock_config_entry)
    assert await read_rows(hass, ENERGY_ID)
