# Personal AI Gateway

One OpenAI-compatible address for every AI you use: your cloud keys, **your Claude / ChatGPT / Gemini accounts (no API key)**, and (optionally) an Ollama you run. Comes with **Open WebUI** as its chat app. Plus **MCP servers** that let AI apps use only what you allow: the Home Assistant devices you pick, **your other computers over SSH** (a second Pi, your laptop...), and any MCP tool you run.

```
apps (Open WebUI, Personal AI in Home Assistant, SillyTavern...) → Gateway → Gemini / OpenAI / Groq / OpenRouter / Claude account ... / your Ollama (optional)
```

## What it does

- **Key rotation and failover.** Add several keys per provider. A key that hits a rate limit rests (10 s, then 30, 60, 120), a key the provider rejects is parked for an hour, and the next key answers. Balanced or sequential mode.
- **Routes (fallback chains).** Make a model name of your own, e.g. `smart`, that tries `gemini/gemini-2.0-flash`, then `groq/llama-3.3-70b-versatile`, then `openrouter/...`.
- **Your own Ollama (optional).** Ollama is no longer built in. If you run one somewhere, put its address in Settings and a **Models** tab appears that manages it: see what's installed, free memory, delete, **search the Ollama library and Hugging Face (GGUF)**, pick a size or quantisation, and **download with a live progress meter**. List names under *Download on start* to fetch them automatically.
- **Custom HTTP providers.** For a service that isn't OpenAI-compatible but has a real endpoint you may use: describe the request (URL, headers, body template with `{{prompt}}`, `{{messages}}`...) and where the reply text is. The gateway presents it as a normal chat model, streaming included.
- Endpoints: `/v1/chat/completions` (streaming, tools), `/v1/embeddings`, `/v1/images/generations`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/models`.

Models are named `provider/model` (`gemini/gemini-2.0-flash`, `ollama/llama3.1:8b`), or by route name. Local Ollama models also work by bare name.

## Sign in with an account (no API key)

Providers → **Sign in with an account**. Each one uses that company's own official command-line tool, installed here when you press **Install**:

| Account | Tool | What you need |
|---|---|---|
| Claude | Claude Code | a Claude Pro or Max plan |
| ChatGPT | Codex | a ChatGPT Plus, Pro or Team plan |
| Gemini | Gemini CLI | any Google account (free tier) |

1. Press **Install** and wait for *Finished ✓* (about a minute; the tools live in the add-on's data, not in backups).
2. Press **Sign in**. A small window opens: press **Open sign-in page**, sign in and approve, copy the code the page shows you, paste it into the box and press **Send**. (ChatGPT shows a short code to type on its page instead.) Done: the window says *Finished ✓*.
3. The account is now a provider with models such as `claude-account/sonnet`, `claude-account/opus`, `claude-account/haiku`, `chatgpt-account/default`, `gemini-account/gemini-2.5-pro`. Use them in Home Assistant, in the Chat tab, or in a route like any other model.

Good to know:

- Answers count against your plan's normal usage limits, the same as using the app. Use it for yourself, under each company's terms.
- These models answer with text. Home Assistant's own "control devices" tools aren't passed through; instead tick MCP servers under **MCP → Signed-in AIs** and the account can use exactly those (devices, computers, tools).
- Each request starts the tool once, so expect a few seconds more than an API key. At most 2 run at once (`CLI_CONCURRENCY` to change).
- **Update** gets the newest version of the tool. **Sign out** forgets the sign-in.
- The tools run as a separate user that can't read the gateway's keys or settings.

## MCP servers

MCP is the standard way AI apps connect to tools. The **MCP** tab makes MCP servers, each with its own private address. There are four kinds:

| Kind | What the AI gets |
|---|---|
| **Home Assistant devices** | only the devices/entities you pick, each read-only or controllable |
| **Computer (SSH)** | another machine: system info, files, and (if you allow) shell commands |
| **Command (stdio MCP)** | any MCP server that runs as a command, inside the gateway or on one of your computers |
| **Remote MCP URL** | another MCP server's address, wrapped behind this gateway's key |

### Home Assistant devices

1. Type a name (e.g. *Living room*) and press **Create**.
2. For every device (or single entity) choose **Hidden**, **Can see** or **Can control**. A device's choice covers all its entities; set an entity on its own to override it. Press **Save devices**.
3. **Copy address** and paste it into an app that supports MCP, as a "Streamable HTTP" server. The address contains the server's private key, so treat it like a password; **New address** replaces it.

Make as many as you like, e.g. *Everything* for yourself and *Lights only* for something else. **Turn off** stops one without deleting it.

Tools the AI gets: `list_devices`, `get_state`, `get_history` (up to 7 days), `list_actions`, and `call_action` (only for *Can control*, and only on that one entity; it can't aim an action at other devices or areas). The last actions (device actions, commands, file writes, tool calls) are listed at the bottom of the tab.

### Computers (SSH): reach your other Pi, your laptop...

1. **MCP → Computers (SSH) → Add a computer:** a name, its address (LAN IP like `192.168.1.20`, or a Tailscale name so it works away from home), user name and SSH port. SSH must be on there (Raspberry Pi OS: `sudo raspi-config` → Interface → SSH; Ubuntu/Debian laptop: `sudo apt install openssh-server`).
2. Press **Set up with password**, type that user's password once. The gateway adds its own key to the computer's `~/.ssh/authorized_keys`; the password isn't saved. (Or copy the command under *The gateway's SSH key* and run it on the computer.) **Test** should say *Connected ✓*.
3. **New MCP server → Computer (SSH)**, then **Settings & tools**: pick the computer and what the AI may do:
   - **Look only:** `system_info` (CPU, temperature, memory, disks, busiest processes, Docker containers), `list_files`, `read_file`, `find_files`.
   - **Read & write files:** plus `write_file`.
   - **Full control:** plus `run_command` (any shell command as that user, with a timeout, e.g. restart a container, check logs, update).
   - **Only these folders** (optional, one per line, e.g. `~/docker`) limits the file tools; symlinks and `..` can't escape it. With *Full control* a command can still go anywhere the user can, so use a user with limited rights if that matters.
4. Untick any tool you don't want under *Tools the AI gets*, **Save tools**, then copy the address like any other server.

Home Assistant OS itself: install the *Advanced SSH & Web Terminal* add-on, paste the gateway's key into its `authorized_keys` option, and add a computer with its user and port. If a computer is reinstalled, press **Forget fingerprint** once.

### Command: any MCP server you can run

Paste the command that starts it, for example `npx -y @modelcontextprotocol/server-memory`, `uvx mcp-server-git`, or `python3 ~/my_mcp.py`, and choose where it runs:

- **Inside the gateway:** runs as a limited user that can't read the gateway's keys; Node.js (`npx`) and `uv`/`uvx` are included. It starts on first use and keeps running; it's restarted if it stops or you change it.
- **On a computer:** runs over SSH with that user's normal PATH, so it can use anything installed there (Docker, files on that machine...).

Add environment variables (tokens) one `NAME=value` per line. **Save & test** starts it and lists its tools; untick the ones to hide. The *Server log* shows its error output if it won't start.

### Remote MCP URL

An MCP server reachable over HTTP (Streamable HTTP), with optional headers such as `Authorization: Bearer ...`. Useful to give all your MCP tools one place and one kind of key. Servers that need a browser sign-in (OAuth) can't be wrapped.

### Using the addresses

- **Signed-in AIs:** tick servers here (as many as you like) and the Claude, ChatGPT and Gemini accounts use them automatically, including when you talk to them through Home Assistant.
- **From another app on your network:** use the address as shown (`http://<home-assistant-ip>:8000/mcp/<key>`). The key can also be sent as `Authorization: Bearer <key>` to `http://<host>:8000/mcp`.
- **From the internet / apps that need https** (claude.ai and ChatGPT connectors only accept `https://`): use **HTTPS address for your MCP servers** at the top of the MCP tab. Once it's on, every server card shows its https address to copy.
  - **Instant tunnel:** one tap, no account. A free Cloudflare quick tunnel gives an `https://<words>.trycloudflare.com` address. It changes every time the add-on restarts, so re-paste it into your app after a restart.
  - **My Cloudflare tunnel:** a fixed address on your own domain. In Cloudflare Zero Trust → Networks → Tunnels make a tunnel, paste its token here, and give it a public hostname with service `http://localhost:8098`.
  - **My own HTTPS:** you already have https in front of the gateway (Nginx Proxy Manager, DuckDNS, Tailscale Funnel…) pointing at port 8000. Enter that address so the cards show it.
  - Only `/mcp/<key>` goes through the tunnel (it talks to a separate MCP-only listener). The gateway page and the `/v1` API stay on your network.
