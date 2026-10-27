"""MATRIX EEG: heat pump boost by the MATRIX EEG platform over MQTT contract v1."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .bridge import Bridge


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge = Bridge(hass, entry)
    await bridge.async_start()
    entry.runtime_data = bridge
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True
