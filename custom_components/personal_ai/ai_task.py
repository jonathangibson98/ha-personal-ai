"""AI Task integration for Personal AI."""

from __future__ import annotations

import base64
import binascii
from typing import Any

import openai
import orjson
from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client
from openai.types.responses.response_output_item import ImageGenerationCall

from . import PersonalAiConfigEntry
from .const import (
    DEFAULT_IMAGE_FILENAME_TEMPLATE,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
    DEFAULT_IMAGE_RESPONSE_FORMAT,
    DEFAULT_IMAGE_SIZE,
    EVENT_IMAGE_SAVED,
    IMAGE_TIMEOUT,
    LOGGER,
    PersonalAiConfigKey,
    PersonalAiSubentryType,
)
from .entity import PersonalAiEntity
from .helpers import _clean_json_data
from .image_storage import (
    async_save_image,
    build_filename,
    default_save_path,
    ext_for_mime,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PersonalAiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AI Task entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type == PersonalAiSubentryType.AI_TASK_DATA:
            async_add_entities(
                [PersonalAITaskEntity(config_entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )
        elif subentry.subentry_type == PersonalAiSubentryType.AI_IMAGE:
            async_add_entities(
                [PersonalAIImageEntity(config_entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class PersonalAITaskEntity(
    PersonalAiEntity,
    ai_task.AITaskEntity,
):
    """Personal AI AI Task entity."""

    _attr_name = None

    def __init__(self, entry: PersonalAiConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the AI Task entity."""
        ai_task.AITaskEntity.__init__(self)
        PersonalAiEntity.__init__(self, entry, subentry)

        features = ai_task.AITaskEntityFeature(0)
        if subentry.data.get(PersonalAiConfigKey.SUPPORT_ATTACHMENTS, True):
            features |= ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        if subentry.data.get(PersonalAiConfigKey.GENERATE_DATA, True):
            features |= ai_task.AITaskEntityFeature.GENERATE_DATA
        if subentry.data.get(PersonalAiConfigKey.GENERATE_IMAGE, True):
            features |= ai_task.AITaskEntityFeature.GENERATE_IMAGE
        self._attr_supported_features = features

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Handle a generate data task."""
        structure_name = task.name.strip() if task.name else None
        await self._async_handle_chat_log(chat_log, structure_name, task.structure)

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError("Last content in chat log is not an AssistantContent")

        text = (chat_log.content[-1].content or "").strip()
        LOGGER.debug("Raw text content from LLM for GenDataTask: %s", text)

        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id,
                data=text,
            )
        try:
            data = orjson.loads(text)
            LOGGER.debug("Structured data from LLM for GenDataTask: %s", data)
        except orjson.JSONDecodeError as err:
            LOGGER.error("Failed to parse structured response from LLM: %s", err)
            raise HomeAssistantError("Error with structured response") from err

        if isinstance(data, dict | list):
            data = _clean_json_data(data)

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=data,
        )

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Handle a generate image task."""
        structure_name = task.name.strip() if task.name else None
        await self._async_handle_chat_log(chat_log, structure_name, force_image=True)

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError("Last content in chat log is not an AssistantContent")

        image_call: ImageGenerationCall | None = None
        for content in reversed(chat_log.content):
            if not isinstance(content, conversation.AssistantContent):
                break
            native = getattr(content, "native", None)
            if isinstance(native, ImageGenerationCall) and native.result:
                image_call = native
                LOGGER.debug("ImageGenerationCall object: %s", image_call)
                break

        if image_call is None or image_call.result is None:
            raise HomeAssistantError("No image returned")

        try:
            image_data = base64.b64decode(image_call.result)
        except (binascii.Error, ValueError) as err:
            LOGGER.error("Failed to decode base64 image data: %s", err)
            raise HomeAssistantError("Invalid image response data") from err

        image_call.result = None

        output_format = getattr(image_call, "output_format", None)
        mime_type = f"image/{output_format}" if output_format else "image/png"

        width: int | None = None
        height: int | None = None
        size = getattr(image_call, "size", None)
        if size:
            try:
                width_str, height_str = str(size).split("x")
                width = int(width_str)
                height = int(height_str)
            except (ValueError, AttributeError):
                width = height = None

        revised_prompt = getattr(image_call, "revised_prompt", None)
        if isinstance(revised_prompt, str):
            revised_prompt = revised_prompt.strip()

        LOGGER.debug(
            "Generated image details: mime_type=%s, width=%s, height=%s, revised_prompt=%s",
            mime_type,
            width,
            height,
            revised_prompt,
        )

        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=mime_type,
            width=width,
            height=height,
            model=self.model,
            revised_prompt=revised_prompt,
        )


class PersonalAIImageEntity(
    PersonalAiEntity,
    ai_task.AITaskEntity,
):
    """Dedicated image-generation AI Task (OpenAI-style /v1/images endpoints).

    Unlike the combined AI Task above, this one talks to the plain Images API that
    most local servers (PersonalAI, stable-diffusion wrappers, Gemini-FastAPI, OpenAI
    itself) expose, and it can save every image to a folder you choose.
    """

    _attr_name = None

    def __init__(self, entry: PersonalAiConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the AI Image entity."""
        ai_task.AITaskEntity.__init__(self)
        PersonalAiEntity.__init__(self, entry, subentry)

        features = ai_task.AITaskEntityFeature.GENERATE_IMAGE
        if subentry.data.get(PersonalAiConfigKey.SUPPORT_ATTACHMENTS, False):
            features |= ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        self._attr_supported_features = features

    # ------------------------------------------------------------------ helpers

    def _opt(self, key: str, default: Any) -> Any:
        value = self.subentry.data.get(key, default)
        return default if value is None or value == "" else value

    def _build_request_kwargs(self) -> dict[str, Any]:
        """Turn the saved options into kwargs for images.generate / images.edit."""
        kwargs: dict[str, Any] = {"model": self.model, "n": 1}

        size = str(self._opt(PersonalAiConfigKey.IMAGE_SIZE, DEFAULT_IMAGE_SIZE))
        if size != "auto":
            kwargs["size"] = size

        quality = str(self._opt(PersonalAiConfigKey.IMAGE_QUALITY, DEFAULT_IMAGE_QUALITY))
        if quality != "auto":
            kwargs["quality"] = quality

        # Only send output_format when it's not the default; dall-e style servers
        # reject parameters they don't know.
        image_format = str(self._opt(PersonalAiConfigKey.IMAGE_FORMAT, DEFAULT_IMAGE_FORMAT))
        if image_format != DEFAULT_IMAGE_FORMAT:
            kwargs["output_format"] = image_format

        response_format = str(
            self._opt(PersonalAiConfigKey.IMAGE_RESPONSE_FORMAT, DEFAULT_IMAGE_RESPONSE_FORMAT)
        )
        if response_format != "omit":
            kwargs["response_format"] = response_format

        return kwargs

    async def _async_read_attachments(
        self, task: ai_task.GenImageTask
    ) -> list[tuple[str, bytes, str]]:
        """Read attached images (for image-to-image / edit requests)."""
        attachments = getattr(task, "attachments", None) or []
        if not attachments:
            return []

        def _read() -> list[tuple[str, bytes, str]]:
            files: list[tuple[str, bytes, str]] = []
            for index, attachment in enumerate(attachments):
                mime = str(getattr(attachment, "mime_type", "") or "image/png")
                if not mime.startswith("image/"):
                    raise HomeAssistantError(
                        f"Attachment {index + 1} is '{mime}', only images can be sent to the image model"
                    )
                path = attachment.path
                files.append((path.name or f"image_{index}.png", path.read_bytes(), mime))
            return files

        return await self.hass.async_add_executor_job(_read)

    async def _async_download(self, url: str) -> tuple[bytes, str | None]:
        """Download an image when the server only gives us a URL."""
        try:
            response = await get_async_client(self.hass).get(
                url, timeout=IMAGE_TIMEOUT, follow_redirects=True
            )
            response.raise_for_status()
        except Exception as err:
            raise HomeAssistantError(f"Could not download generated image: {err}") from err
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        return response.content, content_type or None

    # --------------------------------------------------------------- generation

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Handle a generate image task."""
        prompt = (task.instructions or "").strip()
        if not prompt:
            raise HomeAssistantError("No prompt was given for image generation")

        client = self.entry.runtime_data.with_options(timeout=IMAGE_TIMEOUT)
        kwargs = self._build_request_kwargs()
        attachments = await self._async_read_attachments(task)

        LOGGER.debug(
            "Image request: model=%s, kwargs=%s, attachments=%d", self.model, kwargs, len(attachments)
        )

        try:
            if attachments:
                images: Any = (
                    attachments[0] if len(attachments) == 1 else attachments
                )
                result = await client.images.edit(image=images, prompt=prompt, **kwargs)
            else:
                result = await client.images.generate(prompt=prompt, **kwargs)
        except openai.OpenAIError as err:
            LOGGER.error("Error requesting image from API: %s", err)
            raise HomeAssistantError(f"Error talking to image API: {err}") from err

        if not result.data:
            raise HomeAssistantError("No image returned")

        item = result.data[0]
        mime_type: str | None = None

        if getattr(item, "b64_json", None):
            try:
                image_data = base64.b64decode(item.b64_json)
            except (binascii.Error, ValueError) as err:
                raise HomeAssistantError("Invalid image response data") from err
        elif getattr(item, "url", None):
            image_data, mime_type = await self._async_download(item.url)
        else:
            raise HomeAssistantError(
                "Server returned neither image data nor a URL. "
                "Try changing 'Response format' in this AI Image task's settings."
            )

        mime_type = mime_type or self._sniff_mime(image_data)

        width = height = None
        size = str(kwargs.get("size", ""))
        if "x" in size:
            try:
                width_str, height_str = size.split("x")
                width, height = int(width_str), int(height_str)
            except ValueError:
                width = height = None

        revised_prompt = getattr(item, "revised_prompt", None)
        if isinstance(revised_prompt, str):
            revised_prompt = revised_prompt.strip() or None

        await self._async_maybe_save(prompt, image_data, mime_type)

        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=mime_type,
            width=width,
            height=height,
            model=self.model,
            revised_prompt=revised_prompt,
        )

    @staticmethod
    def _sniff_mime(data: bytes) -> str:
        """Guess the mime type from magic bytes (servers often lie or say nothing)."""
        if data.startswith(b"\x89PNG"):
            return "image/png"
        if data.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:3] == b"GIF":
            return "image/gif"
        return "image/png"

    async def _async_maybe_save(self, prompt: str, image_data: bytes, mime_type: str) -> None:
        """Save the image to the configured folder (if saving is on) and fire an event."""
        if not self.subentry.data.get(PersonalAiConfigKey.SAVE_IMAGES, True):
            return

        directory = str(self._opt(PersonalAiConfigKey.SAVE_PATH, default_save_path(self.hass)))
        filename = build_filename(
            str(
                self._opt(
                    PersonalAiConfigKey.FILENAME_TEMPLATE, DEFAULT_IMAGE_FILENAME_TEMPLATE
                )
            ),
            entity_name=self.subentry.title,
            model=self.model,
            prompt=prompt,
            extension=ext_for_mime(mime_type),
        )

        saved = await async_save_image(
            self.hass, directory=directory, filename=filename, data=image_data
        )
        LOGGER.info("Saved generated image to %s", saved.path)

        self.hass.bus.async_fire(
            EVENT_IMAGE_SAVED,
            {
                "entity_id": self.entity_id,
                "path": saved.path,
                "url": saved.url,
                "prompt": prompt,
                "model": self.model,
                "mime_type": mime_type,
            },
        )
