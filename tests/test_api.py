import asyncio
import json
import re

import httpx
import yaml
import pytest

from agora_hub.adapters import AdapterError
from agora_hub.api import create_app
from agora_hub.config import load_config
from agora_hub.controller import Controller
from agora_hub.home_assistant import HomeAssistant
from tests.fakes import FakeAdapters


@pytest.fixture
def api(config_dir):
    config = load_config(config_dir)
    config.hub.schedule.enabled = False
    config.hub.timeouts.machine_boot_seconds = 0.02
    config.hub.timeouts.machine_shutdown_seconds = 0.02
    config.hub.timeouts.deployment_start_seconds = 0.02
    config.hub.timeouts.readiness_poll_seconds = 0.001
    adapters = FakeAdapters()
    controller = Controller(config, adapters)
    app = create_app(config, controller, HomeAssistant(config))
    headers = {"Authorization": "Bearer " + "a" * 64}
    return config, adapters, controller, app, headers


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_auth_precedes_validation_and_only_fifteen_routes(api):
    _, _, _, app, headers = api
    async with client(app) as request:
        missing = await request.post("/admin/machines/spark-1", json={"action": "bad"})
        wrong = await request.get("/admin/machines/status", headers={"Authorization": "Bearer wrong"})
        right = await request.get("/admin/machines/status", headers=headers)
        integration = await request.get("/admin/integrations/homeassistant")
        hidden = [await request.get(path, headers=headers) for path in ("/docs", "/redoc", "/openapi.json")]
    assert missing.status_code == wrong.status_code == integration.status_code == 401
    assert right.status_code == 200
    assert all(response.status_code == 404 for response in hidden)
    included = [route for item in app.routes if hasattr(item, "original_router") for route in item.original_router.routes]
    paths = [getattr(route, "path", "") for route in [*app.routes, *included]]
    assert len([path for path in paths if path.startswith("/admin")]) == 15


async def test_empty_admin_token_disables_auth(config_dir):
    path = config_dir / "hub.yaml"
    hub = yaml.safe_load(path.read_text())
    hub["api"]["admin_token"] = ""
    path.write_text(yaml.safe_dump(hub))
    config = load_config(config_dir)
    app = create_app(config, Controller(config, FakeAdapters()), HomeAssistant(config))
    async with client(app) as request:
        none = await request.get("/admin/deployments/status")
        anything = await request.get("/admin/deployments/status", headers={"Authorization": "Bearer whatever"})
    assert config.hub.api.admin_token is None
    assert none.status_code == anything.status_code == 200


async def test_invalid_ids_and_actions(api):
    _, _, _, app, headers = api
    async with client(app) as request:
        machine = await request.post("/admin/machines/missing", json={"action": "on"}, headers=headers)
        deployment = await request.post("/admin/deployments/missing", json={"action": "start"}, headers=headers)
        bad_action = await request.post("/admin/machines/spark-1", json={"action": "start"}, headers=headers)
        bad_body = await request.post("/admin/deployments/pro", json={}, headers=headers)
    assert machine.status_code == deployment.status_code == 404
    assert bad_action.status_code == bad_body.status_code == 422


async def test_partial_status_redacts_secrets_and_reads_do_not_mutate(api):
    config, adapters, controller, app, headers = api
    secret = config.hub.api.admin_token.get_secret_value()
    adapters.reachable["spark-1"] = True
    adapters.failures[("observe", "spark-2")] = AdapterError("connect", f"failed {secret}")
    adapters.failures[("ready", "pro")] = AdapterError("http", f"bad {secret}")
    async with client(app) as request:
        machines = await request.get("/admin/machines/status", headers=headers)
        deployments = await request.get("/admin/deployments/status", headers=headers)
    body = machines.text + deployments.text
    assert machines.status_code == deployments.status_code == 200
    assert machines.json()["machines"]["spark-1"]["reachable"] is True
    assert machines.json()["machines"]["spark-2"]["error"]
    spark = machines.json()["machines"]["spark-1"]
    assert {
        "host": "spark-1.local",
        "user": "agora",
        "port": 22,
        "key_path": None,
        "plug_entity_id": "switch.spark_1",
        "shutdown_command": "sudo -S -p '' poweroff",
        "device": "dgx-spark",
    }.items() <= spark.items()
    assert not [key for key in spark if "password" in key]
    assert secret not in body and "test-ha-token" not in body and "REPLACE_LOCALLY" not in body
    deployment = deployments.json()["deployments"]["pro"]
    assert set(("observed_state", "ready", "endpoint", "error", "operation")) <= set(deployment)
    assert deployment["machines"] == ["spark-1", "spark-2", "spark-3", "spark-4"]
    assert deployment["head"] == "spark-1"
    assert not [call for call in adapters.calls if call[0] in {"wake", "shutdown", "stream"}]
    await controller.close()


