from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, service
from .core.auth import set_database
from .core.config import config
from .core.database import Database
from .core.logger import debug_logger
from .services.captcha_runtime import CaptchaRuntime


db = Database()
runtime = CaptchaRuntime(db)

set_database(db)
service.set_dependencies(db, runtime)
admin.set_dependencies(db, runtime)


@asynccontextmanager
async def lifespan(app: FastAPI):
    debug_logger.log_info("=" * 60)
    debug_logger.log_info("flow_captcha_service starting...")

    await db.init_db()
    await runtime.start()

    debug_logger.log_info(f"node={config.node_name}, role={config.cluster_role}")
    debug_logger.log_info("startup complete")
    debug_logger.log_info("=" * 60)

    yield

    debug_logger.log_info("flow_captcha_service shutting down...")
    await runtime.close()
    debug_logger.log_info("shutdown complete")


app = FastAPI(
    title="flow_captcha_service",
    version="0.1.0",
    description="Headed captcha service for Flow2API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(service.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {
        "service": "flow_captcha_service",
        "status": "ok",
        "node": config.node_name,
        "role": config.cluster_role,
    }
