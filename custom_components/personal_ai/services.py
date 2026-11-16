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

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .gateway import Gateway, GatewayAuthError, GatewayError

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
SERVICE_ADD_KEY = "add_api_key"
ADD_KEY_SCHEMA = vol.Schema(
    {
        vol.Required("provider"): str,
        vol.Required("key"): str,
        vol.Optional("base_url"): str,
        vol.Optional("config_entry"): str,
    }
)


def _entry(hass: HomeAssistant, entry_id: str | None):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise HomeAssistantError("Choose which Personal AI server to use (config_entry).")
    return entries[0]


async def _gateway(hass: HomeAssistant, entry_id: str | None, method: str, path: str, **kw: Any) -> dict:
    """Call the gateway's management API with the entry's admin password (or API key)."""
    try:
        return await Gateway(hass, _entry(hass, entry_id)).call(method, path, **kw)
    except GatewayAuthError as err:
        raise HomeAssistantError(
            "The gateway refused the admin password. Open the server's Configure and enter it."
        ) from err
    except GatewayError as err:
        raise HomeAssistantError(f"Gateway: {err}") from err


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

    async def add_key(call: ServiceCall) -> dict:
        entry_id = call.data.get("config_entry")
        name = call.data["provider"].strip().lower()
        state = await _gateway(hass, entry_id, "GET", "state")
        if name in state.get("providers", {}):
            await _gateway(hass, entry_id, "POST", f"providers/{name}/keys", json={"key": call.data["key"]})
            return {"ok": True, "provider": name, "added": "key"}
        url = call.data.get("base_url") or state.get("presets", {}).get(name, {}).get("base_url")
        if not url:
            raise HomeAssistantError(
                f"{name} isn't a provider yet and isn't a ready-made one. Add base_url to create it."
            )
        await _gateway(
            hass,
            entry_id,
            "POST",
            "providers",
            json={"name": name, "kind": "openai", "base_url": url, "key": call.data["key"]},
        )
        return {"ok": True, "provider": name, "added": "provider"}

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_KEY, add_key, schema=ADD_KEY_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, search, schema=SEARCH_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PULL, pull, schema=PULL_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
