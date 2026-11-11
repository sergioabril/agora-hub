import asyncio
import json
from types import SimpleNamespace

import asyncssh
import httpx
import pytest

from agora_hub.adapters import AdapterError, make_adapters
from agora_hub.config import load_config


class FakeConnection:
    def __init__(self, result=None, error=None):
        self.result = result or SimpleNamespace(exit_status=0, stdout="", stderr="")
        self.error = error
        self.runs = []
        self.closed = False

    async def run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.fixture
def config(config_dir):
    return load_config(config_dir)


def install_ssh(monkeypatch, connection=None, error=None):
    calls = []

    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        if error:
            raise error
        return connection

    async def get_server_host_key(host, port):
        return HOST_KEY

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(asyncssh, "get_server_host_key", get_server_host_key)
    return calls


HOST_KEY = asyncssh.generate_private_key("ssh-ed25519")


async def test_unknown_host_key_is_saved_once_and_known_hosts_are_not_rescanned(config, monkeypatch):
    install_ssh(monkeypatch, FakeConnection())
    scans = []

    async def get_server_host_key(host, port):
        scans.append((host, port))
        return HOST_KEY

    monkeypatch.setattr(asyncssh, "get_server_host_key", get_server_host_key)
    known_hosts = config.hub.ssh.known_hosts_path
    other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    known_hosts.write_text(f"spark-2.local {other}")
    adapters = make_adapters(config)
    await adapters.observe("spark-1")
    await adapters.observe("spark-1")
    await adapters.observe("spark-2")
    assert scans == [("spark-1.local", 22)]
    assert known_hosts.read_text() == f"spark-2.local {other}spark-1.local {HOST_KEY.export_public_key().decode()}"


async def test_wake_posts_off_then_on_with_auth_body_and_delay(config, monkeypatch):
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    adapters = make_adapters(config, httpx.MockTransport(handler))
    await adapters.wake("spark-1")

    assert [request.url.path for request in requests] == [
        "/api/services/switch/turn_off",
        "/api/services/switch/turn_on",
    ]
    assert [json.loads(request.content) for request in requests] == [
        {"entity_id": "switch.spark_1"},
        {"entity_id": "switch.spark_1"},
    ]
    assert all(r.headers["Authorization"] == "Bearer test-ha-token" for r in requests)
    assert delays == [5]


async def test_wake_failure_stops_before_turn_on(config, monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(503)

    adapters = make_adapters(config, httpx.MockTransport(handler))
    with pytest.raises(AdapterError, match="503") as caught:
        await adapters.wake("spark-1")
    assert caught.value.kind == "http"
    assert paths == ["/api/services/switch/turn_off"]


@pytest.mark.parametrize("status, succeeds", [(200, True), (503, False)])
async def test_readiness_status(config, status, succeeds):
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    adapters = make_adapters(config, transport)
    if succeeds:
        await adapters.ready("pro")
    else:
        with pytest.raises(AdapterError) as caught:
            await adapters.ready("pro")
        assert caught.value.kind == "http"


async def test_readiness_connection_error_is_redacted(config):
    def handler(request):
        raise httpx.ConnectError("failed with test-ha-token", request=request)

    adapters = make_adapters(config, httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as caught:
        await adapters.ready("pro")
    assert caught.value.kind == "http"
    assert "test-ha-token" not in str(caught.value)
    assert "***" in str(caught.value)


async def test_status_script_returns_valid_state(config, monkeypatch):
    connection = FakeConnection(SimpleNamespace(exit_status=0, stdout=" running\n", stderr=""))
    install_ssh(monkeypatch, connection)
    assert await make_adapters(config).deployment_status("pro") == "running"
    command, kwargs = connection.runs[0]
    assert command == "cd /home/agora/pro && bash status.sh"
    assert kwargs["timeout"] == 10


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(exit_status=0, stdout="healthy\n", stderr=""),
        SimpleNamespace(exit_status=4, stdout="", stderr="broken"),
    ],
)
async def test_status_script_rejects_invalid_output_and_nonzero(config, monkeypatch, result):
    install_ssh(monkeypatch, FakeConnection(result))
    with pytest.raises(AdapterError) as caught:
        await make_adapters(config).deployment_status("pro")
    assert caught.value.kind == "command"


async def test_status_script_timeout(config, monkeypatch):
    install_ssh(monkeypatch, FakeConnection(error=asyncio.TimeoutError()))
    with pytest.raises(AdapterError) as caught:
        await make_adapters(config).deployment_status("pro")
    assert caught.value.kind == "timeout"


@pytest.mark.parametrize(
    "error, kind",
    [
        (asyncssh.PermissionDenied("bad password"), "auth"),
        (OSError("no route"), "connect"),
        (asyncssh.HostKeyNotVerifiable("untrusted"), "host_key"),
    ],
)
async def test_ssh_error_kinds(config, monkeypatch, error, kind):
    install_ssh(monkeypatch, error=error)
    with pytest.raises(AdapterError) as caught:
        await make_adapters(config).observe("spark-1")
    assert caught.value.kind == kind


async def test_shutdown_sends_sudo_password_only_on_stdin(config, monkeypatch):
    connection = FakeConnection(
        SimpleNamespace(
            exit_status=0,
            stdout="REPLACE_LOCALLY output",
            stderr="test-ha-token warning",
        )
    )
    calls = install_ssh(monkeypatch, connection)
    result = await make_adapters(config).shutdown("spark-1")

    command, kwargs = connection.runs[0]
    assert command == "sudo -S -p '' poweroff"
    assert "REPLACE_LOCALLY" not in command
    assert kwargs["input"] == "REPLACE_LOCALLY\n"
    assert result.stdout == "*** output"
    assert result.stderr == "*** warning"
    assert calls[0][1]["known_hosts"] == str(config.hub.ssh.known_hosts_path)
    assert calls[0][1]["connect_timeout"] == 10
    assert calls[0][1]["password"] == "REPLACE_LOCALLY"


async def test_shutdown_accepts_disconnect_after_command(config, monkeypatch):
    disconnect = asyncssh.DisconnectError(10, "power lost")
    install_ssh(monkeypatch, FakeConnection(error=disconnect))
    result = await make_adapters(config).shutdown("spark-1")
    assert result.exit_status == -1


async def test_run_disconnect_is_unknown_and_redacted(config, monkeypatch):
    error = asyncssh.DisconnectError(10, "lost REPLACE_LOCALLY")
    install_ssh(monkeypatch, FakeConnection(error=error))
    result = await make_adapters(config).run("spark-1", "thing", timeout=1)
    assert result.exit_status == -1
    assert result.stdout == ""
    assert "REPLACE_LOCALLY" not in result.stderr
    assert "***" in result.stderr


async def test_run_without_exit_status_is_unknown(config, monkeypatch):
    result = SimpleNamespace(
        exit_status=None, stdout="partial REPLACE_LOCALLY", stderr=""
    )
    install_ssh(monkeypatch, FakeConnection(result))
    outcome = await make_adapters(config).run("spark-1", "thing", timeout=1)
    assert outcome.exit_status == -1
    assert outcome.stdout == "partial ***"
