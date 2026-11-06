# Changelog

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
