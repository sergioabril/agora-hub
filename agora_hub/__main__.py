import argparse
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .adapters import make_adapters
from .config import ConfigError, load_config
from .controller import Controller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-dir", type=Path, default=Path("~/.config/agora/agora-hub")
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    try:
        config = load_config(args.config_dir.expanduser())
    except ConfigError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from error

    controller = Controller(config, make_adapters(config))
    uvicorn.run(
        create_app(config, controller),
        host=args.host if args.host is not None else config.hub.api.host,
        port=args.port if args.port is not None else config.hub.api.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
