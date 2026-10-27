"""Config and options flow: broker connection, then the entities. The options flow edits the same data."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .bridge import async_probe, parse_url
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
    DEFAULT_URL,
    DOMAIN,
    MAX_TARGET_C,
)

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)


def _entity(*domains: str) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=list(domains)))


ENTITIES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DHW_TARGET): _entity("water_heater", "number"),
        vol.Required(CONF_DHW_TEMP): _entity("sensor", "water_heater"),
        vol.Optional(CONF_POWER): _entity("sensor"),
        vol.Optional(CONF_HEATER_ROD): _entity("binary_sensor", "switch", "sensor"),
        vol.Optional(CONF_DHW_MAX): _entity("sensor", "number"),
        vol.Required(CONF_LIMIT_C, default=MAX_TARGET_C): NumberSelector(
            NumberSelectorConfig(min=30, max=MAX_TARGET_C, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.BOX)
        ),
    }
)


async def _async_check_connection(hass: HomeAssistant, user_input: dict) -> dict[str, str]:
    try:
        parse_url(user_input[CONF_URL])
    except ValueError:
        return {CONF_URL: "invalid_url"}
    error = await async_probe(hass, user_input[CONF_URL], user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
    return {"base": error} if error else {}


class MatrixEegConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._conn: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MatrixEegOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()
            errors = await _async_check_connection(self.hass, user_input)
            if not errors:
                self._conn = user_input
                return await self.async_step_entities()
        schema = self.add_suggested_values_to_schema(CONNECTION_SCHEMA, user_input)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=self._conn[CONF_USERNAME], data={**self._conn, **user_input})
        return self.async_show_form(step_id="entities", data_schema=ENTITIES_SCHEMA)


class MatrixEegOptionsFlow(OptionsFlow):
    """Prefilled with the entry data; the result replaces the data and reloads the entry."""

    def __init__(self) -> None:
        self._conn: dict = {}

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})
            errors = await _async_check_connection(self.hass, user_input)
            if not errors:
                self._conn = user_input
                return await self.async_step_entities()
        schema = self.add_suggested_values_to_schema(CONNECTION_SCHEMA, user_input or self.config_entry.data)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_entities(self, user_input=None):
        entry = self.config_entry
        if user_input is not None:
            username = self._conn[CONF_USERNAME]
            self.hass.config_entries.async_update_entry(
                entry, data={**self._conn, **user_input}, title=username, unique_id=username
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        schema = self.add_suggested_values_to_schema(ENTITIES_SCHEMA, entry.data)
        return self.async_show_form(step_id="entities", data_schema=schema)