async def test_concurrent_posts_accept_one_and_report_conflict(api):
    _, adapters, controller, app, headers = api
    held = asyncio.Event()
    adapters.delays[("observe", "spark-1")] = held
    async with client(app) as request:
        first = asyncio.create_task(request.post("/admin/machines/spark-1", json={"action": "on"}, headers=headers))
        await asyncio.sleep(0)
        second = await request.post("/admin/machines/spark-2", json={"action": "on"}, headers=headers)
        held.set()
        accepted = await first
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "in_progress"
    assert second.status_code == 409
    assert second.json()["detail"] == {
        "code": "busy",
        "message": "another operation is active",
        "blocking": {"action": "on", "target": "spark-1", "machines": ["spark-1"]},
    }
    await controller.task


async def test_start_streams_ndjson_and_output_replays(api):
    config, adapters, controller, app, headers = api
    start = config.deployments["pro"].start_command
    adapters.stream_lines[("spark-1", start)] = [("stdout", "loading weights")]
    adapters.run_hooks[("spark-1", start)] = lambda: adapters.readiness.update(pro=True)
    async with client(app) as request:
        response = await request.post("/admin/deployments/pro", json={"action": "start"}, headers=headers)
        output = await request.get("/admin/deployments/pro/output", headers=headers)
    events = [json.loads(line) for line in response.text.splitlines()]
    texts = [event["text"] for event in events if event["type"] == "line"]
    assert response.status_code == 202
    assert response.headers["content-type"] == "application/x-ndjson"
    assert events[0]["operation"]["status"] == "in_progress"
    assert events[-1] == {"type": "end", "operation": events[-1]["operation"]}
    assert events[-1]["operation"]["status"] == "succeeded"
    assert {f"running {start} on spark-1", "loading weights", "ready"} <= set(texts)
    assert output.status_code == 200
    assert [json.loads(line).get("text") for line in output.text.splitlines()][1:-1] == texts


