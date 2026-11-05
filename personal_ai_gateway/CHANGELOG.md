# Changelog

## 1.0.1

- Provider model listing now shows (and logs) why it failed instead of an empty list; failed listings are retried after 20 s instead of cached for 5 minutes.
- "Extra models" per provider, and a model box for the Test button, so a provider works even when listing is blocked.
- Ollama address can be set and tested on the Settings page (the add-on option is only a starting value; it no longer locks the field).
- Downloads check Ollama first and explain how to fix an unreachable address; failures are written to the log.

## 1.0.0

- First release: OpenAI-compatible gateway with provider presets, multi-key rotation with escalating cooldowns, fallback routes, Ollama management with model search (Ollama library + Hugging Face) and live downloads, custom HTTP providers, chat playground, phone-friendly management page.
- Runs as a Home Assistant add-on (ingress) or with Docker Compose.
