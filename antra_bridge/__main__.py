"""python -m antra_bridge  ->  run the HTTP server."""
from __future__ import annotations

import uvicorn

from .config import BridgeConfig


def main() -> None:
    cfg = BridgeConfig.from_env()
    from .app import app  # imported after config setup so logging/env initialize in order

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
