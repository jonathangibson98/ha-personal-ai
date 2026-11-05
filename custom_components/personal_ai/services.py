"""Actions that talk to a Personal AI Gateway: search for models and download them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from aiohttp import ClientError, ClientTimeout
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PersonalAiConfigKey

SERVICE_SEARCH = "search_models"
SERVICE_PULL = "pull_model"

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("query"): str,
        vol.Optional("source", default="all"): vol.In(["all", "ollama", "hf"]),
        vol.Optional("config_entry"): str,
    }
)
PULL_SCHEMA = vol.Schema({vol.Required("model"): str, vol.Optional("config_entry"): str})


def _entry(hass: HomeAssistant, entry_id: str | None):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise HomeAssistantError("Choose which Personal AI server to use (config_entry).")
    return entries[0]


async def _gateway(hass: HomeAssistant, entry_id: str | None, method: str, path: str, **kw: Any) -> dict:
    """Call the gateway's management API. Uses the server's API key as the admin password."""
    entry = _entry(hass, entry_id)
    root = str(entry.data[PersonalAiConfigKey.BASE_URL]).rstrip("/").removesuffix("/v1")
    headers = {"X-Admin-Token": entry.data.get(CONF_API_KEY, "")}
    session = async_get_clientsession(hass)
    try:
        async with session.request(
            method, f"{root}/api/{path}", headers=headers, timeout=ClientTimeout(total=30), **kw
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise HomeAssistantError(
                    f"Gateway said: {data.get('error', resp.status)}"
                    " (this server may not be a Personal AI Gateway, or it has a separate admin password)"
                )
            return data
    except (ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"Could not reach the gateway: {err}") from err


async def async_register_services(hass: HomeAssistant) -> None:
    """Register the gateway actions."""

    async def search(call: ServiceCall) -> dict:
        return await _gateway(
            hass,
            call.data.get("config_entry"),
            "GET",
            "models/search",
            params={"q": call.data["query"], "source": call.data["source"]},
        )

    async def pull(call: ServiceCall) -> dict:
        return await _gateway(
            hass, call.data.get("config_entry"), "POST", "models/pull", json={"name": call.data["model"]}
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, search, schema=SEARCH_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PULL, pull, schema=PULL_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
