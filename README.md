# Agora Hub

## 1. Intro

Agora Hub is a headless service for observing and controlling configured machines and their model deployment. It keeps runtime state in memory and can apply a daily wake/shutdown schedule. The hub also serves an OpenAI-compatible gateway on `:8888` at `/openai/v1`: OpenAI chat completions, as a plain pass-through to the running LLMs, and the OpenAI Responses API, which runs through the hub's harness (vision, image tools, web research, reasoning). The research tools search the web, open pasted URLs, follow links, read tables, inspect images and charts, and publish selected images with source attribution. They try HTTP first and can render JavaScript with the bundled headless Chromium. The model is instructed to finish with an answer and links to pages it actually read; readable extraction alone does not establish correctness.

## 2. Quick install

Clone:

```sh
git clone <repository-url> agora-hub
cd agora-hub
```

Native with `uv`:

```sh
uv sync --locked
# Linux: install the matching headless shell and system libraries (may require sudo).
uv run python -m playwright install --with-deps --only-shell chromium
# On macOS, use: uv run python -m playwright install --only-shell chromium
# Create the configuration directory as described in §3 before starting.
uv run python -m agora_hub --config-dir "$HOME/.config/agora/agora-hub"
```

Docker (complete browser/runtime installation is built into the image):

```sh
make deploy
```

`make deploy` creates `~/.config/agora/agora-hub/` (and any missing parent folder) and adds only the files that are missing; it never overwrites existing ones:

- `hub.yaml`: a copy of `examples/hub.yaml` with an empty `api.admin_token` (no authentication, see below), no scheduled machines and no startup deployment
- `machines.yaml`: `machines: {}`
- `deployments.yaml`: `deployments: {}`
- `known_hosts`: empty

