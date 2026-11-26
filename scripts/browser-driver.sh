#!/bin/sh
# Playwright starts its Node.js driver through this script (PLAYWRIGHT_NODEJS_PATH in the Dockerfile), and the driver
# starts Chromium. Both run as the unprivileged user agora while the hub stays root: Chromium keeps its sandbox, which
# it refuses as root, and a browser that escaped it still can't read /config. The path follows the Dockerfile's Python.
exec env HOME=/home/agora USER=agora LOGNAME=agora \
  setpriv --reuid=agora --regid=agora --init-groups \
  /app/.venv/lib/python3.12/site-packages/playwright/driver/node "$@"
