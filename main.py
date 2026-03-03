from __future__ import annotations

import uvicorn

from src.core.config import config


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False,
    )
