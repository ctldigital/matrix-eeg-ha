from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.matrix_eeg.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONN = {"url": "mqtts://mqtt.matrix.ctl.digital:8883", "username": "u1", "password": "secret"}
ENTITIES = {"dhw_target": "water_heater.wp", "dhw_temp": "sensor.ww", "limit_c": 55}


@pytest.fixture
def probe():
    with patch("custom_components.matrix_eeg.config_flow.async_probe", return_value=None) as p:
        yield p


@pytest.fixture
def setup_entry():
    with (
        patch("custom_components.matrix_eeg.async_setup_entry", return_value=True) as setup,
        patch("custom_components.matrix_eeg.async_unload_entry", return_value=True),
    ):
        yield setup


async def test_happy_path_creates_entry(hass, probe, setup_entry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONN)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "entities"
    probe.assert_awaited_once_with(hass, CONN["url"], "u1", "secret")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTITIES)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**CONN, **ENTITIES}
    assert result["result"].unique_id == "u1"


@pytest.mark.parametrize("error", ["cannot_connect", "invalid_auth"])
async def test_probe_errors(hass, probe, error):
    probe.return_value = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONN)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}


@pytest.mark.parametrize("url", ["http://x", "mqtts://", "mqtt://host:notaport"])
async def test_bad_url(hass, probe, url):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONN, "url": url})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"url": "invalid_url"}
    probe.assert_not_awaited()


async def test_duplicate_username_aborts(hass, probe):
    MockConfigEntry(domain=DOMAIN, unique_id="u1", data={**CONN, **ENTITIES}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONN)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow_changes_url_and_reloads(hass, probe, setup_entry):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u1", data={**CONN, **ENTITIES, "power": "sensor.p"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert setup_entry.call_count == 1

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**CONN, "url": "wss://broker.example/mqtt"}
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.options.async_configure(result["flow_id"], ENTITIES)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # power was cleared in the entities step, so it is gone
    assert entry.data == {**CONN, "url": "wss://broker.example/mqtt", **ENTITIES}
    assert setup_entry.call_count == 2