async def test_machine_off_dependency_conflict_has_remote_state(api):
    _, adapters, _, app, headers = api
    adapters.readiness["pro"] = True
    async with client(app) as request:
        response = await request.post("/admin/machines/spark-1", json={"action": "off"}, headers=headers)
        status = await request.get("/admin/deployments/status", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "machine is running deployment Pro; stop it first"
    assert response.json()["detail"]["blocking"] == {"deployment": "pro"}
    assert status.json()["deployments"]["pro"]["observed_state"] == "running"
    assert not [call for call in adapters.calls if call[0] == "shutdown"]


async def test_disconnect_after_acceptance_does_not_cancel_action(api):
    _, adapters, controller, app, headers = api
    held = asyncio.Event()
    adapters.delays[("wake", "spark-1")] = held
    async with client(app) as request:
        response = await request.post("/admin/machines/spark-1", json={"action": "on"}, headers=headers)
    assert response.status_code == 202 and not controller.task.done()
    held.set()
    await controller.task
    assert controller.machine_operations["spark-1"].status == "succeeded"


async def test_cancelled_pre_acceptance_releases_gate(api):
    _, adapters, controller, app, headers = api
    held = asyncio.Event()
    adapters.delays[("observe", "spark-1")] = held
    async with client(app) as request:
        pending = asyncio.create_task(request.post("/admin/machines/spark-1", json={"action": "on"}, headers=headers))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert controller.active is None
        adapters.delays.clear()
        retry = await request.post("/admin/machines/spark-2", json={"action": "on"}, headers=headers)
    assert retry.status_code == 202
    await controller.task


async def test_new_controller_has_no_operation_memory(api):
    config, adapters, controller, app, headers = api
    adapters.reachable["spark-1"] = True
    async with client(app) as request:
        response = await request.post("/admin/machines/spark-1", json={"action": "off"}, headers=headers)
    assert response.status_code == 202
    await controller.task
    fresh_adapters = FakeAdapters()
    fresh = Controller(config, fresh_adapters)
    fresh_app = create_app(config, fresh, HomeAssistant(config))
    async with client(fresh_app) as request:
        status = await request.get("/admin/machines/status", headers=headers)
    assert all(machine["operation"] is None for machine in status.json()["machines"].values())


NEW_MACHINE = {"name": "Studio", "host": "studio.local", "user": "agora", "password": "hunter2-secret",
               "plug_entity_id": "switch.studio", "device": "mac-studio"}


async def test_add_machine_writes_file_and_status(api):
    config, _, controller, app, headers = api
    async with client(app) as request:
        added = await request.post("/admin/machines/add", json=NEW_MACHINE, headers=headers)
        status = await request.get("/admin/machines/status", headers=headers)
        with_id = await request.post("/admin/machines/add", json={**NEW_MACHINE, "id": "x"}, headers=headers)
        no_name = {key: value for key, value in NEW_MACHINE.items() if key != "name"}
        nameless = await request.post("/admin/machines/add", json=no_name, headers=headers)
    assert added.status_code == 201
    machine_id = added.json()["id"]
    assert re.fullmatch(r"[0-9a-f]{8}", machine_id)
    assert "password" not in added.json() and added.json()["port"] == 22
    stored = yaml.safe_load((config.config_dir / "machines.yaml").read_text())["machines"]
    assert stored[machine_id] == NEW_MACHINE
    assert status.json()["machines"][machine_id]["name"] == "Studio"
    assert with_id.status_code == nameless.status_code == 422
    assert "hunter2-secret" not in with_id.text + nameless.text
    await controller.close()


async def test_machine_login_test_saves_nothing(api):
    config, adapters, controller, app, headers = api
    before = (config.config_dir / "machines.yaml").read_text()
    adapters.failures[("login", "bad.local")] = AdapterError("auth", "Permission denied")
    async with client(app) as request:
        ok = await request.post("/admin/machines/test", json=NEW_MACHINE, headers=headers)
        denied = await request.post("/admin/machines/test", json={**NEW_MACHINE, "host": "bad.local"}, headers=headers)
        no_auth = {key: value for key, value in NEW_MACHINE.items() if key != "password"}
        invalid = await request.post("/admin/machines/test", json=no_auth, headers=headers)
    assert ok.json() == {"reachable": True, "error": None}
    assert denied.json() == {"reachable": True, "error": "auth: Permission denied"}
    assert invalid.status_code == 422 and "password or key_path is required" in invalid.json()["detail"]
    assert ("login", "studio.local") in adapters.calls
    assert (config.config_dir / "machines.yaml").read_text() == before and "test" not in config.machines
    await controller.close()


async def test_update_machine_merges_onto_file(api):
    config, _, _, app, headers = api
    async with client(app) as request:
        updated = await request.post("/admin/machines/spark-2/update", json={"host": "10.0.0.2"}, headers=headers)
        null_name = await request.post("/admin/machines/spark-2/update", json={"name": None}, headers=headers)
        empty_name = await request.post("/admin/machines/spark-2/update", json={"name": " "}, headers=headers)
        missing = await request.post("/admin/machines/missing/update", json={"host": "x"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["host"] == config.machines["spark-2"].host == "10.0.0.2"
    stored = yaml.safe_load((config.config_dir / "machines.yaml").read_text())["machines"]["spark-2"]
    assert stored["host"] == "10.0.0.2" and stored["password"] == "REPLACE_LOCALLY"
    assert null_name.status_code == empty_name.status_code == 422
    assert config.machines["spark-2"].name == "Spark 2"
    assert missing.status_code == 404


async def test_update_busy_machine_conflicts(api):
    _, adapters, controller, app, headers = api
    held = asyncio.Event()
    adapters.delays[("wake", "spark-1")] = held
    async with client(app) as request:
        started = await request.post("/admin/machines/spark-1", json={"action": "on"}, headers=headers)
        busy = await request.post("/admin/machines/spark-1/update", json={"host": "x"}, headers=headers)
    held.set()
    await controller.task
    assert started.status_code == 202
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "machine_busy"


NEW_DEPLOYMENT = {"name": "Mini", "machines": ["spark-1"], "head": "spark-1", "start_command": "/opt/mini/start.sh --tp 1",
                  "stop_command": "/opt/mini/stop.sh", "endpoint": "http://spark-1.local:9000/v1/models"}


async def test_add_and_update_deployment_write_file(api):
    config, _, _, app, headers = api
    async with client(app) as request:
        added = await request.post("/admin/deployments/add", json=NEW_DEPLOYMENT, headers=headers)
        deployment_id = added.json()["id"]
        status = await request.get("/admin/deployments/status", headers=headers)
        updated = await request.post(f"/admin/deployments/{deployment_id}/update", json={"name": "Tiny"}, headers=headers)
    assert added.status_code == 201 and added.json() == {"id": deployment_id, **NEW_DEPLOYMENT}
    assert status.json()["deployments"][deployment_id]["name"] == "Mini"
    assert updated.status_code == 200 and config.deployments[deployment_id].name == "Tiny"
    stored = yaml.safe_load((config.config_dir / "deployments.yaml").read_text())["deployments"]
    assert stored[deployment_id] == {**NEW_DEPLOYMENT, "name": "Tiny"} and "pro" in stored


async def test_update_deployment_rejects_invalid_and_busy(api):
    config, adapters, controller, app, headers = api
    held = asyncio.Event()
    adapters.delays[("stream", "spark-1")] = held
    async with client(app) as request:
        invalid = await request.post("/admin/deployments/pro/update", json={"head": "spark-9"}, headers=headers)
        await controller.deployment_action("pro", "start")
        busy = await request.post("/admin/deployments/pro/update", json={"name": "X"}, headers=headers)
    held.set()
    await controller.task
    assert invalid.status_code == 422 and config.deployments["pro"].head == "spark-1"
    assert busy.status_code == 409
    assert busy.json()["detail"] == {"code": "deployment_busy", "message": "deployment has an active operation",
                                     "blocking": {"action": "start", "target": "pro", "machines": config.deployments["pro"].machines}}
