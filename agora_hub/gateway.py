import json
import secrets
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import Config
from .controller import Controller
from .harness import BadRequest, Harness, UpstreamDown
from .tools import MEDIA_TYPES, FileStore, Tools

REQUEST_DROP = {"authorization", "host", "content-length", "accept-encoding", "connection", "transfer-encoding"}
RESPONSE_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


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
        serving = controller.serving("llm")
        if not serving:
            if name := controller.starting("llm"):
                raise GatewayError(503, "no_llm_running", f"llm deployment {name} is starting", {"Retry-After": "30"})
            raise GatewayError(503, "no_llm_running", "no llm deployment is running")
        if model == "agora":
            return serving[0][0], serving[0][1][0]
        for deployment_id, models in serving:
            if model in models:
                return deployment_id, model
        ids = ", ".join(model_id for _, models in serving for model_id in models)
        raise GatewayError(404, "model_not_found", f"model {model} is not served; use agora or one of: {ids}")

    def image_target() -> tuple[str, str] | None:
        if serving := controller.serving("image"):
            return str(config.deployments[serving[0][0]].endpoint).rstrip("/"), serving[0][1][0]
        return None

    files = FileStore(config.config_dir / "files")
    harness = Harness(client, Tools(client, files, image_target, config.hub.timeouts.image_seconds), files)

    async def parse(request: Request) -> tuple[bytes, dict]:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise GatewayError(400, "invalid_request", "body must be a JSON object with a string model")
        return raw, body

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter(dependencies=[Depends(authenticate)])

    @app.exception_handler(GatewayError)
    async def gateway_error(_request, error: GatewayError):
        kind = "invalid_request_error" if error.status < 500 else "server_error"
        content = {"error": {"message": error.message, "type": kind, "code": error.code}}
        return JSONResponse(content, error.status, error.headers)

    @router.get("/proxy/v1/models")
    @router.get("/agent/v1/models")
    async def models():
        serving = controller.serving("llm")
        data = [{"id": "agora", "object": "model", "created": 0, "owned_by": "agora"}] if serving else []
        for deployment_id, ids in serving:
            data += [{"id": model_id, "object": "model", "created": 0, "owned_by": deployment_id} for model_id in ids]
        return {"object": "list", "data": data}

    @router.post("/proxy/v1/{path:path}")
    async def proxy(path: str, request: Request):
        raw, body = await parse(request)
        deployment_id, model = resolve(body["model"])
        if body["model"] == "agora":
            raw = json.dumps({**body, "model": model}).encode()
        deployment = config.deployments[deployment_id]
        url = str(deployment.endpoint).rstrip("/") + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = [(key, value) for key, value in request.headers.raw if key.decode().lower() not in REQUEST_DROP]
        try:
            upstream = await client.send(client.build_request("POST", url, headers=headers, content=raw), stream=True)
        except httpx.RequestError as error:
            raise GatewayError(502, "upstream_unavailable", f"{deployment.name}: {str(error) or type(error).__name__}")

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in RESPONSE_DROP}
        if upstream.headers.get("content-type", "").startswith("text/event-stream"):
            response_headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return StreamingResponse(relay(), upstream.status_code, response_headers)

    @router.post("/agent/v1/chat/completions")
    async def agent(request: Request):
        _, body = await parse(request)
        deployment_id, body["model"] = resolve(body["model"])
        deployment, gateway = config.deployments[deployment_id], config.hub.gateway
        base_url = f"http://{gateway.public_host}:{gateway.port}/" if gateway.public_host else str(request.base_url)
        try:
            return await harness.chat(body, str(deployment.endpoint).rstrip("/") + "/chat/completions", base_url)
        except BadRequest as error:
            raise GatewayError(400, "invalid_request", str(error))
        except UpstreamDown as error:
            raise GatewayError(502, "upstream_unavailable", f"{deployment.name}: {error}")

    @app.get("/agent/files/{name}")
    async def agent_file(name: str):
        if not (path := files.path(name)):
            raise GatewayError(404, "file_not_found", f"file {name} not found")
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix[1:]], headers=headers)

    app.include_router(router)
    return app
