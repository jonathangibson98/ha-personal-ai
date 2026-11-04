"""Pure helpers for the speech entities: emotion tags, WAV handling, voice lookup.

Nothing in here imports Home Assistant, so it is easy to test on its own.
"""

from __future__ import annotations

import io
import re
import struct
import wave
from typing import Any

# The sentences below are what an instruction-following voice model (like OpenAI's
# gpt-4o-mini-tts) is told for each emotion. Keep the names in step with the
# Personal AI Speech add-on, which understands the same tags.
EMOTION_TEXT: dict[str, str] = {
    "neutral": "Speak in a natural, even, conversational tone.",
    "happy": "Speak in a warm, happy, upbeat tone with a smile in your voice.",
    "cheerful": "Speak in a bright, cheerful, friendly tone.",
    "excited": "Speak with high energy and real excitement, quick and enthusiastic.",
    "sad": "Speak in a soft, sad, heavy-hearted tone, slow and subdued.",
    "angry": "Speak in an angry, intense, clipped tone.",
    "calm": "Speak in a calm, soothing, relaxed tone.",
    "gentle": "Speak in a gentle, tender, kind tone.",
    "whisper": "Whisper softly, as if sharing a secret.",
    "shout": "Shout loudly with force and urgency.",
    "sarcastic": "Speak in a dry, sarcastic, deadpan tone.",
    "serious": "Speak in a serious, grounded, no-nonsense tone.",
    "nervous": "Speak in a nervous, slightly shaky, hesitant tone.",
    "curious": "Speak with curiosity and interest, with a questioning lift.",
    "surprised": "Speak with surprise and astonishment.",
    "tired": "Speak in a tired, sleepy, low-energy tone.",
    "playful": "Speak in a playful, teasing, mischievous tone.",
    "confident": "Speak in a confident, assured, steady tone.",
    "empathetic": "Speak in a warm, empathetic, caring tone.",
    "laughing": "Speak while laughing, light and amused.",
}
EMOTION_NAMES = tuple(EMOTION_TEXT)

EMOTION_ALIASES = {
    "whispering": "whisper",
    "whispers": "whisper",
    "shouting": "shout",
    "yell": "shout",
    "laugh": "laughing",
    "laughs": "laughing",
    "mad": "angry",
    "upset": "sad",
    "joyful": "happy",
    "sleepy": "tired",
    "worried": "nervous",
    "soft": "gentle",
}

_TAG = re.compile(r"\[\s*([A-Za-z]{3,12})\s*\]")
GAP_SECONDS = 0.12


def normalize_emotion(name: object) -> str | None:
    """Return the canonical emotion name, or None if it is not a known one."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip().lower()
    key = EMOTION_ALIASES.get(key, key)
    return key if key in EMOTION_TEXT else None


def split_emotion_segments(text: str, default: str = "neutral") -> list[tuple[str, str]]:
    """Split text on [emotion] tags into (emotion, text) pieces. Unknown [tags] stay as text."""
    segments: list[tuple[str, str]] = []
    current = normalize_emotion(default) or "neutral"
    position = 0
    buffer = ""
    for match in _TAG.finditer(text):
        emotion = normalize_emotion(match.group(1))
        if emotion is None:
            continue
        buffer += text[position : match.start()]
        if buffer.strip():
            segments.append((current, buffer.strip()))
        buffer = ""
        current = emotion
        position = match.end()
    buffer += text[position:]
    if buffer.strip():
        segments.append((current, buffer.strip()))
    return segments


def build_instructions(base: object, emotion: str) -> str | None:
    """Combine an emotion with the user's own style instructions."""
    parts: list[str] = []
    if emotion != "neutral":
        parts.append(f"Emotion: {emotion}. {EMOTION_TEXT[emotion]}")
    if isinstance(base, str) and base.strip():
        parts.append(base.strip())
    return " ".join(parts) or None


def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Wrap raw PCM in a WAV header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def parse_wav(data: bytes) -> tuple[int, int, int, bytes]:
    """Return (rate, channels, bytes_per_sample, pcm). Copes with streaming WAV headers."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    position = 12
    fmt: tuple[int, int, int] | None = None
    while position + 8 <= len(data):
        chunk_id = data[position : position + 4]
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        body = position + 8
        if chunk_id == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", data[body : body + 16])
            if tag not in (1, 0xFFFE):
                raise ValueError("only PCM WAV audio is supported")
            fmt = (rate, channels, bits // 8)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("WAV has no format chunk")
            end = body + size
            if size in (0, 0xFFFFFFFF) or end > len(data):
                end = len(data)
            return (*fmt, data[body:end])
        position = body + size + (size & 1)
    raise ValueError("no audio found in WAV")


def join_wav(chunks: list[bytes], gap_seconds: float = GAP_SECONDS) -> bytes:
    """Join several WAV files into one, with a short pause between them."""
    parsed = [parse_wav(chunk) for chunk in chunks]
    rate, channels, width, _ = parsed[0]
    if any(item[:3] != (rate, channels, width) for item in parsed):
        raise ValueError("the speech pieces came back in different audio formats")
    gap = b"\x00" * (int(rate * gap_seconds) * channels * width)
    return pcm_to_wav(gap.join(item[3] for item in parsed), rate, channels, width)


async def async_fetch_voices(http: Any, base_url: str, api_key: str | None) -> list[str]:
    """Ask a Personal AI Speech add-on (or compatible server) for its voices.

    Returns an empty list if the server has no /audio/voices (e.g. OpenAI itself).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/audio/voices", headers=headers, timeout=10.0
        )
        if response.status_code != 200:
            return []
        voices = response.json().get("voices", [])
    except Exception:
        return []
    return [str(voice) for voice in voices if isinstance(voice, str)]
