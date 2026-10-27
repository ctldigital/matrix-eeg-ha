import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.matrix_eeg import bridge
from custom_components.matrix_eeg.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

FIX = Path(__file__).parent.parent / "contract" / "v1"
CONN = {"url": "mqtts://mqtt.matrix.ctl.digital:8883", "username": "u1", "password": "secret"}
BASE = {**CONN, "dhw_target": "water_heater.wp", "dhw_temp": "sensor.ww", "limit_c": 60}


EVENTS = []  # writes and publishes in the order they happened


def fixture(name):
    return json.loads((FIX / name).read_text())


class FakeClient:
    """Records what the bridge does with paho; the test drives the callbacks."""

    last = None

    def __init__(self, api, client_id=None, transport="tcp"):
        self.client_id, self.transport = client_id, transport
        self.published, self.subscribed = [], []
        self.ws_path = self.tls = self.target = self.will = None
        self.connect_delay = 0
        FakeClient.last = self

    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def tls_set(self):
        self.tls = True

    def ws_set_options(self, path):
        self.ws_path = path

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect_async(self, host, port, keepalive):
        self.target = (host, port, keepalive)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        EVENTS.append(("publish", topic))

    def connect(self, host, port, keepalive):
        time.sleep(self.connect_delay)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    # test helpers
    def accept(self):
        self.on_connect(self, None, None, ReasonCode(PacketTypes.CONNACK, "Success"), None)

    def cmd(self, payload):
        self.on_message(self, None, SimpleNamespace(topic="eeg/u1/cmd", payload=payload))

    def last_json(self, sub):
        return next(json.loads(p) for t, p, _ in reversed(self.published) if t == f"eeg/u1/{sub}")


@pytest.fixture
def fake():
    FakeClient.last = None
    EVENTS.clear()
    with patch("custom_components.matrix_eeg.bridge.mqtt.Client", FakeClient):
        yield lambda: FakeClient.last


async def setup(hass, data=BASE, entry_id="e1"):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u1", data=data, entry_id=entry_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = FakeClient.last
    c.accept()
    await hass.async_block_till_done()
    return entry, c


def service(hass, domain, name, fail=0, hang=False, apply=True):
    """Records calls; the first `fail` calls raise, or every call hangs. apply: the entity shows the
    written value at once (else it never does, like a vendor entity that has not polled yet)."""
    calls = []

    async def handler(call):
        calls.append(call)
        EVENTS.append(("write", dict(call.data)))
        if hang:
            await asyncio.Event().wait()
        if len(calls) <= fail:
            raise HomeAssistantError("vendor integration not ready")
        if apply:
            show(hass, call.data["entity_id"], call.data.get("temperature", call.data.get("value")))

    hass.services.async_register(domain, name, handler)
    return calls


def heater(hass, target, current=48.5):
    hass.states.async_set("water_heater.wp", "eco", {"temperature": target, "current_temperature": current})


def show(hass, entity_id, value):
    if entity_id.startswith("water_heater."):
        st = hass.states.get(entity_id)
        hass.states.async_set(entity_id, st.state, {**st.attributes, "temperature": value})
    else:
        hass.states.async_set(entity_id, str(value))


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_session_setup(hass, fake):
    heater(hass, 50)
    hass.states.async_set("sensor.ww", "48.5")
    entry, c = await setup(hass, {**BASE, "url": "wss://broker.example/mqtt"})
    assert c.client_id == "matrix_eeg-u1" and c.transport == "websockets" and c.ws_path == "/mqtt" and c.tls
    assert c.target == ("broker.example", 443, 30)
    assert c.will == ("eeg/u1/status", "offline", True)
    assert c.subscribed == [("eeg/u1/cmd", 0)]
    assert ("eeg/u1/status", "online", True) in c.published
    await hass.config_entries.async_unload(entry.entry_id)
    assert c.published[-1] == ("eeg/u1/status", "offline", True)


