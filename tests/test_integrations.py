import json

import httpx
import pytest
import yaml

from agora_hub.api import create_app
from agora_hub.config import load_config
from agora_hub.controller import Controller
from agora_hub.home_assistant import HomeAssistant
from tests.fakes import FakeAdapters

STATES = [
    {"entity_id": "switch.spark_1", "state": "on", "attributes": {"friendly_name": "Spark 1 plug"}},
    {"entity_id": "switch.lamp", "state": "unavailable", "attributes": {}},
    {"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk"}},
]
PATH = "/admin/integrations/homeassistant"


@pytest.fixture
def ha(config_dir):
    requests, failure = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        if failure:
            raise httpx.ConnectError("connection refused", request=request)
        requests.append(request)
        return httpx.Response(200, json=STATES if request.url.path == "/api/states" else [])

    config = load_config(config_dir)
    home_assistant = HomeAssistant(config, httpx.MockTransport(handler))
    app = create_app(config, Controller(config, FakeAdapters()), home_assistant)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer " + "a" * 64},
    )
    return config_dir, client, requests, failure


async def test_settings_view_save_and_unset(ha):
    config_dir, client, requests, _ = ha
    async with client:
        view = await client.get(PATH)
        saved = await client.post(PATH, json={"host": "10.0.0.9", "port": 8124})
        saved_text = (config_dir / "hub.yaml").read_text()
        await client.get(PATH + "/switches")
        bad = await client.post(PATH, json={"host": "http://x"})
        after_bad = (config_dir / "hub.yaml").read_text()
        unset = await client.post(PATH, json={"token": None})
        count = len(requests)
        not_configured = await client.get(PATH + "/switches")
    assert view.json() == {"host": "homeassistant.local", "port": 8123, "token_set": True,
                           "power_cycle_delay_seconds": 5}
    assert "test-ha-token" not in view.text and "test-ha-token" not in saved.text
    assert saved.status_code == 200 and saved.json()["host"] == "10.0.0.9"
    hub = yaml.safe_load(saved_text)
    assert hub["home_assistant"]["host"] == "10.0.0.9" and hub["home_assistant"]["port"] == 8124
    assert hub["home_assistant"]["token"] == "test-ha-token" and hub["api"]["admin_token"] == "a" * 64
    assert requests[0].url.host == "10.0.0.9" and requests[0].url.port == 8124
    assert bad.status_code == 422 and after_bad == saved_text
    assert unset.json()["token_set"] is False
    assert not_configured.status_code == 409
    assert not_configured.json()["detail"]["code"] == "home_assistant_not_configured"
    assert len(requests) == count


async def test_list_switches(ha):
    _, client, requests, _ = ha
    async with client:
        response = await client.get(PATH + "/switches")
    assert response.json() == {"switches": [
        {"entity_id": "switch.lamp", "name": "switch.lamp", "state": "unavailable", "machines": []},
        {"entity_id": "switch.spark_1", "name": "Spark 1 plug", "state": "on", "machines": ["spark-1"]},
    ]}
    assert requests[0].headers["Authorization"] == "Bearer test-ha-token"


async def test_set_switches_batches_off_then_on(ha):
    _, client, requests, _ = ha
    body = [{"entity_id": "switch.a", "state": "on"}, {"entity_id": "switch.b", "state": "off"},
            {"entity_id": "switch.c", "state": "off"}]
    async with client:
        response = await client.post(PATH + "/switches", json=body)
        count = len(requests)
        light = await client.post(PATH + "/switches", json=[{"entity_id": "light.desk", "state": "on"}])
        toggle = await client.post(PATH + "/switches", json=[{"entity_id": "switch.a", "state": "toggle"}])
    assert response.status_code == 200 and len(response.json()["switches"]) == 2
    calls = [(r.method, r.url.path, json.loads(r.content) if r.content else None) for r in requests]
    assert calls == [
        ("POST", "/api/services/switch/turn_off", {"entity_id": ["switch.b", "switch.c"]}),
        ("POST", "/api/services/switch/turn_on", {"entity_id": ["switch.a"]}),
        ("GET", "/api/states", None),
    ]
    assert light.status_code == toggle.status_code == 422 and len(requests) == count


async def test_unreachable_home_assistant_is_502(ha):
    _, client, _, failure = ha
    failure.append(True)
    async with client:
        response = await client.get(PATH + "/switches")
    assert response.status_code == 502 and response.json()["detail"].startswith("http: ")
