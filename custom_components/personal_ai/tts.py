"""Text-to-speech platform for Personal AI."""

from __future__ import annotations

import asyncio
from typing import Any

import openai
from homeassistant.components.tts import ATTR_VOICE, TextToSpeechEntity, TtsAudioType, Voice
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client

from . import PersonalAiConfigEntry
from .const import (
    DEFAULT_TTS_EMOTION,
    DEFAULT_TTS_FORMAT,
    DEFAULT_TTS_SPEED,
    DEFAULT_TTS_VOICE,
    FALLBACK_TTS_VOICES,
    LOGGER,
    TTS_ATTR_EMOTION,
    TTS_ATTR_INSTRUCTIONS,
    TTS_ATTR_SPEED,
    TTS_TIMEOUT,
    PersonalAiConfigKey,
    PersonalAiSubentryType,
)
from .entity import PersonalAiEntity
from .speech import (
    async_fetch_voices,
    build_instructions,
    join_wav,
    normalize_emotion,
    split_emotion_segments,
)

try:  # the list of languages Home Assistant knows about
    from homeassistant.generated.languages import LANGUAGES

    SUPPORTED_LANGUAGES = sorted(LANGUAGES)
except ImportError:  # pragma: no cover - very old Home Assistant
    SUPPORTED_LANGUAGES = ["en"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PersonalAiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up text-to-speech entities."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type != PersonalAiSubentryType.AI_TTS:
            continue
        async_add_entities(
            [PersonalAiTTSEntity(config_entry, subentry)], config_subentry_id=subentry_id
        )


class PersonalAiTTSEntity(PersonalAiEntity, TextToSpeechEntity):
    """AI text-to-speech with emotion."""

    _attr_name = None
    _attr_default_language = "en"

    def __init__(self, entry: PersonalAiConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        data = subentry.data
        self._voices: list[str] = list(FALLBACK_TTS_VOICES)
        self._attr_supported_languages = SUPPORTED_LANGUAGES
        self._attr_supported_options = [
            ATTR_VOICE,
            TTS_ATTR_SPEED,
            TTS_ATTR_INSTRUCTIONS,
            TTS_ATTR_EMOTION,
        ]
        self._attr_default_options = {
            ATTR_VOICE: data.get(PersonalAiConfigKey.TTS_VOICE, DEFAULT_TTS_VOICE),
            TTS_ATTR_SPEED: data.get(PersonalAiConfigKey.TTS_SPEED, DEFAULT_TTS_SPEED),
            TTS_ATTR_EMOTION: data.get(PersonalAiConfigKey.TTS_EMOTION, DEFAULT_TTS_EMOTION),
            TTS_ATTR_INSTRUCTIONS: data.get(PersonalAiConfigKey.TTS_INSTRUCTIONS, ""),
        }

    async def async_added_to_hass(self) -> None:
        """Look up the server's own voice list (the add-on has one) for the voice picker."""
        await super().async_added_to_hass()
        voices = await async_fetch_voices(
            get_async_client(self.hass),
            str(self.entry.data["base_url"]),
            self.entry.data.get(CONF_API_KEY),
        )
        if voices:
            self._voices = voices

    @callback
    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        """Return the voices to show in the voice picker."""
        return [Voice(voice, voice) for voice in self._voices]

    async def _async_speak(
        self, text: str, *, voice: str, speed: float, instructions: str | None, fmt: str
    ) -> bytes:
        """Ask the server for one piece of speech."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "voice": voice,
            "input": text,
            "response_format": fmt,
        }
        if abs(speed - 1.0) > 0.01:
            kwargs["speed"] = speed
        if instructions:
            # extra_body works on every openai library version, including ones that
            # predate the `instructions` argument.
            kwargs["extra_body"] = {"instructions": instructions}
        client = self.entry.runtime_data.with_options(timeout=TTS_TIMEOUT)
        try:
            response = await client.audio.speech.create(**kwargs)
        except openai.OpenAIError as err:
            raise HomeAssistantError(f"The speech server returned an error: {err}") from err
        return response.content

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Turn the message into speech. [emotion] tags change the delivery mid-message."""
        data = self.subentry.data
        voice = str(
            options.get(ATTR_VOICE) or data.get(PersonalAiConfigKey.TTS_VOICE) or DEFAULT_TTS_VOICE
        )
        speed = float(
            options.get(TTS_ATTR_SPEED)
            or data.get(PersonalAiConfigKey.TTS_SPEED)
            or DEFAULT_TTS_SPEED
        )
        base_instructions = options.get(
            TTS_ATTR_INSTRUCTIONS, data.get(PersonalAiConfigKey.TTS_INSTRUCTIONS, "")
        )
        default_emotion = (
            normalize_emotion(options.get(TTS_ATTR_EMOTION))
            or normalize_emotion(data.get(PersonalAiConfigKey.TTS_EMOTION))
            or DEFAULT_TTS_EMOTION
        )
        fmt = str(data.get(PersonalAiConfigKey.TTS_FORMAT, DEFAULT_TTS_FORMAT))

        segments = split_emotion_segments(message, default_emotion)
        if not segments:
            raise HomeAssistantError("There is no text to speak")
        LOGGER.debug("Speaking %d segment(s): %s", len(segments), [e for e, _ in segments])

        # Several pieces are asked for as WAV so they can be joined cleanly.
        piece_format = fmt if len(segments) == 1 else "wav"
        pieces = await asyncio.gather(
            *(
                self._async_speak(
                    text,
                    voice=voice,
                    speed=speed,
                    instructions=build_instructions(base_instructions, emotion),
                    fmt=piece_format,
                )
                for emotion, text in segments
            )
        )
        if len(pieces) == 1:
            return fmt, pieces[0]
        try:
            return "wav", join_wav(list(pieces))
        except ValueError as err:
            raise HomeAssistantError(f"Could not join the speech pieces: {err}") from err