async def test_boost_water_heater(hass, fake):
    heater(hass, 50)
    hass.states.async_set("sensor.ww", "48.5")
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data for call in calls] == [{"entity_id": "water_heater.wp", "temperature": 53}]
    assert c.last_json("ack") == fixture("ack_ok.json")


async def test_boost_number(hass, fake):
    hass.states.async_set("number.ww_soll", "50")
    hass.states.async_set("sensor.ww", "48.5")
    calls = service(hass, "number", "set_value")
    _, c = await setup(hass, {**BASE, "dhw_target": "number.ww_soll"})
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data for call in calls] == [{"entity_id": "number.ww_soll", "value": 53}]
    assert c.last_json("ack") == {"id": 7, "ok": True}


async def test_boost_clamped_to_max_entity(hass, fake, freezer):
    heater(hass, 50)
    hass.states.async_set("sensor.ww", "48.5")
    hass.states.async_set("sensor.ww_max", "52")
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass, {**BASE, "dhw_max": "sensor.ww_max"})
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data["temperature"] for call in calls] == [52]
    heater(hass, 52)
    await advance(hass, freezer, 301)  # the lease owns the clamped value
    assert [call.data["temperature"] for call in calls] == [52, 50]


async def test_invalid_cmds(hass, fake):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    for case in fixture("cmd_invalid.json"):
        c.published.clear()
        c.cmd(json.dumps(case["payload"]).encode())
        await hass.async_block_till_done()
        acks = [json.loads(p) for t, p, _ in c.published if t == "eeg/u1/ack"]
        want = [] if case["error"] is None else [{"id": case["payload"]["id"], "ok": False, "error": case["error"]}]
        assert acks == want, case["name"]
    assert calls == []


async def test_unavailable_target_acks_error_and_retries(hass, fake):
    hass.states.async_set("water_heater.wp", "unavailable")
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert calls == []
    assert c.last_json("ack") == {"id": 7, "ok": False, "error": "Warmwasser-Sollwert nicht verfügbar"}

    heater(hass, 50)  # back: the renewal writes, it is not taken for an already applied boost
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data["temperature"] for call in calls] == [53]
    assert c.last_json("ack") == {"id": 7, "ok": True}

    hass.states.async_set("water_heater.wp", "unavailable")  # renewal while gone: never ok
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 7, "ok": False, "error": "Warmwasser-Sollwert nicht verfügbar"}


@pytest.mark.parametrize("exc", [HomeAssistantError, ValueError])
async def test_failed_write_acks_error_and_next_cmd_retries(hass, fake, exc):
    heater(hass, 50)
    calls = []

    async def handler(call):
        calls.append(call.data["temperature"])
        if len(calls) == 1:
            raise exc("not ready")
        heater(hass, call.data["temperature"])

    hass.services.async_register("water_heater", "set_temperature", handler)
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 7, "ok": False, "error": "Sollwert konnte nicht gesetzt werden"}
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert calls == [53, 53]
    assert c.last_json("ack") == {"id": 7, "ok": True}


async def test_hanging_write_times_out(hass, fake, monkeypatch):
    monkeypatch.setattr(bridge, "WRITE_TIMEOUT_S", 0.05)
    heater(hass, 50)
    service(hass, "water_heater", "set_temperature", hang=True)
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 7, "ok": False, "error": "Sollwert konnte nicht gesetzt werden"}


async def test_failed_fallback_is_retried_on_next_tick(hass, fake, freezer, hass_storage):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    heater(hass, 53)
    hass.services.async_remove("water_heater", "set_temperature")
    calls = service(hass, "water_heater", "set_temperature", fail=1)
    await advance(hass, freezer, 301)
    assert [call.data["temperature"] for call in calls] == [50]
    assert hass_storage["matrix_eeg.e1"]["data"]["applied"] == 53  # kept for the retry
    await advance(hass, freezer, 5)
    assert [call.data["temperature"] for call in calls] == [50, 50]
    assert hass_storage["matrix_eeg.e1"]["data"] == {}


