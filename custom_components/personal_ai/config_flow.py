"""Config flow for Personal AI integration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_LLM_HASS_API,
    CONF_MODEL,
    CONF_NAME,
    CONF_PROMPT,
)
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from openai import AsyncOpenAI, OpenAIError

from .const import (
    CONVERSATION_MODES,
    DEFAULT_CONVERSATION_MODE,
    DEFAULT_IMAGE_FILENAME_TEMPLATE,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
    DEFAULT_IMAGE_RESPONSE_FORMAT,
    DEFAULT_IMAGE_SIZE,
    DEFAULT_SEARCH_PROVIDER,
    DEFAULT_SEARCH_RESULTS,
    DEFAULT_TTS_EMOTION,
    DEFAULT_TTS_FORMAT,
    DEFAULT_TTS_SPEED,
    DEFAULT_TTS_VOICE,
    DOMAIN,
    IMAGE_FORMAT_OPTIONS,
    IMAGE_QUALITY_OPTIONS,
    IMAGE_RESPONSE_FORMAT_OPTIONS,
    FALLBACK_TTS_VOICES,
    IMAGE_SIZE_OPTIONS,
    LOGGER,
    RECOMMENDED_CONVERSATION_OPTIONS,
    SEARCH_PROVIDER_GOOGLE_CSE,
    SEARCH_PROVIDERS,
    TIMEOUT,
    TTS_FORMAT_OPTIONS,
    WEB_SEARCH_API_PREFIX,
    PersonalAiConfigKey,
    PersonalAiSubentryType,
)
from .image_storage import default_save_path, is_path_allowed
from .speech import EMOTION_NAMES, async_fetch_voices


class PersonalAiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Personal AI."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this handler."""
        return {
            PersonalAiSubentryType.CONVERSATION: ConversationFlowHandler,
            PersonalAiSubentryType.AI_TASK_DATA: AITaskDataFlowHandler,
            PersonalAiSubentryType.AI_IMAGE: AIImageFlowHandler,
            PersonalAiSubentryType.AI_TTS: AITTSFlowHandler,
            PersonalAiSubentryType.AI_STT: AISTTFlowHandler,
        }

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial config flow step."""
        LOGGER.debug("Config flow: step_user, input: %s", user_input)
        errors = {}
        if user_input is not None:
            self._async_abort_entries_match(user_input)
            LOGGER.debug(
                f"Initialising OpenAI client with base_url: {user_input[PersonalAiConfigKey.BASE_URL]}"
            )

            try:
                base_url = user_input.get(PersonalAiConfigKey.BASE_URL)
                api_key = user_input.get(CONF_API_KEY, "")
                client = AsyncOpenAI(
                    base_url=str(base_url) if base_url is not None else None,
                    api_key=str(api_key) if api_key is not None else "",
                    http_client=get_async_client(self.hass),
                )

                LOGGER.debug("Retrieving model list to ensure server is accessible")
                await client.with_options(timeout=TIMEOUT).models.list()
            except OpenAIError as err:
                LOGGER.exception(f"OpenAI Error: {err}")
                errors["base"] = "cannot_connect"
            except Exception as err:
                LOGGER.exception(f"Unexpected exception: {err}")
                errors["base"] = "unknown"
            else:
                LOGGER.debug("Server connection verified")
                return self.async_create_entry(
                    title=f"{user_input.get(PersonalAiConfigKey.SERVER_NAME, 'Personal AI Server')}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        PersonalAiConfigKey.SERVER_NAME, default="Personal AI Server"
                    ): str,
                    vol.Required(
                        PersonalAiConfigKey.BASE_URL, default="https://api.openai.com/v1"
                    ): str,
                    vol.Optional(CONF_API_KEY): str,
                }
            ),
            errors=errors,
        )


class PersonalAiSubentryFlowHandler(ConfigSubentryFlow):
    """Handle subentry flow for Personal AI."""

    @staticmethod
    def strip_model_pathing(model_name: str) -> str:
        """Strip file path and .gguf extension from model name."""
        matches = re.search(r"([^/]*)\.gguf$", model_name.strip())
        return matches[1] if matches else model_name


class ConversationFlowHandler(PersonalAiSubentryFlowHandler):
    """Handle subentry flow."""

    def get_llm_apis(self) -> list[SelectOptionDict]:
        """Return available LLM APIs as select options."""
        return [
            SelectOptionDict(
                label=api.name,
                value=api.id,
            )
            for api in llm.async_get_apis(self.hass)
            if not api.id.startswith(WEB_SEARCH_API_PREFIX)
        ]

    @staticmethod
    def _validate(data: dict[str, object]) -> dict[str, str]:
        """Check the web search settings are complete when search is switched on."""
        errors: dict[str, str] = {}
        if data.get(PersonalAiConfigKey.WEB_SEARCH):
            if not str(data.get(PersonalAiConfigKey.SEARCH_API_KEY) or "").strip():
                errors[PersonalAiConfigKey.SEARCH_API_KEY] = "search_api_key_required"
            if (
                data.get(PersonalAiConfigKey.SEARCH_PROVIDER) == SEARCH_PROVIDER_GOOGLE_CSE
                and not str(data.get(PersonalAiConfigKey.SEARCH_ENGINE_ID) or "").strip()
            ):
                errors[PersonalAiConfigKey.SEARCH_ENGINE_ID] = "search_engine_id_required"
        return errors

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the configuration schema for conversation options."""
        if options is None:
            options = {}
        llm_apis = self.get_llm_apis()
        client = self._get_entry().runtime_data

        try:
            response = await client.with_options(timeout=TIMEOUT).models.list()
            downloaded_models: list[SelectOptionDict] = [
                SelectOptionDict(
                    label=model.id,
                    value=model.id,
                )
                for model in response.data
            ]
            LOGGER.debug("Found models: %s", downloaded_models)
        except OpenAIError as err:
            LOGGER.exception(f"OpenAI Error retrieving models list: {err}")
            downloaded_models = []
        except Exception as err:
            LOGGER.exception(f"Unexpected exception retrieving models list: {err}")
            downloaded_models = []

        default_model: str = "Local"
        if raw_model := options.get(CONF_MODEL):
            default_model = str(raw_model)
        elif downloaded_models:
            default_model = downloaded_models[0]["value"]

        default_title = self.strip_model_pathing(default_model)
        default_name: str = f"{default_title} AI Agent"
        if raw_name := options.get(CONF_NAME):
            default_name = str(raw_name)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=default_name,
                ): str,
                vol.Required(
                    CONF_MODEL,
                    default=default_model,
                ): SelectSelector(
                    SelectSelectorConfig(options=downloaded_models, custom_value=True)
                ),
                vol.Optional(
                    PersonalAiConfigKey.CONVERSATION_MODE,
                    default=options.get(
                        PersonalAiConfigKey.CONVERSATION_MODE, DEFAULT_CONVERSATION_MODE
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(CONVERSATION_MODES),
                        translation_key="conversation_mode",
                    )
                ),
                vol.Optional(
                    CONF_PROMPT,
                    description={"suggested_value": options.get(CONF_PROMPT) or ""},
                ): TemplateSelector(),
                vol.Optional(
                    CONF_LLM_HASS_API,
                    default=options.get(
                        CONF_LLM_HASS_API,
                        RECOMMENDED_CONVERSATION_OPTIONS[CONF_LLM_HASS_API],
                    ),
                ): SelectSelector(SelectSelectorConfig(options=llm_apis, multiple=True)),
                vol.Optional(
                    PersonalAiConfigKey.WEB_SEARCH,
                    default=options.get(PersonalAiConfigKey.WEB_SEARCH, False),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.SEARCH_PROVIDER,
                    default=options.get(
                        PersonalAiConfigKey.SEARCH_PROVIDER, DEFAULT_SEARCH_PROVIDER
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(SEARCH_PROVIDERS),
                        translation_key="search_provider",
                    )
                ),
                vol.Optional(
                    PersonalAiConfigKey.SEARCH_API_KEY,
                    default=options.get(PersonalAiConfigKey.SEARCH_API_KEY, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    PersonalAiConfigKey.SEARCH_ENGINE_ID,
                    default=options.get(PersonalAiConfigKey.SEARCH_ENGINE_ID, ""),
                ): str,
                vol.Optional(
                    PersonalAiConfigKey.SEARCH_MAX_RESULTS,
                    default=options.get(
                        PersonalAiConfigKey.SEARCH_MAX_RESULTS, DEFAULT_SEARCH_RESULTS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Optional(
                    PersonalAiConfigKey.READ_WEBPAGES,
                    default=options.get(PersonalAiConfigKey.READ_WEBPAGES, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.PARALLEL_TOOL_CALLS,
                    default=options.get(PersonalAiConfigKey.PARALLEL_TOOL_CALLS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_EMOJIS,
                    default=options.get(PersonalAiConfigKey.STRIP_EMOJIS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_EMPHASIS,
                    default=options.get(PersonalAiConfigKey.STRIP_EMPHASIS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_LATEX,
                    default=options.get(PersonalAiConfigKey.STRIP_LATEX, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.EMOTION_TAGS,
                    default=options.get(PersonalAiConfigKey.EMOTION_TAGS, False),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.MANUAL_PROMPTING,
                    default=options.get(PersonalAiConfigKey.MANUAL_PROMPTING, False),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.TEMPERATURE,
                    default=options.get(PersonalAiConfigKey.TEMPERATURE, 1),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.05, mode=NumberSelectorMode.SLIDER)
                ),
            }
        )

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle user step to create a conversation subentry."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=await self.get_schema(),
            )
        user_input = user_input.copy()
        raw_name = user_input.get(CONF_NAME)
        custom_name = raw_name.strip() if isinstance(raw_name, str) else None
        if custom_name:
            user_input[CONF_NAME] = custom_name
        else:
            user_input.pop(CONF_NAME, None)

        if errors := self._validate(user_input):
            return self.async_show_form(
                step_id="user", data_schema=await self.get_schema(user_input), errors=errors
            )

        raw_model = user_input.get(CONF_MODEL, "Local")
        model_name = self.strip_model_pathing(str(raw_model) if raw_model is not None else "Local")
        entry_title = custom_name or f"{model_name} AI Agent"

        return self.async_create_entry(title=entry_title, data=user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfigure step for a conversation subentry."""
        if user_input is not None:
            user_input = user_input.copy()
            raw_name = user_input.get(CONF_NAME)
            custom_name = raw_name.strip() if isinstance(raw_name, str) else None
            if custom_name:
                user_input[CONF_NAME] = custom_name
            else:
                user_input.pop(CONF_NAME, None)

            if errors := self._validate(user_input):
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=await self.get_schema(user_input),
                    errors=errors,
                )

            raw_model = user_input.get(CONF_MODEL, "Local")
            model_name = self.strip_model_pathing(
                str(raw_model) if raw_model is not None else "Local"
            )
            entry_title = custom_name or f"{model_name} AI Agent"

            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=entry_title,
                data=user_input,
            )

        options = self._get_reconfigure_subentry().data.copy()

        hass_apis = [api.get("value") for api in self.get_llm_apis()]
        options["llm_hass_api"] = [
            api for api in options.get("llm_hass_api", []) if api in hass_apis
        ]

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=await self.get_schema(options),
        )


class AITaskDataFlowHandler(PersonalAiSubentryFlowHandler):
    """Handle subentry flow."""

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the configuration schema for AI task options."""
        if options is None:
            options = {}
        try:
            client = self._get_entry().runtime_data
            response = await client.with_options(timeout=TIMEOUT).models.list()
            downloaded_models: list[SelectOptionDict] = [
                SelectOptionDict(
                    label=model.id,
                    value=model.id,
                )
                for model in response.data
            ]
        except OpenAIError as err:
            LOGGER.exception(f"OpenAI Error retrieving models list: {err}")
            downloaded_models = []
        except Exception as err:
            LOGGER.exception(f"Unexpected exception retrieving models list: {err}")
            downloaded_models = []

        default_model: str = "Local"
        if raw_model := options.get(CONF_MODEL):
            default_model = str(raw_model)
        elif downloaded_models:
            default_model = downloaded_models[0]["value"]

        default_title = self.strip_model_pathing(default_model)
        default_name: str = f"{default_title} AI Task"
        if raw_name := options.get(CONF_NAME):
            default_name = str(raw_name)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=default_name,
                ): str,
                vol.Required(
                    CONF_MODEL,
                    default=default_model,
                ): SelectSelector(
                    SelectSelectorConfig(options=downloaded_models, custom_value=True)
                ),
                vol.Optional(
                    PersonalAiConfigKey.GENERATE_DATA,
                    default=options.get(PersonalAiConfigKey.GENERATE_DATA, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.GENERATE_IMAGE,
                    default=options.get(PersonalAiConfigKey.GENERATE_IMAGE, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.SUPPORT_ATTACHMENTS,
                    default=options.get(PersonalAiConfigKey.SUPPORT_ATTACHMENTS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_EMOJIS,
                    default=options.get(PersonalAiConfigKey.STRIP_EMOJIS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_EMPHASIS,
                    default=options.get(PersonalAiConfigKey.STRIP_EMPHASIS, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.STRIP_LATEX,
                    default=options.get(PersonalAiConfigKey.STRIP_LATEX, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.TEMPERATURE,
                    default=options.get(PersonalAiConfigKey.TEMPERATURE, 1),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.05, mode=NumberSelectorMode.SLIDER)
                ),
            }
        )

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle user step to create an AI task subentry."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=await self.get_schema(),
            )
        user_input = user_input.copy()
        raw_name = user_input.get(CONF_NAME)
        custom_name = raw_name.strip() if isinstance(raw_name, str) else None
        if custom_name:
            user_input[CONF_NAME] = custom_name
        else:
            user_input.pop(CONF_NAME, None)

        raw_model = user_input.get(CONF_MODEL, "Local")
        model_name = self.strip_model_pathing(str(raw_model) if raw_model is not None else "Local")
        entry_title = custom_name or f"{model_name} AI Task"

        return self.async_create_entry(title=entry_title, data=user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfigure step for an AI task subentry."""
        if user_input is not None:
            user_input = user_input.copy()
            raw_name = user_input.get(CONF_NAME)
            custom_name = raw_name.strip() if isinstance(raw_name, str) else None
            if custom_name:
                user_input[CONF_NAME] = custom_name
            else:
                user_input.pop(CONF_NAME, None)

            raw_model = user_input.get(CONF_MODEL, "Local")
            model_name = self.strip_model_pathing(
                str(raw_model) if raw_model is not None else "Local"
            )
            entry_title = custom_name or f"{model_name} AI Task"

            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=entry_title,
                data=user_input,
            )

        options = self._get_reconfigure_subentry().data.copy()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=await self.get_schema(options),
        )


def _select(values: tuple[str, ...], *, custom: bool = False) -> SelectSelector:
    """Build a simple dropdown from plain string values."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(label=value, value=value) for value in values],
            custom_value=custom,
        )
    )


class AIImageFlowHandler(PersonalAiSubentryFlowHandler):
    """Subentry flow for the dedicated AI Image task."""

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the configuration schema for AI image options."""
        if options is None:
            options = {}
        try:
            client = self._get_entry().runtime_data
            response = await client.with_options(timeout=TIMEOUT).models.list()
            available_models: list[SelectOptionDict] = [
                SelectOptionDict(label=model.id, value=model.id) for model in response.data
            ]
        except OpenAIError as err:
            LOGGER.exception(f"OpenAI Error retrieving models list: {err}")
            available_models = []
        except Exception as err:
            LOGGER.exception(f"Unexpected exception retrieving models list: {err}")
            available_models = []

        default_model: str = "Local"
        if raw_model := options.get(CONF_MODEL):
            default_model = str(raw_model)
        elif available_models:
            default_model = available_models[0]["value"]

        default_name: str = f"{self.strip_model_pathing(default_model)} AI Image"
        if raw_name := options.get(CONF_NAME):
            default_name = str(raw_name)

        return vol.Schema(
            {
                vol.Optional(CONF_NAME, default=default_name): str,
                vol.Required(CONF_MODEL, default=default_model): SelectSelector(
                    SelectSelectorConfig(options=available_models, custom_value=True)
                ),
                vol.Optional(
                    PersonalAiConfigKey.IMAGE_SIZE,
                    default=options.get(PersonalAiConfigKey.IMAGE_SIZE, DEFAULT_IMAGE_SIZE),
                ): _select(IMAGE_SIZE_OPTIONS, custom=True),
                vol.Optional(
                    PersonalAiConfigKey.IMAGE_QUALITY,
                    default=options.get(PersonalAiConfigKey.IMAGE_QUALITY, DEFAULT_IMAGE_QUALITY),
                ): _select(IMAGE_QUALITY_OPTIONS, custom=True),
                vol.Optional(
                    PersonalAiConfigKey.IMAGE_FORMAT,
                    default=options.get(PersonalAiConfigKey.IMAGE_FORMAT, DEFAULT_IMAGE_FORMAT),
                ): _select(IMAGE_FORMAT_OPTIONS),
                vol.Optional(
                    PersonalAiConfigKey.IMAGE_RESPONSE_FORMAT,
                    default=options.get(
                        PersonalAiConfigKey.IMAGE_RESPONSE_FORMAT, DEFAULT_IMAGE_RESPONSE_FORMAT
                    ),
                ): _select(IMAGE_RESPONSE_FORMAT_OPTIONS),
                vol.Optional(
                    PersonalAiConfigKey.SUPPORT_ATTACHMENTS,
                    default=options.get(PersonalAiConfigKey.SUPPORT_ATTACHMENTS, False),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.SAVE_IMAGES,
                    default=options.get(PersonalAiConfigKey.SAVE_IMAGES, True),
                ): bool,
                vol.Optional(
                    PersonalAiConfigKey.SAVE_PATH,
                    default=options.get(
                        PersonalAiConfigKey.SAVE_PATH, default_save_path(self.hass)
                    ),
                ): str,
                vol.Optional(
                    PersonalAiConfigKey.FILENAME_TEMPLATE,
                    default=options.get(
                        PersonalAiConfigKey.FILENAME_TEMPLATE, DEFAULT_IMAGE_FILENAME_TEMPLATE
                    ),
                ): str,
            }
        )

    async def _async_validate(self, user_input: dict[str, object]) -> dict[str, str]:
        """Check the save folder is somewhere HA is allowed to write."""
        errors: dict[str, str] = {}
        if user_input.get(PersonalAiConfigKey.SAVE_IMAGES, True):
            path = str(user_input.get(PersonalAiConfigKey.SAVE_PATH, "") or "").strip()
            allowed = await self.hass.async_add_executor_job(is_path_allowed, self.hass, path)
            if not allowed:
                errors[PersonalAiConfigKey.SAVE_PATH] = "path_not_allowed"
        return errors

    @staticmethod
    def _clean(user_input: dict[str, object]) -> tuple[dict[str, object], str | None]:
        """Tidy the submitted form and return (data, custom_name)."""
        data = user_input.copy()
        raw_name = data.get(CONF_NAME)
        custom_name = raw_name.strip() if isinstance(raw_name, str) else None
        if custom_name:
            data[CONF_NAME] = custom_name
        else:
            data.pop(CONF_NAME, None)
        if isinstance(data.get(PersonalAiConfigKey.SAVE_PATH), str):
            data[PersonalAiConfigKey.SAVE_PATH] = str(data[PersonalAiConfigKey.SAVE_PATH]).strip()
        return data, custom_name

    def _title(self, data: dict[str, object], custom_name: str | None) -> str:
        raw_model = data.get(CONF_MODEL, "Local")
        model_name = self.strip_model_pathing(str(raw_model) if raw_model is not None else "Local")
        return custom_name or f"{model_name} AI Image"

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle user step to create an AI image subentry."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=await self.get_schema())

        data, custom_name = self._clean(user_input)
        if errors := await self._async_validate(data):
            return self.async_show_form(
                step_id="user", data_schema=await self.get_schema(data), errors=errors
            )
        return self.async_create_entry(title=self._title(data, custom_name), data=data)

    async def async_step_reconfigure(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfigure step for an AI image subentry."""
        if user_input is not None:
            data, custom_name = self._clean(user_input)
            if errors := await self._async_validate(data):
                return self.async_show_form(
                    step_id="reconfigure", data_schema=await self.get_schema(data), errors=errors
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._title(data, custom_name),
                data=data,
            )

        options = self._get_reconfigure_subentry().data.copy()
        return self.async_show_form(
            step_id="reconfigure", data_schema=await self.get_schema(options)
        )


class SpeechFlowBase(PersonalAiSubentryFlowHandler):
    """Shared plumbing for the text-to-speech and speech-to-text subentry flows."""

    title_suffix = "AI Speech"

    async def _async_models(self) -> list[SelectOptionDict]:
        """List the models the server offers (empty if it can't say)."""
        try:
            client = self._get_entry().runtime_data
            response = await client.with_options(timeout=TIMEOUT).models.list()
            return [SelectOptionDict(label=model.id, value=model.id) for model in response.data]
        except OpenAIError as err:
            LOGGER.warning("Could not list models: %s", err)
        except Exception as err:
            LOGGER.warning("Unexpected error listing models: %s", err)
        return []

    def _default_model(self, options: dict[str, object], models: list[SelectOptionDict]) -> str:
        """Pick the model to preselect."""
        if raw_model := options.get(CONF_MODEL):
            return str(raw_model)
        return models[0]["value"] if models else "Local"

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the form for this flow."""
        raise NotImplementedError

    @staticmethod
    def _clean(user_input: dict[str, object]) -> tuple[dict[str, object], str | None]:
        """Tidy the submitted form and return (data, custom_name)."""
        data = user_input.copy()
        raw_name = data.get(CONF_NAME)
        custom_name = raw_name.strip() if isinstance(raw_name, str) else None
        if custom_name:
            data[CONF_NAME] = custom_name
        else:
            data.pop(CONF_NAME, None)
        return data, custom_name

    def _title(self, data: dict[str, object], custom_name: str | None) -> str:
        raw_model = data.get(CONF_MODEL, "Local")
        model_name = self.strip_model_pathing(str(raw_model) if raw_model is not None else "Local")
        return custom_name or f"{model_name} {self.title_suffix}"

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle the user step to create the subentry."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=await self.get_schema())
        data, custom_name = self._clean(user_input)
        return self.async_create_entry(title=self._title(data, custom_name), data=data)

    async def async_step_reconfigure(
        self, user_input: dict[str, object] | None = None
    ) -> SubentryFlowResult:
        """Handle the reconfigure step."""
        if user_input is not None:
            data, custom_name = self._clean(user_input)
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._title(data, custom_name),
                data=data,
            )
        options = self._get_reconfigure_subentry().data.copy()
        return self.async_show_form(
            step_id="reconfigure", data_schema=await self.get_schema(options)
        )


class AITTSFlowHandler(SpeechFlowBase):
    """Subentry flow for an AI voice (text-to-speech) entity."""

    title_suffix = "AI Voice"

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the configuration schema for the voice."""
        options = options or {}
        models = await self._async_models()
        default_model = self._default_model(options, models)

        entry = self._get_entry()
        voices = await async_fetch_voices(
            get_async_client(self.hass),
            str(entry.data[PersonalAiConfigKey.BASE_URL]),
            entry.data.get(CONF_API_KEY),
        )
        voice_options = voices or list(FALLBACK_TTS_VOICES)
        default_voice = str(options.get(PersonalAiConfigKey.TTS_VOICE) or "")
        if not default_voice:
            default_voice = voices[0] if voices else DEFAULT_TTS_VOICE

        return vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=str(
                        options.get(CONF_NAME)
                        or f"{self.strip_model_pathing(default_model)} {self.title_suffix}"
                    ),
                ): str,
                vol.Required(CONF_MODEL, default=default_model): SelectSelector(
                    SelectSelectorConfig(options=models, custom_value=True)
                ),
                vol.Optional(PersonalAiConfigKey.TTS_VOICE, default=default_voice): _select(
                    tuple(voice_options), custom=True
                ),
                vol.Optional(
                    PersonalAiConfigKey.TTS_SPEED,
                    default=options.get(PersonalAiConfigKey.TTS_SPEED, DEFAULT_TTS_SPEED),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.5, max=2.0, step=0.05, mode=NumberSelectorMode.SLIDER
                    )
                ),
                vol.Optional(
                    PersonalAiConfigKey.TTS_EMOTION,
                    default=options.get(PersonalAiConfigKey.TTS_EMOTION, DEFAULT_TTS_EMOTION),
                ): _select(EMOTION_NAMES),
                vol.Optional(
                    PersonalAiConfigKey.TTS_INSTRUCTIONS,
                    description={
                        "suggested_value": options.get(PersonalAiConfigKey.TTS_INSTRUCTIONS) or ""
                    },
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(
                    PersonalAiConfigKey.TTS_FORMAT,
                    default=options.get(PersonalAiConfigKey.TTS_FORMAT, DEFAULT_TTS_FORMAT),
                ): _select(TTS_FORMAT_OPTIONS),
            }
        )


class AISTTFlowHandler(SpeechFlowBase):
    """Subentry flow for an AI transcription (speech-to-text) entity."""

    title_suffix = "AI Transcription"

    async def get_schema(self, options: dict[str, object] | None = None):
        """Return the configuration schema for transcription."""
        options = options or {}
        models = await self._async_models()
        default_model = self._default_model(options, models)
        return vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=str(
                        options.get(CONF_NAME)
                        or f"{self.strip_model_pathing(default_model)} {self.title_suffix}"
                    ),
                ): str,
                vol.Required(CONF_MODEL, default=default_model): SelectSelector(
                    SelectSelectorConfig(options=models, custom_value=True)
                ),
                vol.Optional(
                    PersonalAiConfigKey.STT_LANGUAGE,
                    description={
                        "suggested_value": options.get(PersonalAiConfigKey.STT_LANGUAGE) or ""
                    },
                ): str,
                vol.Optional(
                    PersonalAiConfigKey.STT_PROMPT,
                    description={
                        "suggested_value": options.get(PersonalAiConfigKey.STT_PROMPT) or ""
                    },
                ): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )
