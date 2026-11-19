# Agora Hub

## 1. Intro

Agora Hub is a headless service for observing and controlling configured machines and their model deployment. It keeps runtime state in memory and can apply a daily wake/shutdown schedule. The hub also serves an OpenAI-compatible gateway on `:8888` at `/openai/v1`: OpenAI chat completions, as a plain pass-through to the running LLMs, and the OpenAI Responses API, which runs through the hub's harness (vision, image tools, web search, reasoning).

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

It then builds the image and starts the hub in the background, publishing port `3333` (admin API) and port `8888` (gateway), with the config folder mounted writable so the add and update endpoints can save machines and deployments. The container uses `--restart unless-stopped`, so it comes back after a crash, a Docker restart or a reboot (on macOS, enable Docker Desktop's "Start when you sign in"). Run `make deploy` again to rebuild and replace it; nothing in the config folder is overwritten. Use `make deploy CONFIG_DIR=/other/path` for a different folder.

> **Security: a fresh deploy has no authentication.** `api.admin_token` starts empty, and while it is empty the hub accepts every request, with any bearer token or none. `make deploy` publishes port `3333` on all network interfaces, so anyone who can reach this machine can power machines on and off, start and stop deployments, and add or edit machines. Set a token before using the hub on a shared network: put `openssl rand -hex 32` output in `api.admin_token` in `hub.yaml`, run `docker restart agora-hub`, and give the same value to agora-dashboard (`AGORA_HUB_ADMIN_TOKEN`). `gateway.api_keys` also starts empty, so the gateway on port `8888` is open too: anyone who can reach it can use the running models. Set gateway keys (§3) before sharing the network.

After the first deploy, set Home Assistant's `host`, `port` and long-lived access `token` through `POST /admin/integrations/homeassistant`, then add machines through `POST /admin/machines/add`, then deployments through `POST /admin/deployments/add` (see §4). After editing a YAML file by hand, run `docker restart agora-hub`.

```sh
curl -X POST -H 'Content-Type: application/json' -d '{"host":"192.168.1.10","port":8123,"token":"..."}' http://localhost:3333/admin/integrations/homeassistant
```

SSH host keys are trusted on first use, like `ssh -o StrictHostKeyChecking=accept-new`: the first time the hub connects to a host that isn't in `known_hosts`, it saves that host's key there. From then on, the host must keep presenting a saved key, or the hub refuses to connect and reports a `host_key` error, so a machine impersonating it can't collect the SSH or sudo password. After a legitimate key change (a reinstall), remove the old entry with `ssh-keygen -R <host> -f ~/.config/agora/agora-hub/known_hosts`; the next connection saves the new key. `known_hosts` is read on every SSH connection, so no restart is needed.

Logs: `docker logs -f agora-hub` (the gateway's conversation log is described in §4). Undeploy: `docker rm -f agora-hub`.

## 3. Config files

`hub.yaml` (secrets live in the YAML files, like the machine passwords: `api.admin_token` is empty (no authentication) or 64 hex characters from `openssl rand -hex 32`, which every request must then send as `Authorization: Bearer <token>`; `home_assistant.token` is a Home Assistant long-lived access token):

```yaml
api: {host: 127.0.0.1, port: 3333, admin_token: ""}
gateway:
  port: 8888
  api_keys: [] # empty = NO authentication; otherwise each key is 64 hex chars (openssl rand -hex 32)
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
```

Gateway keys: `gateway.api_keys` is a list; empty means the gateway on `gateway.port` accepts every request, otherwise each key is 64 hex characters (`openssl rand -hex 32`) and clients send one as `Authorization: Bearer <key>`. Gateway keys are independent of the admin token: neither works in place of the other. Edit the list in `hub.yaml`, then run `docker restart agora-hub`. `gateway.port` must differ from `api.port`. `timeouts.image_seconds` bounds one image generation or edit.

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

## 4. Endpoint calls

When `api.admin_token` is set, every endpoint requires `Authorization: Bearer <admin-token>` and answers `401` otherwise; when it is empty, no endpoint checks authentication. Add and update apply immediately (no restart) and rewrite `machines.yaml` or `deployments.yaml`, which drops its YAML comments; saving the Home Assistant settings likewise rewrites `hub.yaml` and drops its comments.

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
  -d '{"name":"Pro","kind":"llm","machines":["spark-1","spark-2"],"head":"spark-1","start_command":"/home/agora/pro/start.sh","stop_command":"/home/agora/pro/stop.sh","endpoint":"http://spark-1.local:8000/v1"}' \
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

### Gateway (port 8888)

The gateway speaks the OpenAI API. Clients use the base URL `http://<public host>:8888/openai/v1`; the prefix names the API format.

When `gateway.api_keys` is set, `/openai/v1` requires `Authorization: Bearer <gateway key>` and answers `401 invalid_api_key` otherwise. The admin token is never valid here, and the admin routes don't exist on this port. Errors use OpenAI's shape, `{"error": {"message", "type", "code"}}`.

`model: "agora"` goes to the first running LLM deployment (in `deployments.yaml` order) and its first served model id. It's the model-agnostic choice for small tools that don't care which model answers. Any other `model` must exactly match a model id a running LLM deployment serves. Otherwise the request fails with `404 model_not_found`, or `503` while no LLM is running.

| Method and path | Auth | What it does |
| --- | --- | --- |
| `GET /openai/v1/models` | key | `{"object": "list", "data": [...]}`: `agora` first (while an LLM runs), then every model id a running LLM deployment serves (`owned_by` is its deployment id). Image models aren't listed. `[]` while no LLM runs. |
| `POST /openai/v1/chat/completions` | key | A plain pass-through to the LLM (below). The hub's own errors: `400 invalid_request` (the body isn't a JSON object, or `model` is missing or not a string), `404 model_not_found` (the message lists the ids `/models` lists; image model ids get it too), `502 upstream_unavailable` (the LLM couldn't be reached), `503 no_llm_running` (with `Retry-After: 30` while an LLM is starting). The LLM's own errors reach the client unchanged. |
| `POST /openai/v1/responses` | key | The OpenAI Responses API, through the hub's harness (below). Errors: `400 invalid_request` (a bad body, `input` or item), `400 unsupported_parameter` (`previous_response_id`, `conversation`, `background`), `400 unsupported_tool`, `404 model_not_found`, `502 upstream_unavailable`, `503 no_llm_running`, and `upstream_error` with the upstream's status when the LLM's first round answers an error. |
| Any other method or path under `/openai/v1/` | key | `404 not_found`, naming the three routes above. `GET /openai/v1/chat/completions` and `GET /openai/v1/responses/<id>` get it too. |

The chat route is a plain pass-through: the hub resolves `model` (rule above), checks the key and logs the request. Everything else in the body, the reply (streamed or not) and the LLM's errors go through unchanged, and so do images in messages. The client's headers are never forwarded. A client disconnect stops the generation.

- Public host: `make deploy` detects the address to advertise (Tailscale `100.x`, else LAN, else any), prints it, and the hub reports it to the dashboard. `make deploy PUBLIC_HOST=…` overrides it.
- `files/` (in the config folder) holds the images the Responses harness stores. It is never cleaned up and not served over HTTP.

```sh
KEY=your-64-character-gateway-key
curl -H "Authorization: Bearer $KEY" http://localhost:8888/openai/v1/models
curl -N -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"agora","stream":true,"messages":[{"role":"user","content":"hi"}]}' http://localhost:8888/openai/v1/chat/completions
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"agora","messages":[{"role":"user","content":"hi"}]}' http://localhost:8888/openai/v1/chat/completions
```

### Responses API

`POST /openai/v1/responses` is for apps: chat, photos the model sees, image generation and editing, and web search.

- **Stateless.** Send the whole conversation each time: your earlier input plus every output item you received. `previous_response_id`, `conversation` and `background: true` answer `400 unsupported_parameter`.
- **Fields honoured:** `model` (same rule as chat), `input` (a string or a list of items), `instructions`, `tools` (`function` tools; `web_search`, `web_search_preview` and `image_generation` are accepted and ignored, since the hub's tools are on anyway; any other type answers `400 unsupported_tool`), `tool_choice`, `stream`, `temperature`, `top_p`, `parallel_tool_calls` and `max_output_tokens`. Everything else (`store`, `include`, `metadata`, `text`, `reasoning`, `user`, …) is ignored; the response echoes `metadata`, and `store` is always `false` and `usage` `null`.
- **Input items:** `message` (user, system, developer and assistant), `function_call`, `function_call_output` and `image_generation_call`. `reasoning` and `web_search_call` items are skipped; any other type answers `400 invalid_request`.
- **The harness:**
  - the model sees every image as `[image_N]` plus the image, including the ones it generated, so it can check a result and fix it once;
  - image generation, and editing of up to four images, while an image deployment runs;
  - `web_search`, always: a stub for now, whose item ends `failed`;
  - your own `function` tools, which come back as `function_call` items;
  - a 10-round backstop, and a disconnect stops the turn.
- **Images in:** `input_image` with a data URL, in JPEG, PNG or WebP. Convert HEIC first, and downscale large photos. Other URLs are unavailable to the model.
- **Images out:** `image_generation_call` items carry `result` (base64), `output_format`, `action` (`generate` or `edit`) and `revised_prompt` (the prompt the model wrote). Send them back with `"result": null`: the hub finds the image by its id.
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
curl -N -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"agora","stream":true,"input":"draw a red fox"}' http://localhost:8888/openai/v1/responses
```

### Logs

Every gateway request is logged to stdout as JSON lines: `{"time", "request", "event", …}`, one line per event. Read them with `docker logs -f agora-hub | grep --line-buffered '^{' | jq .` (uvicorn's access lines aren't JSON).

| Event | Logged by | Fields |
| --- | --- | --- |
| `request` | the gateway, each route | `endpoint` (`chat/completions` or `responses`), `body`: the client's body as received |
| `upstream.request` | the chat route; the harness, each round | `round` (harness only), `url`, `body`: what the hub sent |
| `upstream.response` | same | `round` (harness only), `status`, and either `message` (a streamed reply, collected into one assistant message: `content`, `reasoning`, `tool_calls`, `finish_reason`, `usage`) or `body` (any other reply: its JSON, or its text) |
| `upstream.error` | same | `round` (harness only), `error`: the connection failed, or the stream broke while being read |
| `tool.call` | the harness | `call`, `name`, `arguments` |
| `tool.result` | the harness | `call`, `seconds`, and `content` and `images`, or `error` |
| `response` | the Responses route | `response`: the final response object |
| `disconnect` | the gateway | the client left; while streaming, what it got so far: `message` (chat) or `response` (Responses) |
| `error` | the gateway | `status`, `code`, `message` of an error the hub answered |

Request ids: chat requests get `chat_<16 hex>`, Responses requests their response id, `resp_<32 hex>`. Every line of a request carries its id; an error before the body is parsed (a bad key, a bad body) has `"request": null`.

Images are shortened: a base64 data URL keeps its `data:…;base64,` head followed by `<N chars>`, and a long `result` or `b64_json` becomes `<N chars>`. Everything else, including what users send, is logged in full, so treat the host's Docker logs as sensitive. Docker keeps 5 × 20 MB per container, and each `make deploy` starts a fresh log.

APIs the gateway doesn't serve yet: a coding agent that needs one (Codex needs Responses features the hub doesn't have) can use a deployment's own endpoint directly. That endpoint has no gateway key and no `agora` alias.

Planned (not built yet):

- **Stored responses and resumable runs:** `store` and `previous_response_id`, plus `background` turns that the client can resume by response id and the last `sequence_number` it got, or cancel.
- **Web search** for real, behind the same tool.
- **Partial images** while generating, once the image server streams them.
- **Codex** on `/openai/v1/responses`: custom and shell tool types, and encrypted reasoning.
- **Anthropic Messages** at `/anthropic/v1`, through the same harness.
