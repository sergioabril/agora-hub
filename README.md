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

It then builds the image and starts the hub in the background on port `3333`, with the config folder mounted writable so the add and update endpoints can save machines and deployments. The container uses `--restart unless-stopped`, so it comes back after a crash, a Docker restart or a reboot (on macOS, enable Docker Desktop's "Start when you sign in"). Run `make deploy` again to rebuild and replace it; nothing in the config folder is overwritten. Use `make deploy CONFIG_DIR=/other/path` for a different folder.

> **Security: a fresh deploy has no authentication.** `api.admin_token` starts empty, and while it is empty the hub accepts every request, with any bearer token or none. `make deploy` publishes port `3333` on all network interfaces, so anyone who can reach this machine can power machines on and off, start and stop deployments, and add or edit machines. Set a token before using the hub on a shared network: put `openssl rand -hex 32` output in `api.admin_token` in `hub.yaml`, run `docker restart agora-hub`, and give the same value to agora-dashboard (`AGORA_HUB_ADMIN_TOKEN`).

After the first deploy, set Home Assistant's `host`, `port` and long-lived access `token` through `POST /admin/integrations/homeassistant`, then add machines through `POST /admin/machines/add`, then deployments through `POST /admin/deployments/add` (see §4). After editing a YAML file by hand, run `docker restart agora-hub`.

```sh
curl -X POST -H 'Content-Type: application/json' -d '{"host":"192.168.1.10","port":8123,"token":"..."}' http://localhost:3333/admin/integrations/homeassistant
```

SSH host keys are trusted on first use, like `ssh -o StrictHostKeyChecking=accept-new`: the first time the hub connects to a host that isn't in `known_hosts`, it saves that host's key there. From then on, the host must keep presenting a saved key, or the hub refuses to connect and reports a `host_key` error, so a machine impersonating it can't collect the SSH or sudo password. After a legitimate key change (a reinstall), remove the old entry with `ssh-keygen -R <host> -f ~/.config/agora/agora-hub/known_hosts`; the next connection saves the new key. `known_hosts` is read on every SSH connection, so no restart is needed.

Logs: `docker logs -f agora-hub`. Undeploy: `docker rm -f agora-hub`.

## 3. Config files

`hub.yaml` (secrets live in the YAML files, like the machine passwords: `api.admin_token` is empty (no authentication) or 64 hex characters from `openssl rand -hex 32`, which every request must then send as `Authorization: Bearer <token>`; `home_assistant.token` is a Home Assistant long-lived access token):

```yaml
api: {host: 127.0.0.1, port: 3333, admin_token: ""}
ssh: {known_hosts_path: known_hosts}
home_assistant:
  host: homeassistant.local
  port: 8123
  token: ""
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
  http_seconds: 10
  machine_boot_seconds: 180
  machine_shutdown_seconds: 120
  deployment_start_seconds: 900
  deployment_stop_seconds: 60
  readiness_poll_seconds: 5
```

The hub calls Home Assistant at plain `http://<host>:<port>` (`host` is an IP or hostname, without scheme). An empty `token` means Home Assistant is not configured: the switch endpoints answer `409` and waking a machine fails.

`machines.yaml` (each key is the machine id: ids must be unique, the hub generates them for machines added through the API, and `add` and `test` are reserved; `name` is required and is the label people see; defaults: port `22`, `key_path: null`, `shutdown_command: "sudo -S -p '' poweroff"`, and `device: dgx-spark`; configure either `password` or `key_path`; `device` is one of `dgx-spark`, `rtx6000`, `mac-studio` and only affects how agora-dashboard draws the machine):

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
    machines: [spark-1, spark-2, spark-3, spark-4]
    head: spark-1
    start_command: /home/agora/pro/start.sh
    stop_command: /home/agora/pro/stop.sh
    endpoint: http://spark-1.local:8000/v1/models
```

Each key is the deployment id (hub-generated for deployments added through the API; `add` is reserved). `start_command` and `stop_command` start with an absolute path and may take arguments (`/home/agora/pro/start.sh --tp 4`). They run on `head` from the SSH user's home with no `cd` (a script that needs its own folder does `cd "$(dirname "$0")"`), with stdin closed. The start script must exit after launching the model; after it exits 0, the hub waits for `endpoint` to answer 2xx within `deployment_start_seconds`. `endpoint` is the full URL the hub probes for status: any 2xx means the deployment is up.

For a native run, set up the owner-only configuration directory and required `known_hosts` file by hand (`make deploy` does this for Docker), then set `api.admin_token` in `hub.yaml` to enforce authentication, and set up Home Assistant through `POST /admin/integrations/homeassistant` once the hub runs:

```sh
CONFIG_DIR="$HOME/.config/agora/agora-hub"
mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
cp examples/{hub,machines,deployments}.yaml "$CONFIG_DIR/"
touch "$CONFIG_DIR/known_hosts"
chmod 600 "$CONFIG_DIR"/{hub.yaml,machines.yaml,deployments.yaml,known_hosts}
```

`known_hosts` and relative SSH key paths are resolved from the configuration directory.

## 4. Endpoint calls

When `api.admin_token` is set, every endpoint requires `Authorization: Bearer <admin-token>` and answers `401` otherwise; when it is empty, no endpoint checks authentication. Add and update apply immediately (no restart) and rewrite `machines.yaml` or `deployments.yaml`, which drops its YAML comments; saving the Home Assistant settings likewise rewrites `hub.yaml` and drops its comments.

| Method and path | Body | What it does |
| --- | --- | --- |
| `GET /admin/machines/status` | None | Refreshes machine observations (with each machine's `name`, `host`, `user`, `port`, `key_path`, `plug_entity_id`, `shutdown_command` and `device`; never passwords) without mutations. |
| `POST /admin/machines/add` | Machine fields (`name`, `host`, `user`, ...) | Adds a machine with a hub-generated 8-hex id; returns `201` with the machine (no passwords), or `422`. |
| `POST /admin/machines/test` | Machine fields, as for add | Validates them like add, then tries an SSH login without saving the machine; returns `200` with `{"reachable", "error"}` (`error` is `null` when the login works, otherwise `auth: ...`, `host_key: ...` or `connect: ...`), or `422`. Like any first connection, it saves a new host's key to `known_hosts`. |
| `POST /admin/machines/{machine_id}/update` | Any subset of machine fields; `null` resets one to its default | Updates a machine; returns `200`, `404`, `409` (`machine_busy`), or `422`. |
| `POST /admin/machines/{machine_id}` | `{"action":"on"}` or `{"action":"off"}` | Starts an independent machine action; returns `202`, or `409` when blocked. `{"action":"off"}` answers `409 deployment_active` while the machine runs, starts or stops a deployment. |
| `GET /admin/deployments/status` | None | Probes each `endpoint` without mutations and returns the deployment fields (`name`, `machines`, `head`, `start_command`, `stop_command`, `endpoint`), `observed_state` (`starting`/`stopping` while an operation runs, otherwise `running` when the endpoint answers 2xx, else `stopped`), `ready`, `error`, `checked_at` and `operation`. |
| `POST /admin/deployments/add` | All six deployment fields (`name`, `machines`, `head`, `start_command`, `stop_command`, `endpoint`) | Adds a deployment with a hub-generated 8-hex id; returns `201` with the deployment (`id` plus the six fields), `422`, or `500`. |
| `POST /admin/deployments/{deployment_id}/update` | Any subset of deployment fields; `null` drops one, which is a `422` since all are required | Updates a deployment; returns `200` with the deployment, `404`, `409` (`deployment_busy` while it has an active operation), `422`, or `500`. |
| `POST /admin/deployments/{deployment_id}` | `{"action":"start"}` or `{"action":"stop"}` | Starts the deployment's start or stop script on `head`; returns `202` with the operation's live NDJSON stream (below), or `404`, `409` (`busy`, `deployment_active`), or `422` as JSON. Closing the stream doesn't affect the operation. |
| `GET /admin/deployments/{deployment_id}/output` | None | Returns `200` with the NDJSON stream of the deployment's current or last operation (a finished one replays its log and ends at once), `404 Deployment not found`, or `404 No operation` when it has had none since the hub started. |
| `GET /admin/integrations/homeassistant` | None | Returns `{"host", "port", "token", "power_cycle_delay_seconds"}` without contacting Home Assistant; `token` is `""` when not configured. It returns the token to any authorized caller. |
| `POST /admin/integrations/homeassistant` | Any subset of `host`, `port`, `token`, `power_cycle_delay_seconds`; `null` resets one to its default, and a `null` or `""` token unsets it | Saves the settings without contacting Home Assistant; returns `200` with the settings view, `422`, or `500`. |
| `POST /admin/integrations/homeassistant/test` | As for POST | Validates the merged settings and calls Home Assistant's `GET /api/` with them, saving nothing; returns `200 {"reachable", "error"}` (`reachable` when Home Assistant answered at all, `error` is `null` when it accepted the token, otherwise `not_configured: ...` or `http: ...`), or `422`. |
| `GET /admin/integrations/homeassistant/switches` | None | Returns `200 {"switches": [{"entity_id", "name", "state", "machines"}]}` (every `switch.*` entity, its Home Assistant state, and the ids of machines whose `plug_entity_id` it is), `409` (`home_assistant_not_configured`), or `502` when Home Assistant fails. |
| `POST /admin/integrations/homeassistant/switches` | `[{"entity_id":"switch.x","state":"on"\|"off"}, ...]` | Turns the switches off, then on, and returns the fresh switch list like GET; `409`, `502`, or `422`. **No guard**: it ignores running operations and machine state, so turning off a machine's plug cuts its power without shutting it down. |

Deployment streams are `application/x-ndjson`, one JSON object per line: first `{"type":"operation","operation":{...}}` (the operation as it is now), then `{"type":"line","source":"hub"|"stdout"|"stderr","text":...,"at":...}` for each buffered and then live line (the hub keeps the last 2,000 lines of each deployment's last operation, in memory), and last `{"type":"end","operation":{...}}` once the operation finishes.

```sh
TOKEN=your-64-character-token
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/machines/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"on"}' http://localhost:3333/admin/machines/spark-1
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Spark 5","host":"spark-5.local","user":"agora","password":"...","plug_entity_id":"switch.spark_5"}' \
  http://localhost:3333/admin/machines/add
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Spark 5","host":"spark-5.local","user":"agora","password":"...","plug_entity_id":"switch.spark_5"}' \
  http://localhost:3333/admin/machines/test
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"10.0.0.5"}' http://localhost:3333/admin/machines/spark-1/update
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/deployments/status
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Pro","machines":["spark-1","spark-2"],"head":"spark-1","start_command":"/home/agora/pro/start.sh","stop_command":"/home/agora/pro/stop.sh","endpoint":"http://spark-1.local:8000/v1/models"}' \
  http://localhost:3333/admin/deployments/add
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"start_command":"/home/agora/pro/start.sh --tp 2"}' http://localhost:3333/admin/deployments/pro/update
curl -N -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"start"}' http://localhost:3333/admin/deployments/pro
curl -N -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/deployments/pro/output
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://localhost:3333/admin/integrations/homeassistant/test
curl -H "Authorization: Bearer $TOKEN" http://localhost:3333/admin/integrations/homeassistant/switches
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '[{"entity_id":"switch.lamp","state":"on"}]' http://localhost:3333/admin/integrations/homeassistant/switches
```
