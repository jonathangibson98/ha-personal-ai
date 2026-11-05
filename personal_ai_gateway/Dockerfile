# Gateway + a built-in Ollama, so no second machine or container is needed.
# Ollama's binary needs glibc, so this uses Ubuntu rather than the Alpine-based Home Assistant base.
FROM ollama/ollama:latest AS ollama
# Add-ons have no GPU access, so drop the large GPU libraries and keep the CPU ones.
RUN rm -rf /usr/lib/ollama/cuda_* /usr/lib/ollama/rocm /usr/lib/ollama/vulkan

FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-aiohttp ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PORT=8000
CMD [ "/run.sh" ]
