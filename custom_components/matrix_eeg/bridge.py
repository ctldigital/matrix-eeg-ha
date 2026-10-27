"""MQTT bridge: maps the chosen entities onto contract v1 with an own paho client (not HA's MQTT integration)."""

import asyncio
import logging
import math
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DHW_MAX,
    CONF_DHW_TARGET,
    CONF_DHW_TEMP,
    CONF_HEATER_ROD,
    CONF_LIMIT_C,
    CONF_PASSWORD,
    CONF_POWER,
    CONF_URL,
    CONF_USERNAME,
    MAX_TARGET_C,
)
from .contract import UNAVAILABLE, Reject, build_ack, build_state, parse_cmd
from .lease import LeaseCore

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_S = 30
PROBE_TIMEOUT_S = 10
WRITE_TIMEOUT_S = 10
TICK = timedelta(seconds=5)
STATE_EVERY = timedelta(seconds=30)
DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883, "wss": 443}
ERR_UNAVAILABLE = "Warmwasser-Sollwert nicht verfügbar"
ERR_WRITE = "Sollwert konnte nicht gesetzt werden"
ERR_MAX_UNAVAILABLE = "max. Warmwassertemperatur nicht verfügbar"


def parse_url(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path); ValueError if the URL is not usable."""
    u = urlsplit(url.strip())
    if u.scheme not in DEFAULT_PORTS or not u.hostname:
        raise ValueError(url)
    return u.scheme, u.hostname, u.port or DEFAULT_PORTS[u.scheme], u.path if u.path not in ("", "/") else "/mqtt"


def make_client(url: str, username: str, password: str, client_id: str) -> tuple[mqtt.Client, str, int]:
    """Blocking (loads the system CAs): run it in the executor."""
    scheme, host, port, path = parse_url(url)
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        transport="websockets" if scheme == "wss" else "tcp",
    )
    client.username_pw_set(username, password)
    if scheme != "mqtt":
        client.tls_set()
    if scheme == "wss":
        client.ws_set_options(path=path)
    return client, host, port


def _probe(url: str, username: str, password: str) -> str | None:
    # Own client ID: the broker would otherwise take over the session of a running bridge.
    client, host, port = make_client(url, username, password, f"matrix_eeg-{username}-test")
    connected = threading.Event()
    result = []

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        result.append(reason_code)
        connected.set()

    client.on_connect = on_connect
    deadline = time.monotonic() + PROBE_TIMEOUT_S  # one budget for TCP/TLS connect and CONNACK
    client.connect_timeout = PROBE_TIMEOUT_S
    try:
        client.connect(host, port, KEEPALIVE_S)
    except OSError:
        return "cannot_connect"
    client.loop_start()
    try:
        if not connected.wait(max(0.0, deadline - time.monotonic())):
            return "cannot_connect"
    finally:
        client.disconnect()
        client.loop_stop()
    if result[0].value in (134, 135):  # bad user name or password, not authorized
        return "invalid_auth"
    return "cannot_connect" if result[0].is_failure else None


async def async_probe(hass: HomeAssistant, url: str, username: str, password: str) -> str | None:
    """Connect test for the config flow: None, "cannot_connect" or "invalid_auth"."""
    return await hass.async_add_executor_job(_probe, url, username, password)


class Bridge:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self._hass, self._entry, self._cfg = hass, entry, entry.data
        self._prefix = f"eeg/{self._cfg[CONF_USERNAME]}"
        self._store = Store(hass, 1, f"matrix_eeg.{entry.entry_id}")
        self._lock = asyncio.Lock()  # serialises cmd and tick, both read the setpoint, then write
        self._core: LeaseCore | None = None
        self._client: mqtt.Client | None = None
        self._unsubs = []
        self._stopped = False  # callbacks queued before loop_stop must not act after unload
        self._debouncer = Debouncer(hass, _LOGGER, cooldown=1.0, immediate=False, function=self._publish_state)

    async def async_start(self) -> None:
        self._core = LeaseCore(await self._store.async_load())
        # before the first state publish: a lease that ran out while HA was down is reverted now
        await self._async_tick()
        client, host, port = await self._hass.async_add_executor_job(
            make_client,
            self._cfg[CONF_URL],
            self._cfg[CONF_USERNAME],
            self._cfg[CONF_PASSWORD],
            f"matrix_eeg-{self._cfg[CONF_USERNAME]}",
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.will_set(self._topic("status"), "offline", qos=0, retain=True)
        self._client = client  # set before the listeners, they publish
        entities = [e for k, e in self._cfg.items() if k in _ENTITY_KEYS and e]
        self._unsubs += [
            async_track_state_change_event(self._hass, entities, self._on_entity_change),
            async_track_time_interval(self._hass, self._async_tick, TICK),
            async_track_time_interval(self._hass, self._on_state_interval, STATE_EVERY),
        ]
        client.connect_async(host, port, KEEPALIVE_S)
        client.loop_start()  # paho reconnects on its own

    async def async_stop(self) -> None:
        async with self._lock:  # lets a running cmd or tick finish, later ones see the flag
            self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._debouncer.async_shutdown()
        client = self._client
        client.publish(self._topic("status"), "offline", qos=0, retain=True)  # a clean disconnect sends no will
        await self._hass.async_add_executor_job(_shutdown, client)

    def _topic(self, sub: str) -> str:
        return f"{self._prefix}/{sub}"

    def _publish(self, sub: str, payload: bytes) -> None:
        self._client.publish(self._topic(sub), payload, qos=0, retain=False)

    # paho thread: only paho calls here, everything else is handed to the event loop

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            _LOGGER.warning("MATRIX EEG broker refused the connection: %s", reason_code)
            return
        client.subscribe(self._topic("cmd"), qos=0)
        client.publish(self._topic("status"), "online", qos=0, retain=True)
        self._hass.loop.call_soon_threadsafe(self._publish_state)

    def _on_message(self, _client, _userdata, msg):
        self._hass.loop.call_soon_threadsafe(self._on_cmd, msg.payload)

    # event loop

    @callback
    def _on_cmd(self, payload: bytes) -> None:
        if self._stopped:
            return
        self._entry.async_create_task(self._hass, self._async_cmd(payload))

    async def _async_cmd(self, payload: bytes) -> None:
        res = parse_cmd(payload)
        if res is None:
            return  # no usable id: nothing to answer
        if isinstance(res, Reject):
            self._publish("ack", build_ack(res.id, res.error))
            return
        boost = res.target_c > res.fallback_c
        async with self._lock:
            if self._stopped:
                return
            current = self._target()
            before = self._core.to_store()
            if boost and current is None:
                error = ERR_UNAVAILABLE
            elif boost and self._value(CONF_DHW_MAX) is UNAVAILABLE:
                error = ERR_MAX_UNAVAILABLE  # its clamp is unknown: no new boost
            else:
                write, error = self._core.on_cmd(res, current, dt_util.utcnow().timestamp(), self._max_c())
                if error is None and write is not None:
                    error = await self._async_write(write)
            if error is not None:
                self._core = LeaseCore(before)  # nothing was written: the next cmd tries again
            await self._store.async_save(self._core.to_store())
        self._publish("ack", build_ack(res.id, error))
        self._debouncer.async_schedule_call()  # boost_active may change without a state change

    async def _async_tick(self, _now=None) -> None:
        async with self._lock:
            if self._stopped:
                return
            before = self._core.to_store()
            write = self._core.on_tick(self._target(), dt_util.utcnow().timestamp())
            if write is not None and await self._async_write(write) is not None:
                self._core = LeaseCore(before)  # its until is past: the next tick retries the fallback
            if self._core.to_store() != before:
                await self._store.async_save(self._core.to_store())

    async def _async_write(self, value: float) -> str | None:
        """Every write is clamped to the local limit, the hard cap and the max entity."""
        value = min(value, self._max_c())
        entity_id = self._cfg[CONF_DHW_TARGET]
        domain = split_entity_id(entity_id)[0]
        service, field = ("set_temperature", "temperature") if domain == "water_heater" else ("set_value", "value")
        try:
            async with asyncio.timeout(WRITE_TIMEOUT_S):  # a hanging vendor integration must not hold the lock
                await self._hass.services.async_call(
                    domain, service, {"entity_id": entity_id, field: value}, blocking=True
                )
        except Exception as err:  # noqa: BLE001 - any failure means not written
            _LOGGER.warning("MATRIX EEG could not set %s to %s: %r", entity_id, value, err)
            return ERR_WRITE
        return None

    @callback
    def _on_entity_change(self, _event) -> None:
        self._debouncer.async_schedule_call()

    @callback
    def _on_state_interval(self, _now) -> None:
        self._publish_state()

    @callback
    def _publish_state(self) -> None:
        if self._stopped:
            return
        self._publish(
            "state",
            build_state(
                dhw_temp_c=self._value(CONF_DHW_TEMP, "current_temperature"),
                dhw_target_c=self._value(CONF_DHW_TARGET, "temperature"),
                boost_active=self._core.boost_active(self._target(), dt_util.utcnow().timestamp()),
                power_w=self._power(),
                heater_rod_active=self._heater_rod(),
                dhw_max_c=self._value(CONF_DHW_MAX),
                ts=dt_util.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

    # entity values: None = not configured, UNAVAILABLE = configured but no usable value

    def _state(self, key):
        entity_id = self._cfg.get(key)
        if not entity_id:
            return None
        st = self._hass.states.get(entity_id)
        return UNAVAILABLE if st is None or st.state == STATE_UNAVAILABLE else st

    def _value(self, key, water_heater_attr=None):
        """A water_heater carries the value in an attribute, the other domains in the state."""
        st = self._state(key)
        if st is None or st is UNAVAILABLE:
            return st
        raw = st.attributes.get(water_heater_attr) if st.domain == "water_heater" else st.state
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return UNAVAILABLE
        return v if math.isfinite(v) else UNAVAILABLE

    def _target(self) -> float | None:
        v = self._value(CONF_DHW_TARGET, "temperature")
        return None if v is UNAVAILABLE else v

    def _max_c(self) -> float:
        limit = min(self._cfg[CONF_LIMIT_C], MAX_TARGET_C)
        dhw_max = self._value(CONF_DHW_MAX)
        # unavailable max entity: boosts are refused in _async_cmd, fallback writes keep the limit
        return limit if dhw_max is None or dhw_max is UNAVAILABLE else min(limit, dhw_max)

    def _power(self):
        v = self._value(CONF_POWER)
        if v is None or v is UNAVAILABLE:
            return v
        return v * 1000 if self._hass.states.get(self._cfg[CONF_POWER]).attributes.get("unit_of_measurement") == "kW" else v

    def _heater_rod(self):
        st = self._state(CONF_HEATER_ROD)
        if st is None or st is UNAVAILABLE:
            return st
        if st.state in ("on", "off"):
            return st.state == "on"
        v = self._value(CONF_HEATER_ROD)
        return v if v is UNAVAILABLE else v > 0


_ENTITY_KEYS = (CONF_DHW_TARGET, CONF_DHW_TEMP, CONF_POWER, CONF_HEATER_ROD, CONF_DHW_MAX)


def _shutdown(client: mqtt.Client) -> None:
    client.disconnect()
    client.loop_stop()