It then builds the image and starts the hub in the background, publishing port `3333` (admin API) and port `8888` (gateway), with the config folder mounted writable so the add and update endpoints can save machines and deployments. The container uses `--restart unless-stopped`, so it comes back after a crash, a Docker restart or a reboot (on macOS, enable Docker Desktop's "Start when you sign in"). Run `make deploy` again to rebuild and replace it; nothing in the config folder is overwritten. Use `make deploy CONFIG_DIR=/other/path` for a different folder.

The Debian Bookworm image locks Python dependencies through `uv.lock` and installs only Playwright's matching Chromium headless shell and Linux libraries at build time into `/opt/agora-browsers`. There is no first-request download, search API key, host Chrome, desktop session, CDP connection, extra service or special host network to configure. Outbound internet is required. Existing `hub.yaml` files without `tools` gain default-on research automatically; an explicit disable remains respected.

The normal launch uses `--init`, `--shm-size=1g` and `scripts/browser-seccomp.json`, which permits Chromium user namespaces alongside the Docker syscall policy ([Playwright Docker guidance](https://playwright.dev/python/docs/docker)). The hub runs as root. Only the browser runs unprivileged: `PLAYWRIGHT_NODEJS_PATH` points Playwright at `scripts/browser-driver.sh`, which starts Playwright's Node.js driver, and with it Chromium, as the image's `agora` user (UID 10001). Chromium keeps its sandbox, which it refuses to use as root. On a Linux host, the `agora` user can't read `/config` (mode 700). Docker Desktop's file sharing doesn't apply Linux permissions to container users, so on macOS the browser's main process could read it, while the sandboxed renderers that run web pages see no files. Custom launches need the same runtime options; keep the browser sandbox enabled.

`make deploy` builds the local image and recreates the container. If you distribute an image separately, pulling it alone does not replace a running container: recreate it with the same mounts and runtime options. Text research is a hub update; readable browsing cards and found images require the updated Ago iOS app separately.

Docker smoke (25 November 2026, Docker Desktop on Apple silicon, linux/arm64): the image is 1.41 GB. The sandboxed headless shell ran as UID 10001 with the seccomp profile, the hub as root. A config folder with files left owned by UID 1000 from an earlier image and a `hub.yaml` without `tools` stayed readable and writable (SSH login and `known_hosts`, SQLite, images), and research tools were offered. The container used about 58 MiB idle and peaked at about 285 MiB while rendering pages. The image installs `libatomic1` before `playwright install`, because Playwright's bundled Node driver needs it and the slim base image lacks it.

> **Security: a fresh deploy has no authentication.** `api.admin_token` starts empty, and while it is empty the hub accepts every request, with any bearer token or none. `make deploy` publishes port `3333` on all network interfaces, so anyone who can reach this machine can power machines on and off, start and stop deployments, and add or edit machines. Set a token before using the hub on a shared network: put `openssl rand -hex 32` output in `api.admin_token` in `hub.yaml`, run `docker restart agora-hub`, and give the same value to agora-dashboard (`AGORA_HUB_ADMIN_TOKEN`). While `api.admin_token` is empty, anyone who can reach port `3333` can also create users, and so get gateway tokens. The gateway on port `8888` always needs a user token (§4 "Accounts"); it has no open mode.

After the first deploy, set Home Assistant's `host`, `port` and long-lived access `token` through `POST /admin/integrations/homeassistant`, then add machines through `POST /admin/machines/add`, then deployments through `POST /admin/deployments/add` (see §4). After editing a YAML file by hand, run `docker restart agora-hub`.

```sh
curl -X POST -H 'Content-Type: application/json' -d '{"host":"192.168.1.10","port":8123,"token":"..."}' http://localhost:3333/admin/integrations/homeassistant
```

SSH host keys are trusted on first use, like `ssh -o StrictHostKeyChecking=accept-new`: the first time the hub connects to a host that isn't in `known_hosts`, it saves that host's key there. From then on, the host must keep presenting a saved key, or the hub refuses to connect and reports a `host_key` error, so a machine impersonating it can't collect the SSH or sudo password. After a legitimate key change (a reinstall), remove the old entry with `ssh-keygen -R <host> -f ~/.config/agora/agora-hub/known_hosts`; the next connection saves the new key. `known_hosts` is read on every SSH connection, so no restart is needed.

Logs: `docker logs -f agora-hub` (the gateway's log is described in §4). Undeploy: `docker rm -f agora-hub`.

## 3. Config files

`hub.yaml` (secrets live in the YAML files, like the machine passwords: `api.admin_token` is empty (no authentication) or 64 hex characters from `openssl rand -hex 32`, which every request must then send as `Authorization: Bearer <token>`; `home_assistant.token` is a Home Assistant long-lived access token):

```yaml
api: {host: 127.0.0.1, port: 3333, admin_token: ""}
gateway: {port: 8888}
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
  machines: [spark-1, spark-2, spark-3, spark-4]
  startup_deployments: [pro, images] # started in this order after the machines wake; [] = none
timeouts:
  ssh_connect_seconds: 10
  http_seconds: 10
  machine_boot_seconds: 180
  machine_shutdown_seconds: 120
  deployment_start_seconds: 900
  deployment_stop_seconds: 60
  readiness_poll_seconds: 5
  image_seconds: 300
tools:
  default_round_limit: 10
  total_round_limit: 40
  search:
    enabled: true
    round_limit: 30
    time_limit_seconds: 180
    action_timeout_seconds: 45
    page_chars: 12000
    engines: [duckduckgo]
    browser:
      enabled: true
      idle_seconds: 300
```

`gateway.port` must differ from `api.port`. `timeouts.image_seconds` bounds one image generation or edit.

The entire `tools` block and its nested fields are optional with the defaults above. `engines` must be nonempty, distinct and drawn from `duckduckgo`, `bing`, `google`; order is fallback order unless a call selects one engine. Only `duckduckgo` (its plain HTML page, `html.duckduckgo.com/html/`) gives useful results to the hub: Bing answers non-browser clients with results for the query's first word only, and Google, from the EU, with its cookie page. Both still count as readable pages, so as fallbacks they would hand the model junk instead of an error. Limits must be positive and finite; action limits are integers 1–1000 and `page_chars` is 1000–120000. The browser path is resolved by Playwright, with no host executable/profile setting.

For HTTP-only research set `tools.search.browser.enabled: false`: `web_look` is omitted, while searching, reading and web-image tools remain available; explicit `mode: browser` reports disabled. To disable the entire family and its instruction note, set `tools.search.enabled: false`. Restart the hub after editing YAML. Browser startup failure leaves HTTP usable and returns the actual rendering limitation.

A “round” in these config names means one attempted hub action, including failures, bad arguments, cached visits, pagination and image inspection. All five search functions share 30 actions; existing image functions share the default family allowance of 10. There are at most 40 hub actions overall and 40 tool-bearing model iterations, followed by a final answer iteration. Calls in one model completion execute in order and count separately; an internal HTTP → browser fallback counts once. Exhausted families disappear from later offers, and stale calls still return hub errors rather than becoming client functions. The model is asked to answer from collected evidence and state any gaps; a model that continues demanding hub tools in the final iteration fails explicitly.

Search elapsed time defaults to 180 seconds from its first attempted action, including intervening model calls. Each action has a 45-second deadline including engine fallback, browser startup and queue wait. These stop further research and permit a partial final answer; they do not cut off final text streaming. HTTP requests have 15 seconds and browser navigation 20 seconds, followed by a bounded 350 ms settling interval rather than `networkidle`.

Default resource bounds: 12000 characters per page result including metadata/URLs, 120000 extracted characters per page, 120 retained link and image references each before result-size trimming, 30 snapshots and 1 MB retained page text/reference data per response. Results report `next_offset` and omission counts. HTTP bodies are capped at 5 MiB both on the wire and decompressed, with at most 10 redirects; images at 8 MiB and screenshots at 2 MiB. Validated images are JPEG/PNG/WebP, at most 16384 pixels per dimension and 16 million pixels overall. Browser DOM extraction is bounded to 2 million characters, each context to 300 routed requests, and at most eight web-image/screenshot pixel attachments remain in the model context. Older web pixels are removed while their labels permit re-view; user-image handling is preserved.

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
    kind: llm
    machines: [spark-1, spark-2, spark-3, spark-4]
    head: spark-1
    start_command: /home/agora/pro/start.sh
    stop_command: /home/agora/pro/stop.sh
    endpoint: http://spark-1.local:8000/v1
  images:
    name: Images
    kind: image
    machines: [spark-4]
    head: spark-4
    start_command: /home/agora/images/start.sh
    stop_command: /home/agora/images/stop.sh
    endpoint: http://spark-4.local:8001/v1
```

`kind` is required. `llm` deployments serve OpenAI chat completions. Chat clients' images reach them unchanged, so they should accept image input. `image` deployments speak the OpenAI Images API (`/images/generations` and `/images/edits` with `b64_json`), list at least one model at `/models`, and back the hub's image tools. Their `/images/edits` must accept up to four repeated `image` fields, because an edit can combine images; Qwen-Image-2.1 on vLLM-Omni does. Clients never reach image deployments directly. Any engine works (vLLM, SGLang, TensorFold, …) as long as it speaks these APIs.

Each key is the deployment id (hub-generated for deployments added through the API; `add` is reserved). `start_command` and `stop_command` start with an absolute path and may take arguments (`/home/agora/pro/start.sh --tp 4`). They run on `head` from the SSH user's home with no `cd` (a script that needs its own folder does `cd "$(dirname "$0")"`), with stdin closed. The start script must exit after launching the model; after it exits 0, the hub waits for `<endpoint>/models` to answer 2xx within `deployment_start_seconds`. `endpoint` is the OpenAI-compatible base URL that agents use, usually ending in `/v1` (`http://spark-1.local:8000/v1`, not `…/v1/models`). The hub probes `<endpoint>/models` for status: any 2xx means the deployment is up, and the `id` of each entry in the response's `data` list is a model the deployment serves.

For a native run, set up the owner-only configuration directory and required `known_hosts` file by hand (`make deploy` does this for Docker), then set `api.admin_token` in `hub.yaml` to enforce authentication, and set up Home Assistant through `POST /admin/integrations/homeassistant` once the hub runs:

```sh
CONFIG_DIR="$HOME/.config/agora/agora-hub"
mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
cp examples/{hub,machines,deployments}.yaml "$CONFIG_DIR/"
touch "$CONFIG_DIR/known_hosts"
chmod 600 "$CONFIG_DIR"/{hub.yaml,machines.yaml,deployments.yaml,known_hosts}
```

`known_hosts` and relative SSH key paths are resolved from the configuration directory.

The hub creates `agora.db` (SQLite: users, invites and token hashes) in the configuration directory, next to `files/`. It's backed up with the folder. It also creates `sessions/` there: one folder per chat that names a `conversation` (see "Sessions").

## 4. Endpoint calls

When `api.admin_token` is set, every admin endpoint requires `Authorization: Bearer <admin-token>` and answers `401` otherwise; when it is empty, admin endpoints do not check authentication. Gateway routes still require user tokens as described below. Add and update apply immediately (no restart) and rewrite `machines.yaml` or `deployments.yaml`, which drops its YAML comments; saving the Home Assistant settings likewise rewrites `hub.yaml` and drops its comments.

| Method and path | Body | What it does |
| --- | --- | --- |
| `GET /admin/machines/status` | None | Refreshes machine observations (with each machine's `name`, `host`, `user`, `port`, `key_path`, `plug_entity_id`, `shutdown_command` and `device`; never passwords) without mutations. |
| `POST /admin/machines/add` | Machine fields (`name`, `host`, `user`, ...) | Adds a machine with a hub-generated 8-hex id; returns `201` with the machine (no passwords), or `422`. |
| `POST /admin/machines/test` | Machine fields, as for add | Validates them like add, then tries an SSH login without saving the machine; returns `200` with `{"reachable", "error"}` (`error` is `null` when the login works, otherwise `auth: ...`, `host_key: ...` or `connect: ...`), or `422`. Like any first connection, it saves a new host's key to `known_hosts`. |
| `POST /admin/machines/{machine_id}/update` | Any subset of machine fields; `null` resets one to its default | Updates a machine; returns `200`, `404`, `409` (`machine_busy`), or `422`. |
| `POST /admin/machines/{machine_id}` | `{"action":"on"}` or `{"action":"off"}` | Starts an independent machine action; returns `202`, or `409` when blocked. `{"action":"off"}` answers `409 deployment_active` while the machine runs, starts or stops a deployment. |
| `GET /admin/deployments/status` | None | Probes each `<endpoint>/models` without mutations and returns the deployment fields (`name`, `kind`, `machines`, `head`, `start_command`, `stop_command`, `endpoint`), `observed_state` (`starting`/`stopping` while an operation runs, otherwise `running` when the probe answers 2xx, else `stopped`), `ready`, `models` (the served model ids from the probe; empty when not ready), `error`, `checked_at` and `operation`. The top-level `gateway` is `{"host", "port", "agora", "image"}`: the public host (or `null`), the gateway port, and the ids of the deployments that `model: "agora"` and the image tools currently go to (or `null`). |
| `POST /admin/deployments/add` | All seven deployment fields (`name`, `kind`, `machines`, `head`, `start_command`, `stop_command`, `endpoint`) | Adds a deployment with a hub-generated 8-hex id; returns `201` with the deployment (`id` plus the seven fields), `422`, or `500`. |
| `POST /admin/deployments/{deployment_id}/update` | Any subset of deployment fields; `null` drops one, which is a `422` since all are required | Updates a deployment; returns `200` with the deployment, `404`, `409` (`deployment_busy` while it has an active operation), `422`, or `500`. |
| `POST /admin/deployments/{deployment_id}` | `{"action":"start"}` or `{"action":"stop"}` | Starts the deployment's start or stop script on `head`; returns `202` with the operation's live NDJSON stream (below), or `404`, `409` (`busy`, `deployment_active`), or `422` as JSON. Closing the stream doesn't affect the operation. |
| `GET /admin/deployments/{deployment_id}/output` | None | Returns `200` with the NDJSON stream of the deployment's current or last operation (a finished one replays its log and ends at once), `404 Deployment not found`, or `404 No operation` when it has had none since the hub started. |
| `GET /admin/accounts` | None | Returns `200 {"accounts": [{"id", "name", "disabled", "tokens": [{"device_alias", "created_at", "hash8", "revoked"}]}]}`: every user, oldest first, each with every token it redeemed (revoked ones too, oldest first; `hash8` is the last 8 characters of the stored token hash). |
| `POST /admin/accounts/new` | `{"name":"Ana"}` | Creates a user and its first invite; returns `201 {"id", "invite_code", "expires_at"}`, or `422` for a missing or blank name. |
| `POST /admin/accounts/{user_id}/disable` | `{"disabled":true}` or `{"disabled":false}` | Pauses or resumes a user; returns `200 {"id", "name", "disabled"}`, `404 Account not found`, or `422` unless `disabled` is a JSON boolean. |
| `POST /admin/accounts/{user_id}/invite` | None | Creates a new invite; returns `201 {"id", "invite_code", "expires_at"}`, `404 Account not found`, or `409 Account is disabled`. |
| `POST /admin/accounts/{user_id}/revoke` | None | Revokes all of the user's tokens and unused invites; returns `200 {"revoked": <tokens revoked>}` or `404 Account not found`. |
| `GET /admin/integrations/homeassistant` | None | Returns `{"host", "port", "token", "power_cycle_delay_seconds"}` without contacting Home Assistant; `token` is `""` when not configured. It returns the token to any authorized caller. |
| `POST /admin/integrations/homeassistant` | Any subset of `host`, `port`, `token`, `power_cycle_delay_seconds`; `null` resets one to its default, and a `null` or `""` token unsets it | Saves the settings without contacting Home Assistant; returns `200` with the settings view, `422`, or `500`. |
| `POST /admin/integrations/homeassistant/test` | As for POST | Validates the merged settings and calls Home Assistant's `GET /api/` with them, saving nothing; returns `200 {"reachable", "error"}` (`reachable` when Home Assistant answered at all, `error` is `null` when it accepted the token, otherwise `not_configured: ...` or `http: ...`), or `422`. |
| `GET /admin/integrations/homeassistant/switches` | None | Returns `200 {"switches": [{"entity_id", "name", "state", "machines"}]}` (every `switch.*` entity, its Home Assistant state, and the ids of machines whose `plug_entity_id` it is), `409` (`home_assistant_not_configured`), or `502` when Home Assistant fails. |
| `POST /admin/integrations/homeassistant/switches` | `[{"entity_id":"switch.x","state":"on"\|"off"}, ...]` | Turns the switches off, then on, and returns the fresh switch list like GET; `409`, `502`, or `422`. **No guard**: it ignores running operations and machine state, so turning off a machine's plug cuts its power without shutting it down. |

Deployment streams are `application/x-ndjson`, one JSON object per line: first `{"type":"operation","operation":{...}}` (the operation as it is now), then `{"type":"line","source":"hub"|"stdout"|"stderr","text":...,"at":...}` for each buffered and then live line (the hub keeps the last 2,000 lines of each deployment's last operation, in memory), and last `{"type":"end","operation":{...}}` once the operation finishes.

```sh
ADMIN_TOKEN=your-64-character-token
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:3333/admin/machines/status
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"on"}' http://localhost:3333/admin/machines/spark-1
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Spark 5","host":"spark-5.local","user":"agora","password":"...","plug_entity_id":"switch.spark_5"}' \
  http://localhost:3333/admin/machines/add
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Spark 5","host":"spark-5.local","user":"agora","password":"...","plug_entity_id":"switch.spark_5"}' \
  http://localhost:3333/admin/machines/test
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"10.0.0.5"}' http://localhost:3333/admin/machines/spark-1/update
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:3333/admin/deployments/status
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Pro","kind":"llm","machines":["spark-1","spark-2"],"head":"spark-1","start_command":"/home/agora/pro/start.sh","stop_command":"/home/agora/pro/stop.sh","endpoint":"http://spark-1.local:8000/v1"}' \
  http://localhost:3333/admin/deployments/add
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"start_command":"/home/agora/pro/start.sh --tp 2"}' http://localhost:3333/admin/deployments/pro/update
curl -N -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"start"}' http://localhost:3333/admin/deployments/pro
curl -N -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:3333/admin/deployments/pro/output
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://localhost:3333/admin/integrations/homeassistant/test
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:3333/admin/integrations/homeassistant/switches
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '[{"entity_id":"switch.lamp","state":"on"}]' http://localhost:3333/admin/integrations/homeassistant/switches
```

### Gateway (port 8888)

The gateway speaks the OpenAI API. Clients use the base URL `http://<public host>:8888/openai/v1`; the prefix names the API format.

Every `/openai/v1` route, `GET /agora/v1/me` and the `/agora/v1/sessions…` routes require `Authorization: Bearer <token>`, a user token from `/agora/v1/link` (see "Accounts" below). A missing, unknown or revoked token, or a disabled user's token, gets `401 invalid_api_key`. There's no cache: a revoke, disable or enable applies from the next request. The admin token is never valid here, and the admin routes don't exist on this port. Errors use OpenAI's shape, `{"error": {"message", "type", "code"}}`.

`model: "agora"` goes to the first running LLM deployment (in `deployments.yaml` order) and its first served model id. It's the model-agnostic choice for small tools that don't care which model answers. Any other `model` must exactly match a model id a running LLM deployment serves. Otherwise the request fails with `404 model_not_found`, or `503` while no LLM is running.

| Method and path | Auth | What it does |
| --- | --- | --- |
| `POST /agora/v1/link` | none | Body `{"code":"<invite code>", "device_alias":"<device name>"}`; both are required strings, and `device_alias` can't be blank. Returns `200 {"token", "user": {"id", "name"}}`, `400 invalid_request`, or `400 invalid_invite` (invalid, expired or already used). |
| `GET /agora/v1/me` | token | `200 {"id", "name"}`: the token's user. |
| `GET /openai/v1/models` | token | `{"object": "list", "data": [...]}`: `agora` first (while an LLM runs), then every model id a running LLM deployment serves (`owned_by` is its deployment id). Image models aren't listed. `[]` while no LLM runs. |
| `POST /openai/v1/chat/completions` | token | A plain pass-through to the LLM (below). The hub's own errors: `400 invalid_request` (the body isn't a JSON object, or `model` is missing or not a string), `404 model_not_found` (the message lists the ids `/models` lists; image model ids get it too), `502 upstream_unavailable` (the LLM couldn't be reached), `503 no_llm_running` (with `Retry-After: 30` while an LLM is starting). The LLM's own errors reach the client unchanged. |
| `POST /openai/v1/responses` | token | The OpenAI Responses API, through the hub's harness (below). Errors: `400 invalid_request` (a bad body, `input` or item, a `conversation` that isn't a UUID string, or `background` without a conversation), `400 unsupported_parameter` (`previous_response_id`), `400 unsupported_tool`, `403 forbidden` (another user's conversation), `404 model_not_found`, `409 conversation_busy` (a turn of that conversation is running), `502 upstream_unavailable`, `503 no_llm_running`, and `upstream_error` with the upstream's status when the LLM's first round answers an error. |
| `GET /openai/v1/responses/{id}` | token | A running turn of yours. With `stream=true`, its events from the start, then live until it ends; with `starting_after=<n>`, after the event whose `sequence_number` is `n`. Without `stream`, the response object as it stands. `403 forbidden` (another user's turn), `404 not_found` (no such turn, or it has ended: read its session). |
| `POST /openai/v1/responses/{id}/cancel` | token | Stops a running turn of yours. Answers the response object with `status: "cancelled"` once the turn's end is recorded. `403 forbidden` (another user's turn), `404 not_found` (no such turn, or it has ended). |
| `GET /agora/v1/sessions` | token | `{"data": [...]}`: your chats, newest `updated_at` first (see "Sessions"). Another user's chats are never listed. |
| `GET /agora/v1/sessions/{id}` | token | One of your chats, turn by turn. `403 forbidden` (another user's chat), `404 not_found` (no such chat, or an id that isn't a UUID). |
| `GET /agora/v1/sessions/{id}/files/{name}` | token | One of the chat's images, with its media type. `403 forbidden` (another user's chat), `404 not_found` (no such chat, or `name` isn't linked in its `files/`). |
| Any other method or path under `/openai/v1/` | token | `404 not_found`, naming the five `/openai/v1` routes above. `GET /openai/v1/chat/completions` gets it too. |

The chat route is a plain pass-through: the hub resolves `model` (rule above), checks the token and logs the request. Everything else in the body, the reply (streamed or not) and the LLM's errors go through unchanged, and so do images in messages. The client's headers are never forwarded. A client disconnect stops the generation.

- Public host: `make deploy` detects the address to advertise (Tailscale `100.x`, else LAN, else any), prints it, and the hub reports it to the dashboard. `make deploy PUBLIC_HOST=…` overrides it.
- `files/` (in the config folder) holds the images the Responses harness stores and saved tool calls (`<id>.json`); published found images also have provenance sidecars (`<id>.metadata.json`). It is never cleaned up. Over HTTP, only a chat's own images are served, through `GET /agora/v1/sessions/{id}/files/{name}`. Sessions link into it, so keep it, and copy the config folder whole (`cp -a`, `rsync -a`) so the links survive.

```sh
TOKEN=your-64-character-token # from /agora/v1/link
curl -H "Authorization: Bearer $TOKEN" http://localhost:8888/openai/v1/models
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"agora","stream":true,"messages":[{"role":"user","content":"hi"}]}' http://localhost:8888/openai/v1/chat/completions
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"agora","messages":[{"role":"user","content":"hi"}]}' http://localhost:8888/openai/v1/chat/completions
```

### Accounts

The admin creates a user and gets an invite code, valid once for 10 minutes. The client redeems it at `/agora/v1/link` on the gateway port for a token, naming the device it links in `device_alias`. The token is shown once; the hub stores only its SHA-256, next to that alias. `revoke` cuts off all of a user's tokens and unused invites; a new invite then gives a new token. Disabling pauses a user, and enabling brings back their tokens that weren't revoked. Revoking is for good.

```sh
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Ana"}' http://localhost:3333/admin/accounts/new     # {"id", "invite_code", "expires_at"}
curl -X POST -H 'Content-Type: application/json' \
  -d '{"code":"<invite_code>","device_alias":"iPhone"}' \
  http://localhost:8888/agora/v1/link                               # {"token", "user": {"id", "name"}}
curl -H "Authorization: Bearer $TOKEN" http://localhost:8888/agora/v1/me
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"disabled":true}' http://localhost:3333/admin/accounts/<id>/disable
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:3333/admin/accounts/<id>/revoke
```

Never open `agora.db` from the host while the hub runs: Docker Desktop's bind mount crosses a VM, and SQLite's file locking isn't reliable across it. To look inside, use `docker exec -it agora-hub python -m sqlite3 /config/agora.db`.

### Responses API

`POST /openai/v1/responses` is for apps: chat, photos the model sees, image generation and editing, and web search.

- **Stateless, or a conversation.** Send the whole conversation each time: your earlier input plus every output item you received. Or name it with `conversation` (a UUID) and send only what's new: the hub puts the chat's recorded history before your input (see "Sessions"). `previous_response_id` still answers `400 unsupported_parameter`.
- **Fields honoured:** `model` (same rule as chat), `input` (a string or a list of items), `instructions`, `tools` (`function` tools; `web_search`, `web_search_preview` and `image_generation` are accepted and ignored, since the hub's tools are on anyway; any other type answers `400 unsupported_tool`), `tool_choice`, `stream`, `temperature`, `top_p`, `parallel_tool_calls`, `max_output_tokens`, `conversation` (a UUID string, or `{"id": "<UUID>"}`) and `background`. `metadata` is only echoed back. Everything else (`store`, `include`, `text`, `reasoning`, `user`, …) is ignored; `store` is always `false` and `usage` `null`.
- **Input items:** `message` (user, system, developer and assistant), `function_call`, `function_call_output`, `image_generation_call` and `web_image_call`. `reasoning` and `web_search_call` items are skipped; any other type answers `400 invalid_request`.
- **The harness:**
  - every image is labelled `[image_N]`, in order, including the ones the model generated;
  - the model gets the pixels of the images in view: those of the current turn (a user message starts a turn) and of the latest earlier turn that has any, plus everything the turn makes, so it can check a result and fix it once. Older images reach it as `[image_N, not attached]`, which keeps requests small however long the chat gets;
  - `view_image`, always: the model calls it to look at a not-attached image again. The call is not an output item;
  - a generated image you send back reaches the model as the tool call that made it and its result, as in the turn that made it. As a bare `[image_N]` in the assistant's own text, it taught the model to answer with a label instead of calling the tool;
  - image generation, and editing of up to four images, while an image deployment runs. Editing works on any image of the conversation, attached or not: the hub reads the file by its label;
  - the five default-on search-family functions described below;
  - your own `function` tools, which come back as `function_call` items. A client declaration wins a name collision with a hub function, including when its hub family is disabled or exhausted;
  - the family/global action budgets in §3, a final synthesis iteration, and cleanup when a turn ends or is stopped. `tool_choice: "none"` suppresses hub tool offering and execution from the first iteration; unsolicited hub calls then fail explicitly.
- **Background:** `background: true` keeps a turn running after the client leaves. It needs a `conversation`, where the finished turn lives; otherwise `400 invalid_request`. With `stream: true` the response streams the turn's events; without, it answers at once with the response as it stands (`status: "in_progress"`). Without `background`, the turn stops when the client that started it leaves, as before. Any running turn of yours, with or without `background`, can be watched and stopped:
  - `GET /openai/v1/responses/{id}?stream=true` rejoins it: it replays the turn's events from the start (or after `starting_after=<n>`), then follows it live until it ends. A watcher that gets nothing new for 10 seconds sends a keep-alive comment, and leaving never stops a background turn;
  - `POST /openai/v1/responses/{id}/cancel` stops it: the running tool or LLM round is cancelled, the items in progress are recorded as they stand, and the turn ends `cancelled`. Every watcher's stream ends there, with no closing event, so read the session for the final state;
  - a turn that has ended answers `404 not_found` on both: the hub keeps finished turns only in sessions. A hub restart ends every turn.
- **Images in:** `input_image` with a data URL, in JPEG, PNG or WebP. Convert HEIC first, and downscale large photos. Other URLs are unavailable to the model.
- **Images out:** `image_generation_call` items carry `result` (base64), `output_format`, `action` (`generate` or `edit`) and `revised_prompt` (the prompt the model wrote). Send them back with `"result": null`: the hub finds the image, and the call that made it, by its id. They are JPEG at quality 95: the hub asks the image server for that (`IMAGE_OUTPUT` in `tools/images.py`) and converts nothing itself. The server's default, an uncompressed PNG, is ten times the size.
- **Reasoning:** a thinking model's reasoning arrives as a `reasoning` item. It's never sent back to the model.
- **Errors:** the HTTP errors in the table above. A failure after the first round ends the turn with `status: "failed"` (`response.failed` when streaming); `finish_reason: length` ends it `incomplete`.

With `stream: true` the events are typed, so an app can draw one card per tool call and update it in place. `: keep-alive` comments go out every 10 s while a tool runs, and there is no `[DONE]`. "draw a red fox", shortened:

```
response.created, response.in_progress
response.output_item.added (reasoning) … response.reasoning_text.delta … response.output_item.done
response.output_item.added (message)   … response.output_text.delta "Sure, drawing it." … response.output_item.done
response.output_item.added (image_generation_call, in_progress, action, revised_prompt)
response.image_generation_call.in_progress, response.image_generation_call.generating, : keep-alive
response.image_generation_call.completed
response.output_item.done (image_generation_call, completed, result, output_format)
response.output_item.added (message)   … "Here's your fox." … response.output_item.done
response.completed (the response object with all four items)
```

Without `stream`, the body is the final response object, with HTTP 200 whether it ended `completed`, `incomplete` or `failed`.

```sh
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"agora","stream":true,"input":"draw a red fox"}' http://localhost:8888/openai/v1/responses
```

### Sessions

The hub records every chat that names a `conversation`: a UUID string, or `{"id": "<UUID>"}`, in any case; the hub uses its lower-case form as the folder name. It creates the session on first use; there is no "create conversation" route. The user comes from the token. `/openai/v1/chat/completions` is never recorded.

- **Incognito:** without `conversation`, the turn runs as before and nothing is written under `sessions/`. Its images still go to the shared `files/` store, because later turns replay them by id.
- **First turn** (no `conversation.jsonl` yet): the request's input is the whole conversation, and it is all recorded. That covers a new chat and a chat older than sessions.
- **Later turns:** the hub puts the session's history (below) before the request's input, so the client sends only the new message.
- Either way, the turn's input is recorded exactly as sent.

```
<config folder>/sessions/<user id>/<session id, lower case>/
  session.json         {"id", "user_id", "created_at", "updated_at"}
  conversation.jsonl   one JSON line per event
  files/               only once the chat has an image: relative links into the shared files/
```

- **`session.json`:** `created_at` is the time of the first line and never changes; `updated_at` is the time of the last line. Times are UTC with milliseconds, as in the logs. The file is rewritten atomically after every line.
- **`conversation.jsonl`:** `{"timestamp", "request", "type", "payload"}`, appended and never rewritten. `request` is the response id, as in the logs, so a turn's lines group by it. Because `turn_started` carries the input, a turn splits cleanly into input and output.

| `type` | `payload` |
| --- | --- |
| `turn_started` | `model` (after `agora` is resolved), `instructions` (`null` when there are none) and `input`: the turn's input items as sent, images swapped (below). A string input becomes one user message. |
| `response_item` | an output item: each one when it's done, plus the unfinished ones, as they stand, when a turn ends early |
| `tool_call` | `call` (the hub's 32-hex call id, also the stem of `ig_`/`wi_` ids and generated files), `name`, `arguments` (the model's JSON string) |
| `tool_output` | `call`, `output` (the full text the model read, `null` on an error), `error`, `files` (the file names it made or looked at) |
| `turn_ended` | `status`, `error` |

- **Items** are verbatim, except that a bare `{"role", "content"}` gets `"type": "message"` and images are swapped. No base64 is written. A photo part becomes `{"type": "input_image", "image_url": null, "file": "<name>"}`, and an image item gets `"result": null, "file": "<name>"` (`null` when the hub has no such image). Each file is a relative symlink, `files/<name> -> ../../../../files/<name>`, so it reads both in the container and on the host.
- **End statuses** (`turn_ended`): `completed`, `incomplete` or `failed` (the response's own, with its error message), `failed` also when the first LLM round can't be reached or answers an error; `cancelled` (Stop); `interrupted` (a clean hub shutdown); and `disconnected`, when the client that started a turn without `background` left. Unfinished output items are recorded first, as they stand, with `status: "in_progress"`.
- **Read back,** a turn's `status` is one of those, or `in_progress` while it runs. A turn with no end line that isn't running is `interrupted` too: the hub crashed mid-turn. Nothing is deleted: its finished items are kept, and the half-made ones were never written.

Three routes read your chats back. All three need the token and show only the token user's chats.

- **`GET /agora/v1/sessions`:** your chats, newest `updated_at` first. Each is its `session.json` plus `running`: the id of the turn that is running, or `null`.

  ```json
  {"data": [{"id": "8b1e6c2a-…", "user_id": "75e0ed3d-…", "created_at": "2026-11-26T11:49:01.276Z",
             "updated_at": "2026-11-26T11:52:10.004Z", "running": "resp_ad46…"}]}
  ```

- **`GET /agora/v1/sessions/{id}`:** one chat, turn by turn. Items are as recorded: images are `file` names, and there is never base64. A running turn's `output` holds only the items finished so far. To watch the rest, rejoin it with `GET /openai/v1/responses/{running}?stream=true`, which replays the whole output from the start.

  ```json
  {"id": "8b1e6c2a-…", "user_id": "75e0ed3d-…", "created_at": "…", "updated_at": "…", "running": null,
   "turns": [{"id": "resp_068d…", "started_at": "…", "status": "completed", "error": null,
              "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "draw a fox"},
                                                                      {"type": "input_image", "image_url": null, "file": "59ff….jpg"}]}],
              "output": [{"type": "message", …}, {"type": "image_generation_call", "id": "ig_ef3f…", "result": null, "file": "ef3f….png", …}]}]}
  ```

- **`GET /agora/v1/sessions/{id}/files/{name}`:** one of the chat's images, with its media type. It is served only if `name` is linked in that chat's `files/`; otherwise `404 not_found`.

  ```sh
  curl -o fox.png -H "Authorization: Bearer $TOKEN" http://localhost:8888/agora/v1/sessions/<id>/files/<name>
  ```

Who may see what:

| Request | Yours | Another user's | Doesn't exist |
| --- | --- | --- | --- |
| `POST /openai/v1/responses` with `conversation` | runs (or `409` while busy) | `403 forbidden` | created on first use |
| `GET /openai/v1/responses/{id}`, `…/cancel` | works while running | `403 forbidden` | `404 not_found` (also once ended) |
| `GET /agora/v1/sessions/{id}`, `…/files/{name}` | works | `403 forbidden` | `404 not_found` (also for an id that isn't a UUID) |

"Another user's" means that the id is in another user's folder, or that the turn was started with another user's token. A refused request writes nothing.

**The history the model gets:** for each recorded turn, in order, its input, then its output.

- Each photo's `file` goes back to the photo itself, from the shared `files/`. A `file: null` part stays as it is, and the model gets "[image unavailable]".
- Everything else goes through as if the client had sent it: image items replay by their `ig_`/`wi_` ids; reasoning, searches and unfinished image items are skipped; a stopped reply's partial text counts as a reply.
- `tool_call` and `tool_output` lines are only a record. They are not part of the history.
- So what a client shows from `GET /agora/v1/sessions/{id}` is exactly what the model sees.

Read a session by hand with `docker exec agora-hub ls -R /config/sessions`, or on the host with `jq -c '[.timestamp, .type]' ~/.config/agora/agora-hub/sessions/<user id>/<session id>/conversation.jsonl`.

Sessions hold whole chats, and their links reach every photo. They are never cleaned up. Incognito turns leave only their images in `files/`. Each session's folder and its `files/` are mode 700, inside the private config folder.

### Web research through Responses

The harness automatically offers research tools and instructions to verify changing facts, open user URLs, check dates, compare sources and cite pages actually read. No per-request tool list is needed. The deployment needs function calling and, for pixel inspection, vision support. Tool access does not guarantee factual accuracy; page content remains untrusted. `/openai/v1/chat/completions` stays pass-through.

| Function | Arguments | Result/use |
| --- | --- | --- |
| `web_search` | Required `query` (nonempty, at most 2000 characters); optional `engine` (`duckduckgo`, `bing`, `google`), `mode` | Fresh ordinary search-page fetch, readable text, links and available image references; configured engine fallback when access fails. |
| `open_page` | Exactly one of `url` or `ref`; optional nonnegative integer `offset` (default `0`), `mode` | Direct URL, discovered link, cached revisit or next text segment. |
| `web_look` | Optional `scroll`: `current` (default), `down`, `up` | Render the current readable page and attach a viewport JPEG for model inspection; updated snapshot. |
| `view_web_images` | Required `images`: 1–4 page image references or previously viewed web-image labels | Download/validate and attach image pixels as `[image_N]` for the next model round. |
| `show_web_image` | Required `image`: one page image reference or web-image label | Publish a selected image with source-page and original image-URL attribution. |

For `web_search` and `open_page`, `mode` defaults to `auto`: HTTP once, then headless rendering at most once when extraction is empty, blocked, consent or otherwise unsuccessful. `http` uses only HTTP; `browser` forces rendering. An `auto` revisit reuses a retained readable snapshot; explicit `http`/`browser` refreshes it. Searches are always fresh. Search uses ordinary public engine URLs and generic extraction, with no private search endpoints or engine-specific selectors.

Page results are JSON strings with snapshot id, requested/final URLs, title, fetch time/mode, text, bounded link/image references, offsets, omission counts and attempted modes/engines. Text preserves headings, paragraphs, tables and captions. `access_status` is `content`, `empty`, `blocked`, `consent`, `error` or `unsupported`. Unsupported document types are not extracted. A completed progress item means the tool returned; check the result's access status to distinguish readable content from blocked access.

References are immutable and page-qualified: `p3` is a snapshot, `p3:l7` a link, `p3:i2` an image. These differ from conversation labels such as `image_2`. The model can follow `open_page(ref="p3:l7")`, go back with `open_page(ref="p3")`, or continue with `open_page(ref="p3", offset=<returned next_offset>)`. A refresh/render creates a new snapshot id. Relative links resolve against the document base, redirect destinations and fragments are preserved, and failed navigation preserves the last successful current page. Unknown/evicted and previous-response references fail clearly. Cache and cookies end with the response; reopen cited URLs on a later turn.

Try these prompts, or use the authenticated call below after obtaining `$TOKEN` through Accounts:

- “Find recent research on urban heat mitigation, open the original sources and compare their evidence with links.”
- “Summarize https://en.wikipedia.org/wiki/Urban_heat_island and explain the mitigation table, citing that page.”
- “Find information about the James Webb Space Telescope and show a relevant source image with attribution.”

```sh
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"agora","stream":true,"input":"Search for recent urban heat mitigation research, read the original sources and cite them."}' \
  http://localhost:8888/openai/v1/responses
```

Browsing progress uses `web_search_call` with `id: "ws_<32 hex>"`, `status`, structured `action` and readable `label`. `action` contains the function `type`, supplied arguments and `label`. These fields and `web_image_call` are Agora extensions. A shortened stream (envelopes also carry sequence and item indices):

```text
response.output_item.added: {"item":{"type":"web_search_call","id":"ws_…","status":"in_progress","action":{"type":"open_page","url":"https://example.org/article","label":"Opening https://example.org/article"},"label":"Opening https://example.org/article"}}
response.web_search_call.in_progress
response.web_search_call.searching
response.web_search_call.completed
response.output_item.done: {"item":{"type":"web_search_call","id":"ws_…","status":"completed",…}}
response.output_item.added: {"item":{"type":"web_image_call","id":"wi_…","status":"in_progress","result":null,…}}
response.web_image_call.in_progress
response.web_image_call.completed
response.output_item.done: {"item":{"type":"web_image_call","id":"wi_…","status":"completed","result":"<base64>","output_format":"jpeg","source_page_url":"https://example.org/article","image_url":"https://example.org/photo.jpg",…}}
response.output_text.delta: {"delta":"…answer with Markdown source links…"}
response.completed
```

Failures emit `response.web_search_call.failed` or `response.web_image_call.failed` and finish the item with `status: "failed"` and `error: {"message": "…"}`. A tool failure is evidence for the model to handle, and need not fail the entire response. Non-streamed responses contain the same final output items.

`view_web_images` and `web_look` expose progress but no visible pixel result to the client. Screenshots are model-only in this version; viewing an image does not automatically publish it. `show_web_image` produces a distinct `web_image_call`, never an `image_generation_call`, with completed `result` (base64), `output_format` (`jpeg`, `png`, `webp`), `source_page_url` and `image_url`. Attribution refers to the source page and image URL discovered there; it is not a claim of image ownership or authenticity.

Replay a found image as `{"type":"web_image_call","id":"wi_<original 32 hex>","status":"completed","result":null}` alongside the next user message. FileStore restores bytes, the tool call and provenance without fetching the site again; ordinary `[image_N]` labels let `edit_image` use it later. Generated images retain `image_generation_call`/`ig_` replay. Browsing progress is skipped and snapshots end with the response. Image bytes, including inspected images/screenshots, remain in the private config `files/` directory without automatic cleanup; protect and retain it for ID-only replay. `/files/` is an internal marker, not a public serving endpoint.

Updated Ago is required for readable progress and found-image cards with source links; the hub update alone cannot add those client features.

Run `uv run pytest -q` for tests. Browser tests require the matching Playwright headless shell (§2); socket fixtures use local sites and a scripted model to verify execution and cleanup, not research accuracy.

### Research runtime, limits and diagnosis

The hub owns a lazily started Playwright Chromium process, at most two isolated response contexts and one active tab per response. There is no persistent profile or cross-response cookie history. Contexts/tabs close when the turn ends: it completes, fails, is stopped, or is disconnected (a turn without `background` whose client left); the owned browser closes after 300 idle seconds by default and on hub shutdown. A bounded queue consumes the action deadline. Browser navigation waits for DOM content and a short settling interval; it does not wait for every ad or background connection.

Research permits public HTTP(S) only: embedded credentials, non-web schemes, local/private/non-public IPv4/IPv6 addresses and unsafe destination resolutions are rejected. HTTP and browser traffic use an in-process DNS-checking, numeric-address-pinning proxy, including redirects, image downloads and browser subresources. Service workers are blocked, WebSockets closed and QUIC/non-proxied WebRTC disabled. The web client is separate from gateway/upstream authorization and ignores environment proxy settings; it does not receive user bearer tokens. Queries and requested URLs still go to search engines and source websites. HTTP requests and the browser both identify themselves as `AgoraAgent/1.0 (+https://abril.me)` (`USER_AGENT` in `tools/search/loader.py`). Some sites, such as Wikipedia and its image server, reject agents without contact details, and also reject borrowed browser agents (a Safari or Chrome string) because the connection doesn't match, so the hub doesn't pretend to be a browser. Cookies/profile contents are not traced.

CAPTCHAs, consent, login/paywalls, empty or faulty extraction and some JavaScript sites can remain inaccessible. There is no stealth, CAPTCHA solving, login, arbitrary JavaScript supplied by the model, general clicking, form filling, PDF/video/audio extraction, full-site crawling or background research. Following ordinary links and scrolling the current page are supported. Layout changes can break extraction. Direct image downloads/screenshots do not imply a dedicated image-search API or full-resolution gallery extraction. Unsupported image formats may be visually inspected with `web_look` if rendering works. A text-only model must not claim to have inspected pixels.

Use `docker logs -f agora-hub` and correlate the `resp_` id with its `tool` lines (`name`, `seconds`, `error`). Page results, with their attempts and failure details, are in the session's `tool_output` lines, which incognito turns don't have. For blocked access or timeouts, check outbound connectivity, destination policy, access requirements and deadlines. For browser startup failures in Docker, rebuild/recreate the locked image and check `/opt/agora-browsers`, non-root permissions, seccomp and shared memory. For native installs, rerun the matching Playwright installation from §2. HTTP-only mode remains available while diagnosing rendering.

### Logs

The gateway logs to stdout as short JSON lines: `{"time", "request", "event", …}`. Read them with `docker logs -f agora-hub | grep --line-buffered '^{' | jq .` (uvicorn's access lines, one per HTTP request, aren't JSON).

| Event | When | Fields |
| --- | --- | --- |
| `request` | each `/chat/completions` and `/responses` turn | `endpoint` (`chat/completions` or `responses`), `user` (the token's user id) |
| `tool` | each hub tool, once it has ended | `call`, `name`, `seconds`, `error` (`null` when it worked) |
| `error` | each error the hub answers | `status`, `code`, `message` |

Request ids: chat requests get `chat_<16 hex>`, Responses requests their response id, `resp_<32 hex>`. Every line of a request carries its id; an error outside a turn's request id (a bad key, a bad body, or the read, watch and cancel routes) has `"request": null`.

The logs no longer hold what was said: no request or reply bodies, no tool arguments or results, no fetched pages. Whatever of that a chat needs, its session holds. They still name users and tools, so treat the host's Docker logs as sensitive. Docker keeps 5 × 20 MB per container, and each `make deploy` starts a fresh log.

APIs the gateway doesn't serve yet: a coding agent that needs one (Codex needs Responses features the hub doesn't have) can use a deployment's own endpoint directly. That endpoint has no token check and no `agora` alias.

Planned (not built yet):

- **Stored responses:** `store` and `previous_response_id`.
- **Partial images** while generating, once the image server streams them.
- **Codex** on `/openai/v1/responses`: custom and shell tool types, and encrypted reasoning.
- **Anthropic Messages** at `/anthropic/v1`, through the same harness.
- **Chats from other devices:** list a user's chats on any device, and resume one there.
- **Turns that survive a hub restart.**
