# Agora Hub

## 1. Intro

Agora Hub is a headless service for observing and controlling configured machines and their model deployment. It keeps runtime state in memory and can apply a daily wake/shutdown schedule.

## 2. Quick install

Clone:

```sh
git clone <repository-url> agora-hub
cd agora-hub
```

Native with `uv`:

```sh
uv sync
uv run python -m agora_hub --config-dir "$HOME/.config/agora/agora-hub"
```

Docker:

```sh
make build
docker run --rm --name agora-hub \
  --publish 3333:3333 \
  --mount type=bind,src="$HOME/.config/agora/agora-hub",dst=/config,readonly \
  agora-hub:local
```

## 3. Config files

`hub.yaml`:

```yaml
api: {host: 127.0.0.1, port: 3333, admin_token_env: AGORA_HUB_ADMIN_TOKEN}
ssh: {known_hosts_path: known_hosts}
home_assistant:
  base_url: http://homeassistant.local:8123
  token_env: HOME_ASSISTANT_TOKEN
  turn_off_path: /api/services/switch/turn_off
  turn_on_path: /api/services/switch/turn_on
  power_cycle_delay_seconds: 5
schedule:
  enabled: true
  timezone: Europe/Madrid
  wake_at: "08:00"
  shutdown_at: "23:00"
  machines: [spark-1]
  startup_deployment: pro
timeouts:
  ssh_connect_seconds: 10
  status_command_seconds: 10
  http_seconds: 10
  machine_boot_seconds: 180
  machine_shutdown_seconds: 120
  deployment_start_seconds: 900
  deployment_stop_seconds: 60
  readiness_poll_seconds: 5
```

`machines.yaml` (defaults: port `22`, `key_path: null`, `shutdown_command: "sudo -S -p '' poweroff"`, and `device: dgx-spark`; configure either `password` or `key_path`; `device` is one of `dgx-spark`, `rtx6000`, `mac-studio` and only affects how agora-dashboard draws the machine):

```yaml
machines:
  spark-1:
    host: spark-1.local
    user: agora
    password: REPLACE_LOCALLY
    sudo_password: REPLACE_LOCALLY
    plug_entity_id: switch.spark_1
    device: dgx-spark
```

`deployments.yaml`:

```yaml
deployments:
  pro:
    name: Pro
    machines: [spark-1]
    head: spark-1
    working_directory: /home/agora/pro
    start_command: bash start.sh
    stop_command: bash stop.sh
    status_command: bash status.sh
    endpoint: http://spark-1.local:8000
    readiness: {path: /v1/models, expected_status: 200}
```

`.env`:

```dotenv
AGORA_HUB_ADMIN_TOKEN=64_HEX_CHARACTERS
HOME_ASSISTANT_TOKEN=REPLACE_LOCALLY
```

Set up the owner-only configuration directory and required `known_hosts` file:

```sh
CONFIG_DIR="$HOME/.config/agora/agora-hub"
mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
cp examples/{hub,machines,deployments}.yaml "$CONFIG_DIR/"
TOKEN=$(openssl rand -hex 32)
printf 'AGORA_HUB_ADMIN_TOKEN=%s\nHOME_ASSISTANT_TOKEN=%s\n' "$TOKEN" 'REPLACE_LOCALLY' > "$CONFIG_DIR/.env"
ssh-keyscan -H <host> >> "$CONFIG_DIR/known_hosts"
chmod 600 "$CONFIG_DIR"/{hub.yaml,machines.yaml,deployments.yaml,.env,known_hosts}
```

`known_hosts` and relative SSH key paths are resolved from the configuration directory.

## 4. Endpoint calls

All endpoints require `Authorization: Bearer <admin-token>`.

| Method and path | Body | What it does |
| --- | --- | --- |
| `GET /admin/machines/status` | None | Refreshes machine observations (with each machine's `host` and `device`) without mutations. |
| `POST /admin/machines/{machine_id}` | `{"action":"on"}` or `{"action":"off"}` | Starts an independent machine action; returns `202`, or `409` when blocked. |
| `GET /admin/deployments/status` | None | Refreshes deployment state, readiness, endpoint, member `machines`, `head`, and operation status without mutations. |
| `POST /admin/deployments/{deployment_id}` | `{"action":"start"}` or `{"action":"stop"}` | Starts an independent deployment action; returns `202`, or `409` when blocked. |

```sh
TOKEN=your-64-character-token
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/machines/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"on"}' http://localhost:3333/admin/machines/spark-1
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/deployments/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"start"}' http://localhost:3333/admin/deployments/pro
```
