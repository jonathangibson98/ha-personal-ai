"""Talk to a Personal AI Gateway's management API (add-on or Docker, on any device)."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import PersonalAiConfigKey

CONF_ADMIN_PASSWORD = "gateway_admin_password"


class GatewayError(Exception):
    """The gateway answered with an error, or couldn't be reached."""


class GatewayAuthError(GatewayError):
    """The gateway refused the admin password."""


class Gateway:
    """Management API client for one Personal AI server entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, admin_password: str | None = None) -> None:
        """Build a client from the entry's server address and keys."""
        self.hass = hass
        base = str(entry.data[PersonalAiConfigKey.BASE_URL]).rstrip("/")
        self.root = base.removesuffix("/v1")
        self.admin = (
            admin_password
            if admin_password is not None
            else entry.options.get(CONF_ADMIN_PASSWORD) or entry.data.get(CONF_API_KEY, "")
        )

    @property
    def host(self) -> str:
        """Scheme + host + port of the gateway, for showing MCP addresses."""
        return self.root

    async def health(self) -> dict | None:
        """Return the gateway's /health answer, or None if this isn't a gateway."""
        try:
            async with async_get_clientsession(self.hass).get(
                f"{self.root}/health", timeout=ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) and data.get("ok") else None
        except (ClientError, TimeoutError, ValueError):
            return None

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        """Call /api/<path> with the admin password."""
        try:
            async with async_get_clientsession(self.hass).request(
                method,
                f"{self.root}/api/{path}",
                headers={"X-Admin-Token": self.admin},
                timeout=ClientTimeout(total=kw.pop("timeout", 30)),
                **kw,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 401:
                    raise GatewayAuthError("The gateway refused the admin password.")
                if resp.status >= 400:
                    msg = data.get("error") if isinstance(data, dict) else None
                    if isinstance(msg, dict):
                        msg = msg.get("message")
                    raise GatewayError(str(msg or f"HTTP {resp.status}"))
                return data
        except (ClientError, TimeoutError) as err:
            raise GatewayError(f"Could not reach the gateway: {err}") from err
        except ValueError as err:
            raise GatewayError("The server didn't answer like a Personal AI Gateway.") from err