async def test_no_cmd_after_unload(hass, fake):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    entry, c = await setup(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    c.published.clear()
    c.cmd((FIX / "cmd_boost.json").read_bytes())  # queued by paho before loop_stop
    await hass.async_block_till_done()
    assert calls == [] and c.published == []


def test_probe_uses_one_time_budget(fake, monkeypatch):
    monkeypatch.setattr(bridge, "PROBE_TIMEOUT_S", 0.5)
    real_init = FakeClient.__init__

    def slow_init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.connect_delay = 0.4  # CONNACK never comes

    monkeypatch.setattr(FakeClient, "__init__", slow_init)
    start = time.monotonic()
    assert bridge._probe("mqtts://broker.example:8883", "u1", "pw") == "cannot_connect"
    assert time.monotonic() - start < 0.75


async def test_state_follows_fixture_rules(hass, fake, freezer):
    heater(hass, 50)
    hass.states.async_set("sensor.ww", "48.5")
    _, c = await setup(hass)
    state = c.last_json("state")
    want = fixture("state_heat_pump.json")
    assert state.pop("ts") and want.pop("ts")
    assert state == want

    # configured but unavailable: field omitted; kW is converted to W; rod on
    entry, c = await setup(
        hass,
        {**BASE, "power": "sensor.p", "heater_rod": "switch.rod", "dhw_max": "number.max"},
        entry_id="e2",
    )
    hass.states.async_set("sensor.p", "1.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("switch.rod", "on")
    hass.states.async_set("number.max", "unknown")
    hass.states.async_set("sensor.ww", "unavailable")
    await advance(hass, freezer, 1.1)
    state = c.last_json("state")
    state.pop("ts")
    assert state == {
        "v": 1,
        "category": "heat_pump",
        "dhw_target_c": 50,
        "boost_active": False,
        "power_w": 1500,
        "heater_rod_active": True,
    }


async def test_lease_expiry_writes_fallback(hass, fake, freezer, hass_storage):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    heater(hass, 53)
    assert hass_storage["matrix_eeg.e1"]["data"]["applied"] == 53
    await advance(hass, freezer, 1.1)
    assert c.last_json("state")["boost_active"] is True

    await advance(hass, freezer, 300)
    assert [call.data["temperature"] for call in calls] == [53, 50]
    assert hass_storage["matrix_eeg.e1"]["data"] == {}


async def test_manual_change_is_kept(hass, fake, freezer):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    heater(hass, 53)
    heater(hass, 45)  # manual change mid-boost
    await advance(hass, freezer, 1.1)
    assert c.last_json("state")["boost_active"] is False

    await advance(hass, freezer, 300)
    assert [call.data["temperature"] for call in calls] == [53]


async def test_restart_after_expiry_writes_fallback_before_first_state(hass, fake, hass_storage):
    hass_storage["matrix_eeg.e1"] = {
        "version": 1,
        "key": "matrix_eeg.e1",
        "data": {"applied": 53, "fallback_c": 50, "until": 0},
    }
    heater(hass, 53)
    service(hass, "water_heater", "set_temperature")
    await setup(hass)  # includes the broker accepting the connection, which publishes state
    assert hass_storage["matrix_eeg.e1"]["data"] == {}
    state_at = EVENTS.index(("publish", "eeg/u1/state"))
    assert EVENTS.index(("write", {"entity_id": "water_heater.wp", "temperature": 50})) < state_at


async def test_local_limit_clamps(hass, fake):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass, {**BASE, "limit_c": 52})
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data["temperature"] for call in calls] == [52]
    assert c.last_json("ack") == {"id": 7, "ok": True}

    c.cmd(b'{"v":1,"id":8,"target_c":55,"fallback_c":55,"lease_s":0}')  # end: the fallback is clamped too
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 8, "ok": True}
    c.cmd(b'{"v":1,"id":9,"target_c":53,"fallback_c":52,"lease_s":300}')  # clamp at fallback: nothing to boost
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 9, "ok": False, "error": "Boost nicht möglich: Obergrenze 52 °C"}
    assert [call.data["temperature"] for call in calls] == [52, 52]


