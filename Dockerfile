FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never PLAYWRIGHT_BROWSERS_PATH=/opt/agora-browsers PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && uv sync --locked --no-dev --no-install-project --no-cache \
 && /app/.venv/bin/python -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /opt/agora-browsers \
 && rm -rf /var/lib/apt/lists/* /root/.cache
COPY agora_hub ./agora_hub
COPY scripts/browser-entrypoint.py /app/browser-entrypoint.py
EXPOSE 3333 8888
ENTRYPOINT ["/app/.venv/bin/python", "/app/browser-entrypoint.py"]
CMD ["/app/.venv/bin/python", "-m", "agora_hub", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "3333", "--gateway-port", "8888"]
