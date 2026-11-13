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

    async def create_process(self, command, **kwargs):
        self.runs.append((command, kwargs))
        return self.process

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
        return httpx.Response(200, json=[])

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
        {"entity_id": ["switch.spark_1"]},
        {"entity_id": ["switch.spark_1"]},
    ]
    assert all(request.url.netloc == b"homeassistant.local:8123" for request in requests)
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


async def test_readiness_gets_models_under_the_endpoint_and_returns_ids(config):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen3-30b"}, {"id": "qwen3-30b-lora"}]})

    assert await make_adapters(config, httpx.MockTransport(handler)).ready("pro") == ["qwen3-30b", "qwen3-30b-lora"]
    assert urls == ["http://spark-1.local:8000/v1/models"]


@pytest.mark.parametrize("status, succeeds", [(204, True), (404, False)])
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


@pytest.mark.parametrize(
    "error, kind",
    [
        (asyncssh.PermissionDenied("bad password"), "auth"),
        (OSError("no route"), "connect"),
        (TimeoutError(), "connect"),
        (asyncssh.HostKeyNotVerifiable("untrusted"), "host_key"),
    ],
)
async def test_ssh_error_kinds(config, monkeypatch, error, kind):
    install_ssh(monkeypatch, error=error)
    with pytest.raises(AdapterError) as caught:
        await make_adapters(config).observe("spark-1")
    assert caught.value.kind == kind
    assert str(caught.value)


async def test_ssh_connect_timeout_says_so(config, monkeypatch):
    install_ssh(monkeypatch, error=TimeoutError())
    with pytest.raises(AdapterError) as caught:
        await make_adapters(config).observe("spark-1")
    assert str(caught.value) == "timed out after 10s"


async def test_readiness_timeout_has_a_message(config):
    def handler(request):
        raise httpx.ConnectTimeout("", request=request)

    with pytest.raises(AdapterError) as caught:
        await make_adapters(config, httpx.MockTransport(handler)).ready("pro")
    assert str(caught.value) == "ConnectTimeout"


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


class FakeProcess:
    def __init__(self, stdout, stderr, exit_status):
        async def lines(items):
            for item in items:
                await asyncio.sleep(0)
                yield item

        self.stdout, self.stderr, self.exit_status = lines(stdout), lines(stderr), exit_status
        self.stdin = SimpleNamespace(write_eof=lambda: setattr(self, "eof", True))

    async def wait_closed(self):
        pass


async def test_stream_delivers_redacted_lines_and_exit_status(config, monkeypatch):
    connection = FakeConnection()
    connection.process = FakeProcess(["loading REPLACE_LOCALLY\n", "done\r\n"], ["warn test-ha-token\n"], 3)
    install_ssh(monkeypatch, connection)
    lines = []
    status = await make_adapters(config).stream("spark-1", "/start.sh", timeout=1, on_line=lambda *line: lines.append(line))
    assert status == 3
    assert connection.runs == [("/start.sh", {"encoding": "utf-8", "errors": "replace"})]
    assert connection.process.eof and connection.closed
    assert sorted(lines) == [("stderr", "warn ***"), ("stdout", "done"), ("stdout", "loading ***")]