async def test_unavailable_max_entity_refuses_boost_but_not_fallback(hass, fake, freezer, hass_storage):
    heater(hass, 50)
    hass.states.async_set("sensor.ww_max", "55")
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass, {**BASE, "dhw_max": "sensor.ww_max", "limit_c": 58})
    t0 = dt_util.utcnow().timestamp()
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    hass.states.async_set("sensor.ww_max", "unavailable")
    await advance(hass, freezer, 100)

    c.cmd((FIX / "cmd_boost.json").read_bytes())  # renewal: refused, the lease is not extended
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 7, "ok": False, "error": "max. Warmwassertemperatur nicht verfügbar"}
    assert hass_storage["matrix_eeg.e1"]["data"]["until"] == t0 + 300

    await advance(hass, freezer, 201)  # the expiry still writes the fallback
    assert [call.data["temperature"] for call in calls] == [53, 50]
    c.cmd(b'{"v":1,"id":9,"target_c":59,"fallback_c":50,"lease_s":300}')
    await hass.async_block_till_done()
    assert c.last_json("ack") == {"id": 9, "ok": False, "error": "max. Warmwassertemperatur nicht verfügbar"}
    assert len(calls) == 2 and hass_storage["matrix_eeg.e1"]["data"] == {}


async def test_vendor_applies_late_is_in_flight(hass, fake, freezer):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature", apply=False)
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data["temperature"] for call in calls] == [53]
    assert c.last_json("ack") == {"id": 7, "ok": True}  # at once, no wait for the vendor entity
    await advance(hass, freezer, 1.1)
    assert c.last_json("state")["boost_active"] is True  # still 50: value in flight
    await advance(hass, freezer, 59)
    c.accept()  # publishes the state at once
    await hass.async_block_till_done()
    assert c.last_json("state")["boost_active"] is True
    heater(hass, 53)  # lands after 60 s
    await advance(hass, freezer, 1.1)
    assert c.last_json("state")["boost_active"] is True


async def test_vendor_never_applies_reads_false_after_window(hass, fake, freezer):
    heater(hass, 50)
    service(hass, "water_heater", "set_temperature", apply=False)
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    await advance(hass, freezer, 121)
    c.accept()
    await hass.async_block_till_done()
    assert c.last_json("state")["boost_active"] is False


async def test_end_while_in_flight_writes_fallback(hass, fake):
    heater(hass, 50)
    calls = service(hass, "water_heater", "set_temperature", apply=False)
    _, c = await setup(hass)
    c.cmd((FIX / "cmd_boost.json").read_bytes())
    await hass.async_block_till_done()
    c.cmd((FIX / "cmd_end.json").read_bytes())
    await hass.async_block_till_done()
    assert [call.data["temperature"] for call in calls] == [53, 50]  # cancels a 53 that may still land
    assert c.last_json("ack") == {"id": 8, "ok": True}


async def test_restart_with_target_missing_keeps_boost(hass, fake, freezer, hass_storage):
    hass_storage["matrix_eeg.e1"] = {
        "version": 1,
        "key": "matrix_eeg.e1",
        "data": {"applied": 53, "fallback_c": 50, "until": dt_util.utcnow().timestamp() + 60},
    }
    calls = service(hass, "water_heater", "set_temperature")
    _, c = await setup(hass)  # the vendor integration has not created the entity yet
    assert c.last_json("state")["boost_active"] is True
    await advance(hass, freezer, 65)  # the lease ran out, ownership is still undecidable
    assert calls == [] and hass_storage["matrix_eeg.e1"]["data"]["applied"] == 53
    heater(hass, 53)
    await advance(hass, freezer, 5)
    assert [call.data["temperature"] for call in calls] == [50]