- **Plain Docker install:** the add-on connects to Home Assistant by itself. In Docker, set `HA_URL` and `HA_TOKEN` (a long-lived access token from your HA profile → Security), or enter them on the MCP tab (only needed for device servers).

## Chat: Open WebUI

The **Chat** tab opens **Open WebUI**, a ChatGPT-style app with chat history, file uploads and a model picker showing every model and route in the gateway.

- **Home Assistant:** install the **Open WebUI (AI Gateway chat)** add-on from this same repository and start it. It finds the gateway by itself (the gateway leaves its address and a dedicated key in `/share/personal_ai_gateway`), so there's nothing to copy, and changing the proxy key doesn't break it. Open it from the Chat tab or the add-on's *Open Web UI* button (port 3000). The first account you create is the admin.
- **Docker:** the compose file runs it next to the gateway on port 3000 (set `PROXY_API_KEY` in `.env`).
- It needs a fair bit of RAM (around 1 GB) and a few GB of disk; a Pi 4 with 4 GB+ is fine.
- No Open WebUI? The Chat tab keeps a quick test chat.

## Connect Personal AI (Home Assistant)

Settings → Devices & Services → Personal AI → add a server:

| Field | Value |
|---|---|
| Server URL | `http://<host>:8000/v1`, or with the add-on `http://local-personal-ai-gateway:8000/v1` |
| API key | the proxy key (page → Overview → Copy key) |

