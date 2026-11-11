import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Config, ConfigError, parse_machine, save_machine
from .controller import Conflict, Controller
from .home_assistant import HomeAssistant
from .integrations import home_assistant_router
from .schedule import run_schedule


class MachineAction(BaseModel):
    action: Literal["on", "off"]


class DeploymentAction(BaseModel):
    action: Literal["start", "stop"]


def create_app(config: Config, controller: Controller, home_assistant: HomeAssistant) -> FastAPI:
    async def authenticate(authorization: Annotated[str | None, Header()] = None):
        token = config.hub.api.admin_token
        if token is None:
            return
        expected = "Bearer " + token.get_secret_value()
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        schedule_task = asyncio.create_task(run_schedule(config, controller))
        try:
            yield
        finally:
            schedule_task.cancel()
            try:
                await schedule_task
            except asyncio.CancelledError:
                pass
            await controller.close()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authenticate)],
        lifespan=lifespan,
    )

    @app.exception_handler(Conflict)
    async def conflict_handler(_request, error: Conflict):
        detail = {"code": error.code, "message": error.message, "blocking": error.blocking}
        return JSONResponse(status_code=409, content={"detail": detail})

    @app.get("/admin/machines/status")
    async def machines_status():
        return await controller.machines_status()

    def save(machine_id: str | None, body: dict) -> dict:
        try:
            return save_machine(config, machine_id, body)
        except ConfigError as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(500, "cannot write machines.yaml") from error

    @app.post("/admin/machines/add", status_code=201)
    async def add_machine(body: dict):
        return save(None, body)

    @app.post("/admin/machines/test")
    async def test_machine(body: dict):
        try:
            machine = parse_machine(config, body)
        except ConfigError as error:
            raise HTTPException(422, str(error)) from error
        return await controller.test_login(machine)

    @app.post("/admin/machines/{machine_id}/update")
    async def update_machine(machine_id: str, body: dict):
        if machine_id not in config.machines:
            raise HTTPException(404, "Machine not found")
        if controller.active and machine_id in controller.active["machines"]:
            raise Conflict("machine_busy", "machine has an active operation", dict(controller.active))
        return save(machine_id, body)

    @app.post("/admin/machines/{machine_id}", status_code=202)
    async def machine_action(machine_id: str, body: MachineAction):
        if machine_id not in config.machines:
            raise HTTPException(404, "Machine not found")
        return await controller.machine_action(machine_id, body.action)

    @app.get("/admin/deployments/status")
    async def deployments_status():
        return await controller.deployments_status()

    @app.post("/admin/deployments/{deployment_id}", status_code=202)
    async def deployment_action(deployment_id: str, body: DeploymentAction):
        if deployment_id not in config.deployments:
            raise HTTPException(404, "Deployment not found")
        return await controller.deployment_action(deployment_id, body.action)

    app.include_router(home_assistant_router(config, home_assistant))
    return app
