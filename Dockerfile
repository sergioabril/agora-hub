FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY agora_hub ./agora_hub
RUN pip install --no-cache-dir .
EXPOSE 3333
CMD ["python", "-m", "agora_hub", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "3333"]