Then add a conversation agent and pick any model from the list. Personal AI 1.4 also adds two actions that work with a gateway: `personal_ai.search_models` and `personal_ai.pull_model` (they use the server's API key as the admin password, so they don't work if you set a separate `ADMIN_PASSWORD`).

## Install

**Home Assistant add-on:** add the repository, install *Personal AI Gateway*, start it, open it from the sidebar. Add a provider key or sign in with an account on **Providers**. For chat, also install **Open WebUI (AI Gateway chat)** from the same repository.

**Docker Compose** (any machine): gateway + Open WebUI.

```bash
cd personal_ai_gateway
cp .env.example .env        # set PROXY_API_KEY, add API keys
docker compose up -d
```

Gateway on `http://<host>:8000` (sign in with the proxy key), chat on `http://<host>:3000`. Update with `docker compose pull && docker compose up -d`.

**Upgrading from 1.x:** Ollama is no longer built in. On first start 2.0 deletes the old built-in Ollama's model folder (`/data/ollama`) to free the space. If you want Ollama, run it anywhere (another PC, a NAS, `ollama/ollama` in Docker) and put its address in Settings.

## Signing in

Outside Home Assistant (Docker, or the add-on's port 8000) the page asks for the **admin password**: your `ADMIN_PASSWORD`, or the proxy key if you didn't set one. Leave **Keep me signed in** on and that phone or computer stays signed in for 90 days (renewed while you use it). Sign out from Settings → *Signed in*; **Sign out everywhere** signs out every device. Inside Home Assistant there's no sign-in.

On a phone, use Share → **Add to Home Screen** and it opens like an app.

## Settings by environment / add-on options

`PROXY_API_KEY`, `ADMIN_PASSWORD`, `OLLAMA_URL` (optional), `OLLAMA_PRELOAD`, `OPEN_WEBUI_PORT` (Docker), `HA_URL`, `HA_TOKEN` (Docker only, for device MCP servers), `CLI_CONCURRENCY`, and `<PROVIDER>_API_KEY_<N>` (e.g. `GEMINI_API_KEY_2`). Known provider names are created automatically; add any other OpenAI-compatible service from the Providers tab.

## Troubleshooting

- **Account sign-in window shows an error:** read the text in the window; press **Sign in again** to start over. "Not logged in" or "authentication" errors in chat mean the sign-in expired: sign in again.
- **An account model answers slowly:** normal for the first request after a restart (the tool loads). Big Home Assistant prompts (many exposed entities) also take longer.
- **A computer says it refused the key:** press **Set up with password** again, or check the user name. *Fingerprint changed:* **Forget fingerprint**.
- **A Command server won't start:** open **Settings & tools → Test** and read the *Server log*; usually a typo or a missing program on that machine.
- **MCP tab says Home Assistant isn't connected:** update the add-on (1.7.0 adds Home Assistant API access) and restart it. In Docker, check `HA_URL` and `HA_TOKEN`.
- **An MCP app says 401:** the address is wrong, was replaced with **New address**, or the server is turned off.

- **"Can't reach Ollama":** the address must be that machine's LAN address (not localhost), and Ollama must listen on the network (`OLLAMA_HOST=0.0.0.0`). Use Settings → Test connection.
- **HTTPS address stuck on "Starting" or erroring:** open *Tunnel log* on the MCP tab. A quick tunnel needs outbound internet; a token tunnel needs a valid token and a public hostname pointing at `http://localhost:8098`.
- **Claude says it can't connect to the MCP server:** use the https address (not the http one), and after an add-on restart re-copy it if you use the instant tunnel.
- **Open WebUI shows no models:** start the gateway first, then restart Open WebUI. Its log says which gateway address it found.
- **A provider lists no models:** press *List models* on its card; the exact reason (bad key, HTTP 403, network) is shown, and written to the add-on log. Chat can still work: use the full name, e.g. `gemini/gemini-2.5-flash`, or add names under *Extra models* so apps' model pickers show them.
- **Test** with a model name typed in the box if listing is blocked for your key.

## Good to know

- Rotating several keys is meant for keys you own (several projects, a paid and a free key). Don't use it to get around a provider's per-person limits or terms.
- Sites without an API (for example Perchance generators) can't be used: the only ways in are driving a hidden browser and defeating the site's bot checks, which this gateway does not do. Use a provider that offers an API; many have free tiers (Gemini, Groq, OpenRouter free models, Cerebras) and rotate well here.
- Model search needs internet access from the gateway container. The Ollama library page has no official API, so if its layout changes that search may come back empty; Hugging Face search and "download by exact name" keep working.
- Hugging Face models are pulled by Ollama as `hf.co/user/repo:QUANT`. Check the size shown against your free RAM/VRAM (roughly file size plus 20%).
- Keep the port on your LAN; anyone with the proxy key can spend your provider keys.
