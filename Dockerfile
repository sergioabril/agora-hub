FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY agora_hub ./agora_hub
EXPOSE 3333
CMD ["/app/.venv/bin/python", "-m", "agora_hub", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "3333"]
