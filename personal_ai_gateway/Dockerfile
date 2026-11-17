# Personal AI Gateway add-on. Ubuntu base: the account sign-in tools (Node) want glibc.
# cloudflared gives the MCP servers an https address (Settings in the MCP tab).
FROM cloudflare/cloudflared:latest AS cloudflared

# Node.js runs the official Claude / ChatGPT (Codex) / Gemini sign-in tools, installed on demand into /data.
FROM node:22-bookworm-slim AS node

FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-aiohttp ca-certificates git ripgrep openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /var/lib/aicli --shell /usr/sbin/nologin aicli
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version
# uv / uvx, for MCP servers written in Python ("Command" MCP servers).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PORT=8000
CMD [ "/run.sh" ]
