"""Speech-to-text platform for Personal AI."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import openai
from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PersonalAiConfigEntry
from .const import LOGGER, STT_TIMEOUT, PersonalAiConfigKey, PersonalAiSubentryType
from .entity import PersonalAiEntity
from .speech import pcm_to_wav

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
    """Set up speech-to-text entities."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type != PersonalAiSubentryType.AI_STT:
            continue
        async_add_entities(
            [PersonalAiSTTEntity(config_entry, subentry)], config_subentry_id=subentry_id
        )


class PersonalAiSTTEntity(PersonalAiEntity, SpeechToTextEntity):
    """AI speech-to-text (any OpenAI-compatible /audio/transcriptions server)."""

    _attr_name = None

    def __init__(self, entry: PersonalAiConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)

    @property
    def supported_languages(self) -> list[str]:
        """Return the languages the entity accepts."""
        return SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[AudioFormats]:
        """Home Assistant sends raw PCM, which is wrapped in a WAV header here."""
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        """Return the audio codecs the entity accepts."""
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        """Return the bit rates the entity accepts."""
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        """Return the sample rates the entity accepts."""
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        """Return the channel layouts the entity accepts."""
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Transcribe the audio stream."""
        pcm = b"".join([chunk async for chunk in stream])
        if not pcm:
            return SpeechResult(None, SpeechResultState.ERROR)

        wav = pcm_to_wav(
            pcm,
            rate=int(metadata.sample_rate),
            channels=int(metadata.channel),
            width=int(metadata.bit_rate) // 8,
        )

        data = self.subentry.data
        kwargs: dict[str, Any] = {"model": self.model, "file": ("speech.wav", wav, "audio/wav")}
        # An explicit language setting wins; otherwise use the language of the voice pipeline.
        language = str(data.get(PersonalAiConfigKey.STT_LANGUAGE) or metadata.language or "")
        if language.strip():
            kwargs["language"] = language.strip().split("-")[0].lower()
        if prompt := str(data.get(PersonalAiConfigKey.STT_PROMPT) or "").strip():
            kwargs["prompt"] = prompt

        client = self.entry.runtime_data.with_options(timeout=STT_TIMEOUT)
        try:
            response = await client.audio.transcriptions.create(**kwargs)
        except openai.OpenAIError as err:
            LOGGER.error("Speech-to-text request failed: %s", err)
            return SpeechResult(None, SpeechResultState.ERROR)

        text = (response.text or "").strip()
        LOGGER.debug("Transcribed: %s", text)
        if not text:
            return SpeechResult(None, SpeechResultState.ERROR)
        return SpeechResult(text, SpeechResultState.SUCCESS)
