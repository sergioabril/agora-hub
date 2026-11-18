import asyncio
import json
import secrets
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import Config
from .controller import Controller
from .harness import BadRequest, Harness, UpstreamDown
from .tools import MEDIA_TYPES, FileStore, Tools


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


def create_gateway(config: Config, controller: Controller, transport=None) -> FastAPI:
    client = httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(None, connect=config.hub.timeouts.http_seconds)
    )

    async def authenticate(authorization: Annotated[str | None, Header()] = None):
        keys = config.hub.gateway.api_keys
        if keys and not any(
            secrets.compare_digest((authorization or "").encode(), f"Bearer {key.get_secret_value()}".encode())
            for key in keys
        ):
            raise GatewayError(401, "invalid_api_key", "invalid gateway key")

    def resolve(model: str) -> tuple[str, str]:
        llms = controller.serving("llm")
        if not llms:
            if name := controller.starting("llm"):
                raise GatewayError(503, "no_llm_running", f"llm deployment {name} is starting", {"Retry-After": "30"})
            raise GatewayError(503, "no_llm_running", "no llm deployment is running")
        if model == "agora":
            return llms[0][0], llms[0][1][0]
        if match := next((key for key, models in llms if model in models), None):
            return match, model
        ids = ", ".join(model_id for _, models in llms for model_id in models)
        raise GatewayError(404, "model_not_found", f"model {model} is not served; use agora or one of: {ids}")

    def image_target() -> tuple[str, str] | None:
        if serving := controller.serving("image"):
            return str(config.deployments[serving[0][0]].endpoint).rstrip("/"), serving[0][1][0]
        return None

    files = FileStore(config.config_dir / "files")
    harness = Harness(client, Tools(client, files, image_target, config.hub.timeouts.image_seconds), files)

    async def parse(request: Request) -> dict:
        try:
            body = json.loads(await request.body())
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise GatewayError(400, "invalid_request", "body must be a JSON object with a string model")
        return body

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter(dependencies=[Depends(authenticate)])

    @app.exception_handler(GatewayError)
    async def gateway_error(_request, error: GatewayError):
        kind = "invalid_request_error" if error.status < 500 else "server_error"
        content = {"error": {"message": error.message, "type": kind, "code": error.code}}
        return JSONResponse(content, error.status, error.headers)

    @router.get("/openai/v1/models")
    async def models():
        llms = controller.serving("llm")
        data = [{"id": "agora", "object": "model", "created": 0, "owned_by": "agora"}] if llms else []
        for deployment_id, ids in llms:
            data += [{"id": model_id, "object": "model", "created": 0, "owned_by": deployment_id} for model_id in ids]
        return {"object": "list", "data": data}

    @router.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        body = await parse(request)
        deployment_id, body["model"] = resolve(body["model"])
        deployment, gateway = config.deployments[deployment_id], config.hub.gateway
        base_url = f"http://{gateway.public_host}:{gateway.port}/" if gateway.public_host else str(request.base_url)
        url = str(deployment.endpoint).rstrip("/") + "/chat/completions"
        # FastAPI would run a non-streaming turn to the end after the client left: race it against the disconnect.
        turn = asyncio.ensure_future(harness.chat(body, url, base_url))
        gone = asyncio.ensure_future(request.receive())
        await asyncio.wait({turn, gone}, return_when=asyncio.FIRST_COMPLETED)
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            return Response(status_code=499)  # never delivered: the client has gone
        gone.cancel()
        try:
            return turn.result()
        except BadRequest as error:
            raise GatewayError(400, "invalid_request", str(error))
        except UpstreamDown as error:
            raise GatewayError(502, "upstream_unavailable", f"{deployment.name}: {error}")

    @router.api_route("/openai/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def not_served(path: str, request: Request):
        raise GatewayError(404, "not_found", f"{request.method} /openai/v1/{path} is not served; the gateway serves "
                                             "GET /openai/v1/models and POST /openai/v1/chat/completions")

    @app.get("/files/{name}")
    async def file(name: str):
        if not (path := files.path(name)):
            raise GatewayError(404, "file_not_found", f"file {name} not found")
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix[1:]], headers=headers)

    app.include_router(router)
    return app
