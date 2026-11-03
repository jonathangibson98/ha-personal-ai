"""Conversation support for Personal AI."""

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PersonalAiConfigEntry
from .const import (
    DEFAULT_CONVERSATION_MODE,
    DEFAULT_SEARCH_PROVIDER,
    DEFAULT_SEARCH_RESULTS,
    DOMAIN,
    LOGGER,
    MODE_CHAT,
    SEARCH_PROVIDER_GOOGLE_CSE,
    WEB_SEARCH_API_PREFIX,
    PersonalAiConfigKey,
    PersonalAiSubentryType,
)
from .entity import PersonalAiEntity
from .prompt import resolve_prompt
from .web_search import SearchSettings, WebSearchAPI


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PersonalAiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type != PersonalAiSubentryType.CONVERSATION:
            continue
        async_add_entities(
            [PersonalAiConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry_id,
        )


class PersonalAiConversationEntity(PersonalAiEntity, conversation.ConversationEntity):
    """Personal AI conversation agent."""

    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: PersonalAiConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the agent."""
        super().__init__(entry, subentry)
        self._web_api_id = f"{WEB_SEARCH_API_PREFIX}{subentry.subentry_id}"
        if self._home_apis():
            self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    @property
    def _mode(self) -> str:
        return str(
            self.subentry.data.get(
                PersonalAiConfigKey.CONVERSATION_MODE, DEFAULT_CONVERSATION_MODE
            )
        )

    def _home_apis(self) -> list[str]:
        """Home Assistant LLM APIs this agent may use (none in plain chat mode)."""
        if self._mode == MODE_CHAT:
            return []
        return list(self.subentry.data.get(CONF_LLM_HASS_API) or [])

    def _search_settings(self) -> SearchSettings | None:
        """Search settings, or None if web search is off or not fully configured."""
        data = self.subentry.data
        if not data.get(PersonalAiConfigKey.WEB_SEARCH):
            return None
        provider = str(data.get(PersonalAiConfigKey.SEARCH_PROVIDER, DEFAULT_SEARCH_PROVIDER))
        api_key = str(data.get(PersonalAiConfigKey.SEARCH_API_KEY) or "").strip()
        engine_id = str(data.get(PersonalAiConfigKey.SEARCH_ENGINE_ID) or "").strip()
        if not api_key or (provider == SEARCH_PROVIDER_GOOGLE_CSE and not engine_id):
            LOGGER.warning("Web search is on for %s but its API key is missing", self.subentry.title)
            return None
        return SearchSettings(
            provider=provider,
            api_key=api_key,
            engine_id=engine_id,
            max_results=int(data.get(PersonalAiConfigKey.SEARCH_MAX_RESULTS, DEFAULT_SEARCH_RESULTS)),
            read_webpages=bool(data.get(PersonalAiConfigKey.READ_WEBPAGES, True)),
        )

    async def async_added_to_hass(self) -> None:
        """Offer the web search tools to this agent."""
        await super().async_added_to_hass()
        if settings := self._search_settings():
            api = WebSearchAPI(
                self.hass, self._web_api_id, f"Web search ({self.subentry.title})", settings
            )
            self.async_on_remove(llm.async_register_api(self.hass, api))

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process the user input and call the API."""
        LOGGER.debug("Received user input: %s", user_input)
        LOGGER.debug("Initial chat log: %s", chat_log)
        options = self.subentry.data
        system_prompt = resolve_prompt(options)

        hass_apis = [api.id for api in llm.async_get_apis(self.hass)]

        llm_apis = [api for api in self._home_apis() if api in hass_apis]
        if self._web_api_id in hass_apis:
            llm_apis.append(self._web_api_id)
        LOGGER.debug("System prompt: %s", system_prompt)
        LOGGER.debug("LLM APIs available: %s", llm_apis)

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                llm_apis,
                system_prompt,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            LOGGER.error("Error during conversation setup: %s", err)
            return err.as_conversation_result()

        await self._async_handle_chat_log(chat_log, user_input=user_input)

        result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        LOGGER.debug("Final conversation result: %s", result)
        return result
