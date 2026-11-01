"""Helpers for saving generated images to disk."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import DEFAULT_IMAGE_FILENAME_TEMPLATE, DEFAULT_IMAGE_SAVE_SUBDIR

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM_LEN = 120
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(slots=True)
class SavedImage:
    """Where a generated image ended up."""

    path: str
    url: str | None


def default_save_path(hass: HomeAssistant) -> str:
    """Return the default save folder (inside <config>/www, served at /local)."""
    return hass.config.path(DEFAULT_IMAGE_SAVE_SUBDIR)


def is_path_allowed(hass: HomeAssistant, path: str) -> bool:
    """Return True if HA is allowed to write to this folder."""
    if not path or not path.strip():
        return False
    return hass.config.is_allowed_path(path.strip())


def ext_for_mime(mime_type: str) -> str:
    """Return a file extension (no dot) for an image mime type."""
    return _EXT_BY_MIME.get(mime_type.lower(), "png")


def build_filename(
    template: str | None,
    *,
    entity_name: str,
    model: str,
    prompt: str,
    extension: str,
    now: datetime | None = None,
) -> str:
    """Build a safe filename from the user's template.

    Supported placeholders: {date} {time} {timestamp} {slug} {name} {model}
    Plus any strftime codes such as %Y-%m-%d.
    """
    now = now or dt_util.now()
    template = (template or "").strip() or DEFAULT_IMAGE_FILENAME_TEMPLATE

    values = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "timestamp": str(int(now.timestamp())),
        "slug": slugify(prompt)[:40] or "image",
        "name": slugify(entity_name) or "image",
        "model": slugify(model) or "model",
    }

    try:
        rendered = now.strftime(template)
        rendered = rendered.format_map(_SafeDict(values))
    except (ValueError, KeyError, IndexError):
        rendered = DEFAULT_IMAGE_FILENAME_TEMPLATE.format_map(values)

    # Never let a template walk out of the folder.
    rendered = rendered.replace("/", "_").replace("\\", "_")
    rendered = _SAFE_CHARS.sub("_", rendered).strip("._") or "image"

    # Strip an extension the user typed themselves, we add the right one.
    stem = Path(rendered).stem if Path(rendered).suffix.lower().lstrip(".") in {
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
    } else rendered

    return f"{stem[:_MAX_STEM_LEN]}.{extension}"


class _SafeDict(dict):
    """dict that leaves unknown {placeholders} as-is instead of exploding."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _write_unique(directory: str, filename: str, data: bytes) -> Path:
    """Blocking: create folder, pick a free filename, write the file."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)

    target = folder / filename
    counter = 1
    while target.exists():
        target = folder / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1

    target.write_bytes(data)
    return target


def _public_url(hass: HomeAssistant, file_path: Path) -> str | None:
    """Work out a URL / media-source id for the saved file, if there is one."""
    resolved = file_path.resolve()

    www = Path(hass.config.path("www")).resolve()
    if resolved.is_relative_to(www):
        return f"/local/{resolved.relative_to(www).as_posix()}"

    for media_id, base in hass.config.media_dirs.items():
        base_path = Path(base).resolve()
        if resolved.is_relative_to(base_path):
            return f"media-source://media_source/{media_id}/{resolved.relative_to(base_path).as_posix()}"

    return None


async def async_save_image(
    hass: HomeAssistant,
    *,
    directory: str,
    filename: str,
    data: bytes,
) -> SavedImage:
    """Save image bytes to disk and return where they went."""
    directory = directory.strip()
    if not is_path_allowed(hass, directory):
        raise HomeAssistantError(
            f"Save folder '{directory}' is not allowed. Add it to "
            "allowlist_external_dirs in configuration.yaml, or pick a folder under "
            "/config/www or /media."
        )

    try:
        saved = await hass.async_add_executor_job(_write_unique, directory, filename, data)
    except OSError as err:
        raise HomeAssistantError(f"Could not save image to '{directory}': {err}") from err

    url = await hass.async_add_executor_job(_public_url, hass, saved)
    return SavedImage(path=str(saved), url=url)
