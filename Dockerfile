FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never PLAYWRIGHT_BROWSERS_PATH=/opt/agora-browsers PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin agora \
 && uv sync --locked --no-dev --no-install-project --no-cache \
 && /app/.venv/bin/python -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /opt/agora-browsers \
 && rm -rf /var/lib/apt/lists/* /root/.cache
COPY agora_hub ./agora_hub
# The hub runs as root; Playwright's driver and Chromium run as agora (scripts/browser-driver.sh). UID 10001, not 1000:
# earlier images gave the files in /config to UID 1000. Set after the build-time install, which runs the driver as root.
COPY --chmod=755 scripts/browser-driver.sh /app/browser-driver.sh
ENV PLAYWRIGHT_NODEJS_PATH=/app/browser-driver.sh
EXPOSE 3333 8888
CMD ["/app/.venv/bin/python", "-m", "agora_hub", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "3333", "--gateway-port", "8888"]
