from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, constr

from .config import Config, ConfigError, save_home_assistant
from .controller import Conflict
from .home_assistant import HomeAssistant, HomeAssistantError


class SwitchChange(BaseModel):
    entity_id: constr(pattern=r"^switch\.[a-z0-9_]+$")
    state: Literal["on", "off"]


def home_assistant_router(config: Config, home_assistant: HomeAssistant) -> APIRouter:
    router = APIRouter(prefix="/admin/integrations/homeassistant")

    def view() -> dict:
        settings = config.hub.home_assistant
        return {
            "host": settings.host,
            "port": settings.port,
            "token_set": settings.token is not None,
            "power_cycle_delay_seconds": settings.power_cycle_delay_seconds,
        }

    async def switches(changes: dict[str, str]) -> dict:
        try:
            await home_assistant.set_switches(changes)
            return {"switches": await home_assistant.switches()}
        except HomeAssistantError as error:
            if error.kind == "not_configured":
                raise Conflict("home_assistant_not_configured", str(error)) from error
            raise HTTPException(502, f"{error.kind}: {error}") from error

    @router.get("")
    async def get_settings():
        return view()

    @router.post("")
    async def save_settings(body: dict):
        try:
            save_home_assistant(config, body)
        except ConfigError as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(500, "cannot write hub.yaml") from error
        return view()

    @router.get("/switches")
    async def get_switches():
        return await switches({})

    @router.post("/switches")
    async def set_switches(body: list[SwitchChange]):
        return await switches({item.entity_id: item.state for item in body})

    return router
