from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries

from . import DOMAIN

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            mac = user_input["mac"].strip().upper()
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"iTag {mac}", data={"mac": mac})
        schema = vol.Schema({vol.Required("mac"): str})
        return self.async_show_form(step_id="user", data_schema=schema)