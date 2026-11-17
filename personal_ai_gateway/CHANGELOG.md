# Changelog

## 1.10.0 - 2026-11-16

- **Pick the web UI wallpaper.** New **Wallpaper** option on the Configuration tab. Leave it empty to use your Home Assistant theme's wallpaper, enter `none` for no wallpaper, or point it at your own image: drop it in `/config/www` and enter the file name (e.g. `wallpaper.jpg`), `/local/wallpaper.jpg`, or a full `https://` link.

## 1.9.0 - 2026-11-16

- **Follows your Home Assistant theme.** The web UI now uses the colors of whatever theme Home Assistant is showing, light or dark, and changes live when you switch. Themes with a dashboard wallpaper and frosted-glass cards (like Apple / Liquid Glass themes) carry over too: wallpaper behind, see-through blurred cards, matching corners. Opened outside Home Assistant it keeps its own look.

## 1.8.0

- **MCP servers for anything, not just devices.** The Devices tab is now **MCP** and makes four kinds of server:
  - **Computer (SSH):** reach another Pi, your laptop, a NAS or Home Assistant's SSH add-on. Look only (system info, Docker containers, list/read/search files), read & write files, or full control (run commands with a timeout). Optional folder limits that symlinks and `..` can't escape.
  - **Command:** run any stdio MCP server (`npx`, `uvx`, `docker run -i`, your own script) inside the gateway as a limited user or on one of your computers over SSH.
  - **Remote MCP URL:** wrap another MCP server (JSON or SSE replies, sessions renewed automatically) behind the gateway's address.
- **Computers (SSH):** the gateway makes its own SSH key; add it with *Set up with password* (password not saved) or by hand. Test, edit, forget fingerprint.
- Hide any tool of any server; **Save & test** shows its tools and error log.
- Signed-in Claude / ChatGPT / Gemini accounts can now use **several** MCP servers at once.
- Image: adds `openssh-client`, `sshpass` and `uv`/`uvx`.

## 1.7.0

- **Sign in with an account, no API key.** Providers → *Sign in with an account*: install the official Claude Code, Codex (ChatGPT) or Gemini CLI tool with one button, sign in from the page (open the link, paste the code), and use `claude-account/sonnet`, `chatgpt-account/default`, `gemini-account/gemini-2.5-pro`... like any other model, in Home Assistant too.
- **Devices tab: MCP servers for the devices you choose.** Make one or more MCP servers, pick which devices or entities each can see or control, and paste its address into any MCP app. Tools: list devices, state, history, available actions, and controlling one allowed entity at a time.
- The signed-in accounts can use one of those MCP servers, so Claude, ChatGPT or Gemini can control the devices you chose.
- The account tools run as a separate user that can't read the gateway's keys.
- The add-on now has Home Assistant API access (needed for Devices). Docker installs use `HA_URL` + `HA_TOKEN`.
- Fixed the Chat tab not opening.

## 1.1.2

- Docker Compose: Ollama is now part of the stack by default (no profile, no URL to set), the gateway waits for it to be healthy, and the Ollama address stays changeable in the page. Previously the gateway defaulted to an Ollama on the host that usually wasn't reachable, so downloads failed.
- `.env` is optional; example file trimmed to what matters.

## 1.1.1

- Fixed downloads stuck at 99%: the built-in Ollama now writes straight to the add-on log (no pipe that could fill up and freeze it).
- Stalled downloads are detected after 3 minutes and resumed automatically (up to 3 times); the page shows Ollama's current step and how long ago it last reported.
- Checks free disk space before and during a download and stops with a clear message if there isn't enough. Free space is shown on the Installed tab.

## 1.1.0

- **Built-in Ollama in the add-on** (CPU): no separate device needed. Starts with the add-on, models kept in `/data/ollama` and excluded from backups. Options: `builtin_ollama`, `ollama_keep_alive`; optional host port for `11434/tcp`.
- The add-on image is now Ubuntu-based (Ollama needs glibc).

## 1.0.1

- Provider model listing now shows (and logs) why it failed instead of an empty list; failed listings are retried after 20 s instead of cached for 5 minutes.
- "Extra models" per provider, and a model box for the Test button, so a provider works even when listing is blocked.
- Ollama address can be set and tested on the Settings page (the add-on option is only a starting value; it no longer locks the field).
- Downloads check Ollama first and explain how to fix an unreachable address; failures are written to the log.

## 1.0.0

- First release: OpenAI-compatible gateway with provider presets, multi-key rotation with escalating cooldowns, fallback routes, Ollama management with model search (Ollama library + Hugging Face) and live downloads, custom HTTP providers, chat playground, phone-friendly management page.
- Runs as a Home Assistant add-on (ingress) or with Docker Compose.
