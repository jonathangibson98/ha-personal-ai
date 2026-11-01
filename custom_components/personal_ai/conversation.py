"""Conversation platform for Personal AI."""
from __future__ import annotations

import logging
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_PROMPT, DEFAULT_PROMPT

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Personal AI conversation entity."""
    async_add_entities([PersonalAIConversationEntity(config_entry)])

class PersonalAIConversationEntity(conversation.ConversationEntity):
    """Personal AI conversation agent."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self._config_entry = config_entry
        self._attr_name = "Personal AI"
        self._attr_unique_id = f"{config_entry.entry_id}-conversation"

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages."""
        return "*"

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a conversation input."""
        response_text = f"Personal AI processed: '{user_input.text}'"
        
        intent_response = conversation.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_text)
        
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )
