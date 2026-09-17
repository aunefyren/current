"""Tests that the translations are complete and actually used."""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.current.const import DOMAIN

from .conftest import CurrentApiMock, FakeSession, setup_entry

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / DOMAIN
TRANSLATIONS = COMPONENT / "translations"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load(path: pathlib.Path) -> dict:
    """Load one translation file."""
    return json.loads(path.read_text(encoding="utf-8"))


def flatten(tree: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested translations into dotted keys."""
    flat: dict[str, str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def test_english_matches_strings() -> None:
    """translations/en.json is what custom integrations load; keep it in sync."""
    assert load(TRANSLATIONS / "en.json") == load(COMPONENT / "strings.json")


@pytest.mark.parametrize(
    "path", sorted(TRANSLATIONS.glob("*.json")), ids=lambda p: p.name
)
def test_translation_complete(path: pathlib.Path) -> None:
    """Every language has every string, with the same placeholders."""
    english = flatten(load(TRANSLATIONS / "en.json"))
    other = flatten(load(path))

    assert other.keys() == english.keys()
    for key, text in english.items():
        assert set(PLACEHOLDER.findall(other[key])) == set(PLACEHOLDER.findall(text)), (
            key
        )


def test_config_flow_messages_exist() -> None:
    """Each error and abort reason the flow can produce has a message."""
    source = (COMPONENT / "config_flow.py").read_text()
    config = load(TRANSLATIONS / "en.json")["config"]

    errors = set(re.findall(r'errors\["base"\] = "(\w+)"', source))
    assert errors
    assert errors <= config["error"].keys()

    # already_configured comes from _abort_if_unique_id_configured.
    aborts = set(re.findall(r'reason="(\w+)"', source)) | {"already_configured"}
    assert aborts <= config["abort"].keys()


async def test_entity_names_come_from_translations(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Every entity is named from en.json, not left nameless or hard-coded."""
    await setup_entry(hass, mock_config_entry)
    names = load(TRANSLATIONS / "en.json")["entity"]

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert len(entries) == 12

    for entry in entries:
        assert entry.translation_key in names[entry.domain], entry.entity_id
        expected = names[entry.domain][entry.translation_key]["name"]
        state = hass.states.get(entry.entity_id)
        assert state.attributes["friendly_name"] == f"Test Charger {expected}"


async def test_entity_ids_unchanged_by_translation_keys(
    hass: HomeAssistant,
    patched_session: FakeSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Entity ids match what the hard-coded English names produced.

    New installs must get the same ids existing ones have, or shared
    automations and dashboards would not carry over.
    """
    await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
    }
    assert ids == {
        "sensor.test_charger_charger_status",
        "sensor.test_charger_session_energy",
        "sensor.test_charger_charging_duration",
        "sensor.test_charger_live_power",
        "sensor.test_charger_live_current",
        "sensor.test_charger_state_of_charge",
        "sensor.test_charger_last_session_cost",
        "sensor.test_charger_last_session_energy",
        "switch.test_charger_ev_charging",
        "switch.test_charger_require_authentication",
        "switch.test_charger_cable_lock",
        "button.test_charger_restart_charger",
    }


async def test_command_error_message_is_translated(
    hass: HomeAssistant,
    patched_session: FakeSession,
    api: CurrentApiMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failed command shows the message from en.json, with the cause filled in."""
    await setup_entry(hass, mock_config_entry)
    api.command_status = 500

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "switch.test_charger_ev_charging"},
            blocking=True,
        )

    assert str(err.value) == (
        "Could not send the command to CURRENT: "
        "Request to Commands/RemoteStart failed: 500"
    )
