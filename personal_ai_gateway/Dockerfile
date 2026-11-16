# Gateway + a built-in Ollama, so no second machine or container is needed.
# Ollama's binary needs glibc, so this uses Ubuntu rather than the Alpine-based Home Assistant base.
FROM ollama/ollama:latest AS ollama
# Add-ons have no GPU access, so drop the large GPU libraries and keep the CPU ones.
RUN rm -rf /usr/lib/ollama/cuda_* /usr/lib/ollama/rocm /usr/lib/ollama/vulkan

# Node.js runs the official Claude / ChatGPT (Codex) / Gemini sign-in tools, installed on demand into /data.
FROM node:22-bookworm-slim AS node

FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-aiohttp ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /var/lib/aicli --shell /usr/sbin/nologin aicli
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version
COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PORT=8000
CMD [ "/run.sh" ]
