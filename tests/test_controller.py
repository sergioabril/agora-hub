import asyncio

import pytest

from agora_hub.adapters import AdapterError
from agora_hub.config import load_config
from agora_hub.controller import Conflict, Controller
from tests.fakes import FakeAdapters


@pytest.fixture
def setup(config_dir):
    config = load_config(config_dir)
    timeouts = config.hub.timeouts
    timeouts.machine_boot_seconds = 0.02
    timeouts.machine_shutdown_seconds = 0.02
    timeouts.deployment_start_seconds = 0.02
    timeouts.deployment_stop_seconds = 0.02
    timeouts.readiness_poll_seconds = 0.001
    adapters = FakeAdapters()
    adapters.readiness["pro"] = False
    return config, adapters, Controller(config, adapters)


def run_calls(adapters, command):
    return [call for call in adapters.calls if call[0] == "stream" and call[2] == command]


@pytest.mark.asyncio
async def test_simultaneous_duplicate_start_runs_one_command(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    gate = asyncio.Event()
    adapters.delays[("ready", "pro")] = gate
    adapters.run_hooks[("spark-1", start)] = lambda: adapters.readiness.update(pro=True)

    first = asyncio.create_task(controller.deployment_action("pro", "start"))
    await asyncio.sleep(0)
    with pytest.raises(Conflict, match="another operation") as caught:
        await controller.deployment_action("pro", "start")
    assert caught.value.code == "busy"
    gate.set()
    await first
    await controller.task

    assert len(run_calls(adapters, start)) == 1


@pytest.mark.asyncio
async def test_machine_off_during_start_is_rejected_without_shutdown(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    adapters.reachable.update({key: True for key in config.machines})
    gate = asyncio.Event()
    adapters.delays[("stream", "spark-1", start)] = gate
    await controller.deployment_action("pro", "start")
    while not run_calls(adapters, start):
        await asyncio.sleep(0)

    with pytest.raises(Conflict) as caught:
        await controller.machine_action("spark-2", "off")
    assert caught.value.code == "deployment_active"
    assert not [call for call in adapters.calls if call[0] == "shutdown"]
    gate.set()
    await controller.task


@pytest.mark.asyncio
async def test_already_satisfied_and_remote_restart_states_conflict(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    adapters.reachable["spark-1"] = True
    with pytest.raises(Conflict) as machine:
        await controller.machine_action("spark-1", "on")
    assert machine.value.code == "machine_on"

    adapters.readiness["pro"] = True
    with pytest.raises(Conflict) as deployment:
        await Controller(config, adapters).deployment_action("pro", "start")
    assert deployment.value.code == "deployment_active"
    assert not run_calls(adapters, start)


@pytest.mark.asyncio
async def test_refresh_does_not_replace_in_progress_operation(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    adapters.reachable.update({key: True for key in config.machines})
    gate = asyncio.Event()
    adapters.delays[("stream", "spark-1", start)] = gate
    await controller.deployment_action("pro", "start")
    status = await controller.deployments_status()

    assert status["deployments"]["pro"]["operation"]["status"] == "in_progress"
    gate.set()
    await controller.task


@pytest.mark.asyncio
async def test_partial_cluster_wake_failure_is_recorded(setup):
    _, adapters, controller = setup
    adapters.failures[("wake", "spark-2")] = AdapterError("http", "plug failed")
    await controller.deployment_action("pro", "start")
    await controller.task
    status = await controller.machines_status()

    assert controller.deployment_operations["pro"].status == "failed"
    assert status["machines"]["spark-2"]["operation"]["status"] == "failed"
    assert "spark-2" in controller.deployment_operations["pro"].error
    assert {call[1] for call in adapters.calls if call[0] == "wake"} == {"spark-1", "spark-2", "spark-3", "spark-4"}


@pytest.mark.asyncio
async def test_timeout_error_persists_and_explicit_retry_replaces_it(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    adapters.reachable.update({key: True for key in config.machines})
    adapters.failures[("stream", "spark-1", start)] = AdapterError("timeout", "start command timed out")
    await controller.deployment_action("pro", "start")
    await controller.task
    first = (await controller.deployments_status())["deployments"]["pro"]["operation"]
    second = (await controller.deployments_status())["deployments"]["pro"]["operation"]

    assert first["status"] == second["status"] == "failed"
    assert "timed out" in second["error"]
    assert len(run_calls(adapters, start)) == 1

    del adapters.failures[("stream", "spark-1", start)]
    adapters.run_hooks[("spark-1", start)] = lambda: adapters.readiness.update(pro=True)
    await controller.deployment_action("pro", "start")
    await controller.task
    retried = (await controller.deployments_status())["deployments"]["pro"]["operation"]
    assert retried["status"] == "succeeded"
    assert retried["error"] is None
    assert len(run_calls(adapters, start)) == 2


@pytest.mark.asyncio
async def test_schedule_shutdown_cancels_start_then_stops_and_shuts_down(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    stop = config.deployments["pro"].stop_command
    adapters.reachable.update({key: True for key in config.machines})
    gate = asyncio.Event()
    adapters.delays[("stream", "spark-1", start)] = gate
    await controller.deployment_action("pro", "start")
    while not run_calls(adapters, start):
        await asyncio.sleep(0)
    cancelled = controller.deployment_operations["pro"]

    await controller.schedule_shutdown()
    await controller.task

    assert cancelled.status == "cancelled"
    assert run_calls(adapters, stop)
    assert {call[1] for call in adapters.calls if call[0] == "shutdown"} == set(config.hub.schedule.machines)
    assert controller.machine_operations["spark-1"].status == "succeeded"


@pytest.mark.asyncio
async def test_failing_start_script_skips_endpoint(setup):
    config, adapters, controller = setup
    adapters.reachable.update({key: True for key in config.machines})
    adapters.stream_exits[("spark-1", config.deployments["pro"].start_command)] = 3
    await controller.deployment_action("pro", "start")
    calls = len(adapters.calls)
    await controller.task

    assert controller.deployment_operations["pro"].error == "start command exited 3"
    assert ("ready", "pro") not in adapters.calls[calls:]


@pytest.mark.asyncio
async def test_closing_follower_leaves_start_running(setup):
    config, adapters, controller = setup
    start = config.deployments["pro"].start_command
    adapters.reachable.update({key: True for key in config.machines})
    gate = asyncio.Event()
    adapters.delays[("stream", "spark-1", start)] = gate
    adapters.run_hooks[("spark-1", start)] = lambda: adapters.readiness.update(pro=True)
    await controller.deployment_action("pro", "start")
    follower = controller.follow("pro")
    assert (await anext(follower))["operation"]["status"] == "in_progress"
    assert (await anext(follower))["text"] == f"running {start} on spark-1"
    await follower.aclose()

    gate.set()
    await controller.task
    assert controller.deployment_operations["pro"].status == "succeeded"
