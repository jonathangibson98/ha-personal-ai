"""Configure (options) flow: manage a Personal AI Gateway from Home Assistant.

Works for the add-on and for the Docker version on another device, because everything goes
through the gateway's own management API at the entry's server address.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .gateway import CONF_ADMIN_PASSWORD, Gateway, GatewayAuthError, GatewayError

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
TEXT = TextSelector()


def _select(options: list[tuple[str, str]]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=l) for v, l in options],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _tail(text: str, lines: int = 14) -> str:
    out = "\n".join(text.rstrip().split("\n")[-lines:]).replace("```", "'''")
    return f"```\n{out or '…'}\n```"


class GatewayOptionsFlow(OptionsFlow):
    """Menu for the gateway behind this server: providers, accounts, devices, models."""

    def __init__(self) -> None:
        """Set up per-flow state."""
        self.gw: Gateway | None = None
        self.acct: str = ""
        self.session: dict[str, Any] = {}
        self.server_id: str = ""
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ helpers
    def _done(self, message: str) -> ConfigFlowResult:
        """Close the dialog with a message (options stay as they are)."""
        return self.async_abort(reason="message", description_placeholders={"message": message})

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        assert self.gw is not None
        return await self.gw.call(method, path, **kw)

    def _fail(self, err: Exception) -> ConfigFlowResult:
        return self._done(f"The gateway said: {err}")

    # ------------------------------------------------------------ start
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Check this server is a gateway and that we may manage it."""
        self.gw = Gateway(self.hass, self.config_entry)
        health = await self.gw.health()
        if not health:
            return self.async_abort(reason="not_gateway")
        try:
            await self._api("GET", "state")
        except GatewayAuthError:
            return await self.async_step_admin()
        except GatewayError as err:
            return self._fail(err)
        return await self.async_step_menu()

    async def async_step_admin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the gateway's admin password when it isn't the API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            pw = str(user_input.get(CONF_ADMIN_PASSWORD, ""))
            self.gw = Gateway(self.hass, self.config_entry, admin_password=pw)
            try:
                await self._api("GET", "state")
            except GatewayAuthError:
                errors["base"] = "bad_admin"
            except GatewayError as err:
                return self._fail(err)
            else:
                return self.async_create_entry(data={**self.config_entry.options, CONF_ADMIN_PASSWORD: pw})
        return self.async_show_form(
            step_id="admin", data_schema=vol.Schema({vol.Required(CONF_ADMIN_PASSWORD): PASSWORD}), errors=errors
        )

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Main menu."""
        try:
            state = await self._api("GET", "state")
        except GatewayError as err:
            return self._fail(err)
        provs = state.get("providers", {})
        return self.async_show_menu(
            step_id="menu",
            menu_options=["providers", "accounts", "devices", "download_model"],
            description_placeholders={
                "version": str(state.get("version", "?")),
                "providers": ", ".join(sorted(provs)) or "none",
                "where": "Home Assistant add-on" if state.get("addon") else "Docker",
            },
        )

    # ------------------------------------------------------------ providers and API keys
    async def async_step_providers(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Providers submenu."""
        return self.async_show_menu(
            step_id="providers", menu_options=["add_preset", "add_custom", "add_key", "remove_provider"]
        )

    async def async_step_add_preset(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a ready-made provider (OpenAI, Gemini, Groq...) with its key."""
        state = await self._api("GET", "state")
        presets: dict[str, dict] = state.get("presets", {})
        if user_input is not None:
            name = user_input["preset"]
            try:
                await self._api(
                    "POST",
                    "providers",
                    json={
                        "name": name,
                        "kind": "openai",
                        "base_url": presets[name]["base_url"],
                        "key": user_input.get("key", ""),
                    },
                )
            except GatewayError as err:
                return self._fail(err)
            return self._done(f"Added {presets[name]['label']}. Its models are named {name}/<model>.")
        return self.async_show_form(
            step_id="add_preset",
            data_schema=vol.Schema(
                {
                    vol.Required("preset"): _select([(k, v["label"]) for k, v in presets.items()]),
                    vol.Required("key"): PASSWORD,
                }
            ),
        )

    async def async_step_add_custom(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add any other OpenAI-compatible server."""
        if user_input is not None:
            try:
                await self._api(
                    "POST",
                    "providers",
                    json={
                        "name": user_input["name"],
                        "kind": "openai",
                        "base_url": user_input["base_url"],
                        "key": user_input.get("key", ""),
                    },
                )
            except GatewayError as err:
                return self._fail(err)
            return self._done(f"Added {user_input['name']}. Its models are named {user_input['name']}/<model>.")
        return self.async_show_form(
            step_id="add_custom",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): TEXT,
                    vol.Required("base_url"): URL,
                    vol.Optional("key"): PASSWORD,
                }
            ),
        )

    async def async_step_add_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add another API key to a provider (the gateway rotates between them)."""
        state = await self._api("GET", "state")
        names = [n for n, p in state.get("providers", {}).items() if p.get("kind") == "openai"]
        if not names:
            return self._done("No API-key providers yet. Add one first.")
        if user_input is not None:
            try:
                await self._api("POST", f"providers/{user_input['provider']}/keys", json={"key": user_input["key"]})
            except GatewayError as err:
                return self._fail(err)
            return self._done(f"Key added to {user_input['provider']}.")
        return self.async_show_form(
            step_id="add_key",
            data_schema=vol.Schema(
                {vol.Required("provider"): _select([(n, n) for n in names]), vol.Required("key"): PASSWORD}
            ),
        )

    async def async_step_remove_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Remove a provider and its saved keys."""
        state = await self._api("GET", "state")
        names = [n for n in state.get("providers", {}) if n != "ollama"]
        if not names:
            return self._done("There's nothing to remove.")
        if user_input is not None:
            try:
                await self._api("DELETE", f"providers/{user_input['provider']}")
            except GatewayError as err:
                return self._fail(err)
            return self._done(f"Removed {user_input['provider']}.")
        return self.async_show_form(
            step_id="remove_provider",
            data_schema=vol.Schema({vol.Required("provider"): _select([(n, n) for n in names])}),
        )

    # ------------------------------------------------------------ sign in with an account
    async def _accounts(self) -> list[dict]:
        return (await self._api("GET", "accounts"))["accounts"]

    async def async_step_accounts(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick Claude, ChatGPT or Gemini."""
        try:
            accts = await self._accounts()
        except GatewayError as err:
            return self._fail(err)
        if user_input is not None:
            self.acct = user_input["account"]
            return await self.async_step_account()

        def label(a: dict) -> str:
            st = "signed in" if a["signed_in"] else "installed, not signed in" if a["installed"] else "not installed"
            return f"{a['label']} ({st}) - {a['plan']}"

        return self.async_show_form(
            step_id="accounts",
            data_schema=vol.Schema({vol.Required("account"): _select([(a["id"], label(a)) for a in accts])}),
        )

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What to do with the chosen account."""
        a = next(x for x in await self._accounts() if x["id"] == self.acct)
        opts = ["account_install"]
        if a["installed"]:
            opts.append("account_login")
        if a["signed_in"]:
            opts.append("account_logout")
        if a["installed"] and not a["in_providers"]:
            opts.append("account_provider")
        return self.async_show_menu(
            step_id="account",
            menu_options=opts,
            description_placeholders={
                "account": a["label"],
                "status": "signed in" if a["signed_in"] else "installed" if a["installed"] else "not installed",
                "models": ", ".join(f"{a['provider']}/{m}" for m in a["models"]),
            },
        )

    async def _wait_session(self, sid: str, seconds: float, until_link: bool = False) -> dict:
        view: dict = {}
        for _ in range(int(seconds / 1.5)):
            view = await self._api("GET", f"sessions/{sid}")
            if view["status"] != "running" or (until_link and view.get("links")):
                break
            await asyncio.sleep(1.5)
        return view

    async def async_step_account_install(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Install or update the account's official tool (shows progress)."""
        if self._task is None:
            try:
                self.session = await self._api("POST", f"accounts/{self.acct}/install")
            except GatewayError as err:
                return self._fail(err)

            async def run() -> None:
                self.session = await self._wait_session(self.session["id"], 600)

            self._task = self.hass.async_create_task(run())
        if not self._task.done():
            return self.async_show_progress(
                step_id="account_install", progress_action="installing", progress_task=self._task
            )
        self._task = None
        return self.async_show_progress_done(next_step_id="account_install_done")

    async def async_step_account_install_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Report how the install went."""
        if self.session.get("status") == "done":
            return self._done(
                "Installed. Open Configure again, pick the account and choose Sign in."
            )
        return self._done("The install didn't finish:\n\n" + _tail(self.session.get("output", ""), 10))

    async def async_step_account_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start signing in, show the sign-in link and take the code."""
        if user_input is None:
            try:
                self.session = await self._api("POST", f"accounts/{self.acct}/login")
            except GatewayError as err:
                return self._fail(err)
            self.session = await self._wait_session(self.session["id"], 15, until_link=True)
        else:
            code = str(user_input.get("code") or "").strip()
            try:
                if code:
                    await self._api("POST", f"sessions/{self.session['id']}/input", json={"text": code})
                self.session = await self._wait_session(self.session["id"], 45 if code else 20)
            except GatewayError as err:
                return self._fail(err)
        status = self.session.get("status")
        if status == "done":
            return self._done(
                "Signed in. The account is now a provider you can pick as a model, "
                "and you can give it device access under Devices."
            )
        if status in ("failed", "cancelled"):
            return self._done("Sign-in stopped:\n\n" + _tail(self.session.get("output", "")))
        links = self.session.get("links") or []
        return self.async_show_form(
            step_id="account_login",
            data_schema=vol.Schema({vol.Optional("code"): TEXT}),
            description_placeholders={
                "link": f"[Open the sign-in page]({links[0]})" if links else "(no link yet, submit to refresh)",
                "output": _tail(self.session.get("output", "")),
            },
        )

    async def async_step_account_logout(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign out of the account."""
        try:
            await self._api("POST", f"accounts/{self.acct}/logout")
        except GatewayError as err:
            return self._fail(err)
        return self._done("Signed out.")

    async def async_step_account_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Put the account back in the providers list."""
        try:
            await self._api("POST", f"accounts/{self.acct}/provider")
        except GatewayError as err:
            return self._fail(err)
        return self._done("Added to providers.")

    # ------------------------------------------------------------ devices (MCP servers)
    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Devices submenu."""
        try:
            m = await self._api("GET", "mcp")
        except GatewayError as err:
            return self._fail(err)
        opts = ["new_server"]
        if m["servers"]:
            opts += ["edit_server", "ai_server"]
        if not m["ha_locked"]:
            opts.append("ha_link")
        if not m["ha_connected"]:
            return await self.async_step_ha_link() if not m["ha_locked"] else self._done(
                "The gateway add-on can't reach Home Assistant yet. Update and restart the add-on."
            )
        return self.async_show_menu(
            step_id="devices",
            menu_options=opts,
            description_placeholders={"servers": ", ".join(s["name"] for s in m["servers"]) or "none yet"},
        )

    async def async_step_ha_link(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect a Docker gateway to this Home Assistant."""
        if user_input is not None:
            try:
                await self._api(
                    "PUT", "mcp/settings", json={"ha_url": user_input["ha_url"], "ha_token": user_input["ha_token"]}
                )
                m = await self._api("GET", "mcp")
                if m["ha_connected"]:
                    await self._api("GET", "mcp/catalog")
            except GatewayError as err:
                return self._fail(err)
            return self._done("Connected. Open Configure > Devices again to make an MCP server.")
        try:
            here = get_url(self.hass, prefer_external=False)
        except NoURLAvailableError:
            here = ""
        return self.async_show_form(
            step_id="ha_link",
            data_schema=vol.Schema(
                {vol.Required("ha_url", default=here): URL, vol.Required("ha_token"): PASSWORD}
            ),
        )

    async def async_step_new_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name a new MCP server, then pick its devices."""
        if user_input is not None:
            try:
                srv = await self._api("POST", "mcp/servers", json={"name": user_input["name"]})
            except GatewayError as err:
                return self._fail(err)
            self.server_id = srv["id"]
            return await self.async_step_server_devices()
        return self.async_show_form(step_id="new_server", data_schema=vol.Schema({vol.Required("name"): TEXT}))

    async def async_step_edit_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick which MCP server to change."""
        m = await self._api("GET", "mcp")
        if user_input is not None:
            self.server_id = user_input["server"]
            return await self.async_step_server_devices()
        return self.async_show_form(
            step_id="edit_server",
            data_schema=vol.Schema({vol.Required("server"): _select([(s["id"], s["name"]) for s in m["servers"]])}),
        )

    async def async_step_server_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose devices and entities: can see, or can see and control."""
        m = await self._api("GET", "mcp")
        srv = next((s for s in m["servers"] if s["id"] == self.server_id), None)
        if not srv:
            return self._done("That MCP server is gone.")
        if user_input is not None:
            devices = {d: "read" for d in user_input.get("see_devices", [])}
            devices |= {d: "control" for d in user_input.get("control_devices", [])}
            entities = {e: "read" for e in user_input.get("see_entities", [])}
            entities |= {e: "control" for e in user_input.get("control_entities", [])}
            try:
                srv = await self._api(
                    "POST",
                    "mcp/servers",
                    json={
                        "id": srv["id"],
                        "name": user_input.get("name") or srv["name"],
                        "enabled": user_input.get("enabled", True),
                        "devices": devices,
                        "entities": entities,
                    },
                )
            except GatewayError as err:
                return self._fail(err)
            url = f"{self.gw.host}/mcp/{srv['key']}" if self.gw else ""
            return self._done(
                f"Saved {srv['name']}: {len(devices)} devices and {len(entities)} single entities.\n\n"
                f"MCP address (keep it private):\n\n`{url}`\n\n"
                "Paste it into any app that supports MCP (Streamable HTTP)."
            )

        def pick(group: str, mode: str) -> list[str]:
            return [k for k, v in srv[group].items() if v == mode]

        many = {"multiple": True}
        return self.async_show_form(
            step_id="server_devices",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=srv["name"]): TEXT,
                    vol.Required("enabled", default=srv.get("enabled", True)): BooleanSelector(),
                    vol.Optional("control_devices", default=pick("devices", "control")): DeviceSelector(
                        DeviceSelectorConfig(**many)
                    ),
                    vol.Optional("see_devices", default=pick("devices", "read")): DeviceSelector(
                        DeviceSelectorConfig(**many)
                    ),
                    vol.Optional("control_entities", default=pick("entities", "control")): EntitySelector(
                        EntitySelectorConfig(**many)
                    ),
                    vol.Optional("see_entities", default=pick("entities", "read")): EntitySelector(
                        EntitySelectorConfig(**many)
                    ),
                }
            ),
            description_placeholders={"name": srv["name"]},
        )

    async def async_step_ai_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which device list the signed-in Claude / ChatGPT / Gemini accounts may use."""
        m = await self._api("GET", "mcp")
        if user_input is not None:
            choice = "" if user_input["server"] == "none" else user_input["server"]
            try:
                await self._api("PUT", "mcp/settings", json={"ai_server": choice})
            except GatewayError as err:
                return self._fail(err)
            return self._done("Saved.")
        return self.async_show_form(
            step_id="ai_server",
            data_schema=vol.Schema(
                {
                    vol.Required("server", default=m["ai_server"] or "none"): _select(
                        [("none", "No device access")] + [(s["id"], s["name"]) for s in m["servers"]]
                    )
                }
            ),
        )

    # ------------------------------------------------------------ models
    async def async_step_download_model(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Download an Ollama model onto the gateway's device."""
        if user_input is not None:
            try:
                await self._api("POST", "models/pull", json={"name": user_input["model"]})
            except GatewayError as err:
                return self._fail(err)
            return self._done(
                f"Downloading {user_input['model']}. Watch it in the gateway page (Models > Downloads); "
                f"it'll be ollama/{user_input['model']} when done."
            )
        return self.async_show_form(step_id="download_model", data_schema=vol.Schema({vol.Required("model"): TEXT}))
