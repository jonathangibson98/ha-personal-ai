"""Config flow for Personal AI integration."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, CONF_API_KEY, CONF_MODEL, CONF_PROMPT, DEFAULT_MODEL, DEFAULT_PROMPT

class PersonalAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Personal AI."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(title="Personal AI", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): str,
                vol.Optional(CONF_PROMPT, default=DEFAULT_PROMPT): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )
