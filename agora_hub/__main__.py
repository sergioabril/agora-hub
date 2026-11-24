import argparse
import asyncio
import socket
import sys
from pathlib import Path

import uvicorn

from .accounts import Accounts
from .api import create_app
from .adapters import make_adapters
from .config import ConfigError, load_config
from .controller import Controller
from .gateway import create_gateway


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-dir", type=Path, default=Path("~/.config/agora/agora-hub")
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--gateway-port", type=int)
    parser.add_argument("--public-host")
    args = parser.parse_args()

    try:
        config = load_config(args.config_dir.expanduser())
    except ConfigError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from error

    gateway_config = config.hub.gateway
    if args.gateway_port is not None:
        gateway_config.port = args.gateway_port
    if args.public_host is not None:
        gateway_config.public_host = args.public_host
    adapters = make_adapters(config)
    controller = Controller(config, adapters)
    accounts = Accounts(config.config_dir / "agora.db")
    gateway = create_gateway(config, controller, accounts)
    admin = create_app(config, controller, adapters.home_assistant, accounts, shutdown=gateway.state.close)
    gateway_port = gateway_config.port

    async def dispatch(scope, receive, send):
        app = gateway if scope["type"] != "lifespan" and scope["server"][1] == gateway_port else admin
        await app(scope, receive, send)

    host = args.host if args.host is not None else config.hub.api.host
    port = args.port if args.port is not None else config.hub.api.port
    sockets = [socket.create_server((host, port)), socket.create_server((host, gateway_port))]
    asyncio.run(uvicorn.Server(uvicorn.Config(dispatch)).serve(sockets=sockets))


if __name__ == "__main__":
    main()
