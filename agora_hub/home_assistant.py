import asyncio

import httpx

from .config import Config


class HomeAssistantError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class HomeAssistant:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def _request(self, method: str, path: str, body: dict | None = None):
        settings = self.config.hub.home_assistant
        if settings.token is None:
            raise HomeAssistantError("not_configured", "home assistant token is not set")
        try:
            async with httpx.AsyncClient(
                base_url=f"http://{settings.host}:{settings.port}",
                headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
                timeout=self.config.hub.timeouts.http_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, json=body)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HomeAssistantError("http", str(error) or type(error).__name__) from error

    async def switches(self) -> list[dict]:
        states = await self._request("GET", "/api/states")
        return [
            {
                "entity_id": entity["entity_id"],
                "name": entity.get("attributes", {}).get("friendly_name") or entity["entity_id"],
                "state": entity["state"],
                "machines": [
                    machine_id
                    for machine_id, machine in self.config.machines.items()
                    if machine.plug_entity_id == entity["entity_id"]
                ],
            }
            for entity in sorted(states, key=lambda entity: entity["entity_id"])
            if entity["entity_id"].startswith("switch.")
        ]

    async def set_switches(self, changes: dict[str, str]) -> None:
        for service in ("off", "on"):
            if entity_ids := [entity_id for entity_id, state in changes.items() if state == service]:
                await self._request("POST", f"/api/services/switch/turn_{service}", {"entity_id": entity_ids})

    async def power_cycle(self, entity_id: str) -> None:
        await self.set_switches({entity_id: "off"})
        await asyncio.sleep(self.config.hub.home_assistant.power_cycle_delay_seconds)
        await self.set_switches({entity_id: "on"})
