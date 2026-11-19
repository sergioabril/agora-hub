import asyncio
import json
import secrets
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .controller import Controller
from .harness import Harness, Message, UpstreamDown, UpstreamError, error_text
from .responses import BadRequest, respond
from .tools import FileStore, Tools
from .trace import trace


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
    async def gateway_error(request: Request, error: GatewayError):
        trace(getattr(request.state, "trace", None), "error", status=error.status, code=error.code, message=error.message)
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

    async def race(request: Request, work, name: str) -> Response:
        """FastAPI would run a non-streaming turn to the end after the client left: race it against the disconnect."""
        task, gone = asyncio.ensure_future(work), asyncio.ensure_future(request.receive())
        await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            trace(request.state.trace, "disconnect")
            return Response(status_code=499)  # never delivered: the client has gone
        gone.cancel()
        try:
            return task.result()
        except BadRequest as error:
            raise GatewayError(400, error.code, error.message)
        except UpstreamDown as error:
            raise GatewayError(502, "upstream_unavailable", f"{name}: {error}")
        except UpstreamError as error:
            raise GatewayError(error.status, "upstream_error", f"{name}: {error.message}")

    async def relay(upstream: httpx.Response, request_id: str):
        message = Message()
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:") and (data := line[5:].strip()) != "[DONE]":
                    try:
                        message.add(json.loads(data))
                    except ValueError:
                        pass
                yield line + "\n"
            trace(request_id, "upstream.response", status=upstream.status_code, message=message.summary())
        except httpx.HTTPError as error:
            trace(request_id, "upstream.error", error=error_text(error))
        except (asyncio.CancelledError, GeneratorExit):
            trace(request_id, "disconnect", message=message.summary())
            raise
        finally:
            await upstream.aclose()

    async def forward(body: dict, url: str, name: str, request_id: str) -> Response:
        trace(request_id, "upstream.request", url=url, body=body)
        try:
            upstream = await client.send(client.build_request("POST", url, json=body), stream=True)
            content_type = upstream.headers.get("content-type", "")
            if not content_type.startswith("text/event-stream"):
                await upstream.aread()
        except httpx.RequestError as error:
            trace(request_id, "upstream.error", error=error_text(error))
            raise GatewayError(502, "upstream_unavailable", f"{name}: {error_text(error)}")
        if content_type.startswith("text/event-stream"):
            headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            return StreamingResponse(relay(upstream, request_id), upstream.status_code, headers, content_type)
        try:
            logged = upstream.json()
        except ValueError:
            logged = upstream.text
        trace(request_id, "upstream.response", status=upstream.status_code, body=logged)
        return Response(upstream.content, upstream.status_code, media_type=content_type or None)

    @router.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        body = await parse(request)
        request_id = request.state.trace = f"chat_{secrets.token_hex(8)}"
        trace(request_id, "request", endpoint="chat/completions", body=body)
        deployment_id, model = resolve(body["model"])
        deployment = config.deployments[deployment_id]
        url = str(deployment.endpoint).rstrip("/") + "/chat/completions"
        return await race(request, forward({**body, "model": model}, url, deployment.name, request_id), deployment.name)

    @router.post("/openai/v1/responses")
    async def responses(request: Request):
        body = await parse(request)
        request_id = request.state.trace = f"resp_{secrets.token_hex(16)}"
        trace(request_id, "request", endpoint="responses", body=body)
        deployment_id, model = resolve(body["model"])
        deployment = config.deployments[deployment_id]
        url = str(deployment.endpoint).rstrip("/") + "/chat/completions"
        work = respond(harness, files, {**body, "model": model}, url, request_id)
        return await race(request, work, deployment.name)

    @router.api_route("/openai/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def not_served(path: str, request: Request):
        served = "GET /openai/v1/models, POST /openai/v1/chat/completions and POST /openai/v1/responses"
        raise GatewayError(404, "not_found", f"{request.method} /openai/v1/{path} is not served; the gateway serves {served}")

    app.include_router(router)
    return app
