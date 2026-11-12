import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import Config, ConfigError, parse_machine, save_deployment, save_machine
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

    def save(machine_id: str | None, body: dict, kind: str = "machines") -> dict:
        try:
            return (save_machine if kind == "machines" else save_deployment)(config, machine_id, body)
        except ConfigError as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(500, f"cannot write {kind}.yaml") from error

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

    def stream(deployment_id: str, status_code: int) -> StreamingResponse:
        lines = (json.dumps(event) + "\n" async for event in controller.follow(deployment_id))
        return StreamingResponse(lines, status_code, media_type="application/x-ndjson")

    @app.post("/admin/deployments/add", status_code=201)
    async def add_deployment(body: dict):
        return save(None, body, "deployments")

    @app.post("/admin/deployments/{deployment_id}/update")
    async def update_deployment(deployment_id: str, body: dict):
        if deployment_id not in config.deployments:
            raise HTTPException(404, "Deployment not found")
        operation = controller.deployment_operations.get(deployment_id)
        if (controller.active and controller.active["target"] == deployment_id) or (
            operation and operation.status == "in_progress"
        ):
            raise Conflict("deployment_busy", "deployment has an active operation", dict(controller.active or {}))
        return save(deployment_id, body, "deployments")

    @app.post("/admin/deployments/{deployment_id}", status_code=202)
    async def deployment_action(deployment_id: str, body: DeploymentAction):
        if deployment_id not in config.deployments:
            raise HTTPException(404, "Deployment not found")
        await controller.deployment_action(deployment_id, body.action)
        return stream(deployment_id, 202)

    @app.get("/admin/deployments/{deployment_id}/output")
    async def deployment_output(deployment_id: str):
        if deployment_id not in config.deployments:
            raise HTTPException(404, "Deployment not found")
        if deployment_id not in controller.deployment_operations:
            raise HTTPException(404, "No operation")
        return stream(deployment_id, 200)

    app.include_router(home_assistant_router(config, home_assistant))
    return app
