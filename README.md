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

Docker (the only supported way to run the image locally):

```sh
make deploy
```

`make deploy` creates `~/.config/agora/agora-hub/` (and any missing parent folder) and adds only the files that are missing; it never overwrites existing ones:

- `hub.yaml`: a copy of `examples/hub.yaml` with an empty `api.admin_token` (no authentication, see below), no scheduled machines and no startup deployment
- `machines.yaml`: `machines: {}`
- `deployments.yaml`: `deployments: {}`
- `known_hosts`: empty

It then builds the image and starts the hub in the background on port `3333`, with the config folder mounted writable so the add and update endpoints can save machines. The container uses `--restart unless-stopped`, so it comes back after a crash, a Docker restart or a reboot (on macOS, enable Docker Desktop's "Start when you sign in"). Run `make deploy` again to rebuild and replace it; nothing in the config folder is overwritten. Use `make deploy CONFIG_DIR=/other/path` for a different folder.

> **Security: a fresh deploy has no authentication.** `api.admin_token` starts empty, and while it is empty the hub accepts every request, with any bearer token or none. `make deploy` publishes port `3333` on all network interfaces, so anyone who can reach this machine can power machines on and off, start and stop deployments, and add or edit machines. Set a token before using the hub on a shared network: put `openssl rand -hex 32` output in `api.admin_token` in `hub.yaml`, run `docker restart agora-hub`, and give the same value to agora-dashboard (`AGORA_HUB_ADMIN_TOKEN`).

After the first deploy, replace `home_assistant.token` in `hub.yaml` with your Home Assistant token, add host keys with `ssh-keyscan -H <host> >> ~/.config/agora/agora-hub/known_hosts`, and add machines through `POST /admin/machines/add`. `known_hosts` is read on every SSH connection; after editing a YAML file by hand, run `docker restart agora-hub`.

Logs: `docker logs -f agora-hub`. Undeploy: `docker rm -f agora-hub`.

## 3. Config files

`hub.yaml` (secrets live in the YAML files, like the machine passwords: `api.admin_token` is empty (no authentication) or 64 hex characters from `openssl rand -hex 32`, which every request must then send as `Authorization: Bearer <token>`; `home_assistant.token` is a Home Assistant long-lived access token):

```yaml
api: {host: 127.0.0.1, port: 3333, admin_token: ""}
ssh: {known_hosts_path: known_hosts}
home_assistant:
  base_url: http://homeassistant.local:8123
  token: REPLACE_LOCALLY
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

`machines.yaml` (each key is the machine id: ids must be unique, the hub generates them for machines added through the API, and `add` is reserved; `name` is required and is the label people see; defaults: port `22`, `key_path: null`, `shutdown_command: "sudo -S -p '' poweroff"`, and `device: dgx-spark`; configure either `password` or `key_path`; `device` is one of `dgx-spark`, `rtx6000`, `mac-studio` and only affects how agora-dashboard draws the machine):

```yaml
machines:
  spark-1:
    name: Spark 1
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

For a native run, set up the owner-only configuration directory and required `known_hosts` file by hand (`make deploy` does this for Docker), then set `home_assistant.token` (and, to enforce authentication, `api.admin_token`) in `hub.yaml`:

```sh
CONFIG_DIR="$HOME/.config/agora/agora-hub"
mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
cp examples/{hub,machines,deployments}.yaml "$CONFIG_DIR/"
ssh-keyscan -H <host> >> "$CONFIG_DIR/known_hosts"
chmod 600 "$CONFIG_DIR"/{hub.yaml,machines.yaml,deployments.yaml,known_hosts}
```

`known_hosts` and relative SSH key paths are resolved from the configuration directory.

## 4. Endpoint calls

When `api.admin_token` is set, every endpoint requires `Authorization: Bearer <admin-token>` and answers `401` otherwise; when it is empty, no endpoint checks authentication. Add and update apply immediately (no restart) and rewrite `machines.yaml`, which drops its YAML comments.

| Method and path | Body | What it does |
| --- | --- | --- |
| `GET /admin/machines/status` | None | Refreshes machine observations (with each machine's `name`, `host` and `device`) without mutations. |
| `POST /admin/machines/add` | Machine fields (`name`, `host`, `user`, ...) | Adds a machine with a hub-generated 8-hex id; returns `201` with the machine (no passwords), or `422`. |
| `POST /admin/machines/{machine_id}/update` | Any subset of machine fields; `null` resets one to its default | Updates a machine; returns `200`, `404`, `409` (`machine_busy`), or `422`. |
| `POST /admin/machines/{machine_id}` | `{"action":"on"}` or `{"action":"off"}` | Starts an independent machine action; returns `202`, or `409` when blocked. |
| `GET /admin/deployments/status` | None | Refreshes deployment state, readiness, endpoint, member `machines`, `head`, and operation status without mutations. |
| `POST /admin/deployments/{deployment_id}` | `{"action":"start"}` or `{"action":"stop"}` | Starts an independent deployment action; returns `202`, or `409` when blocked. |

```sh
TOKEN=your-64-character-token
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/machines/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"on"}' http://localhost:3333/admin/machines/spark-1
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Spark 5","host":"spark-5.local","user":"agora","password":"...","plug_entity_id":"switch.spark_5"}' \
  http://localhost:3333/admin/machines/add
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"10.0.0.5"}' http://localhost:3333/admin/machines/spark-1/update
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/deployments/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"start"}' http://localhost:3333/admin/deployments/pro
```
