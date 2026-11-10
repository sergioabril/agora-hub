import asyncio
import re

import httpx
import yaml
import pytest

from agora_hub.adapters import AdapterError
from agora_hub.api import create_app
from agora_hub.config import load_config
from agora_hub.controller import Controller
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
    adapters.states["pro"] = "stopped"
    controller = Controller(config, adapters)
    app = create_app(config, controller)
    headers = {"Authorization": "Bearer " + "a" * 64}
    return config, adapters, controller, app, headers


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_auth_precedes_validation_and_only_six_routes(api):
    _, _, _, app, headers = api
    async with client(app) as request:
        missing = await request.post("/admin/machines/spark-1", json={"action": "bad"})
        wrong = await request.get("/admin/machines/status", headers={"Authorization": "Bearer wrong"})
        right = await request.get("/admin/machines/status", headers=headers)
        hidden = [await request.get(path, headers=headers) for path in ("/docs", "/redoc", "/openapi.json")]
    assert missing.status_code == wrong.status_code == 401
    assert right.status_code == 200
    assert all(response.status_code == 404 for response in hidden)
    assert len([route for route in app.routes if getattr(route, "path", "").startswith("/admin")]) == 6


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
    secret = config.admin_token.get_secret_value()
    adapters.reachable["spark-1"] = True
    adapters.failures[("observe", "spark-2")] = AdapterError("connect", f"failed {secret}")
    adapters.failures[("deployment_status", "pro")] = AdapterError("command", f"bad {secret}")
    async with client(app) as request:
        machines = await request.get("/admin/machines/status", headers=headers)
        deployments = await request.get("/admin/deployments/status", headers=headers)
    body = machines.text + deployments.text
    assert machines.status_code == deployments.status_code == 200
    assert machines.json()["machines"]["spark-1"]["reachable"] is True
    assert machines.json()["machines"]["spark-2"]["error"]
    assert {"host": "spark-1.local", "device": "dgx-spark"}.items() <= machines.json()["machines"]["spark-1"].items()
    assert secret not in body and "test-ha-token" not in body and "REPLACE_LOCALLY" not in body
    deployment = deployments.json()["deployments"]["pro"]
    assert set(("observed_state", "ready", "endpoint", "errors", "operation")) <= set(deployment)
    assert deployment["machines"] == ["spark-1", "spark-2", "spark-3", "spark-4"]
    assert deployment["head"] == "spark-1"
    assert not [call for call in adapters.calls if call[0] in {"wake", "shutdown", "run"}]
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


async def test_machine_off_dependency_conflict_has_remote_state(api):
    _, adapters, _, app, headers = api
    adapters.states["pro"] = "running"
    adapters.readiness["pro"] = True
    async with client(app) as request:
        response = await request.post("/admin/machines/spark-1", json={"action": "off"}, headers=headers)
        status = await request.get("/admin/deployments/status", headers=headers)
    assert response.status_code == 409
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
    fresh_app = create_app(config, fresh)
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
