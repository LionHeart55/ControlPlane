"""FastAPI application: lifespan, middleware, liveness and readiness.

Startup deliberately tolerates an unreachable Postgres. Refusing to boot when
a dependency is down would defeat the point of a control plane: the moment you
most need it to tell you what is broken is exactly the moment something is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.registry import close_all as close_adapters
from app.api.errors import register_exception_handlers
from app.api.routers import api_router, system
from app.api.routers.system import check_database as _check_database
from app.config import get_settings
from app.db.session import dispose_engine, get_engine
from app.jobs.base import job_session
from app.jobs.scheduler import shutdown_scheduler, start_scheduler
from app.logging_conf import RequestContextMiddleware, configure_logging, get_logger
from app.services.cluster_service import ensure_default_cluster

log = get_logger("main")

API_V1 = "/api/v1"


async def _bootstrap_cluster() -> None:
    """Register the cluster described by .env if none exists yet. Never raises.

    Startup must not fail because bootstrapping did: the API being up is more
    important than the convenience of a pre-registered cluster, and the next
    restart retries.
    """
    try:
        async with job_session() as session:
            await ensure_default_cluster(session)
    except Exception as exc:
        log.warning("cluster_bootstrap_failed", error=f"{type(exc).__name__}: {exc}"[:300])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.cp_log_level)

    log.info(
        "startup",
        milvus_uri=settings.milvus_uri,
        postgres=f"{settings.postgres_host}:{settings.postgres_port}",
        health_interval_s=settings.cp_health_interval_s,
    )

    # Build the engine but do not require it. pool_pre_ping means a Postgres
    # that comes back later is picked up transparently, with no restart.
    get_engine()
    reachable, error = await _check_database()
    if reachable:
        log.info("database_connected")
        await _bootstrap_cluster()
    else:
        log.warning("database_unreachable_at_startup", error=error)

    start_scheduler(settings)

    try:
        yield
    finally:
        log.info("shutdown")
        shutdown_scheduler()
        await close_adapters()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.cp_log_level)

    app = FastAPI(
        title="Milvus Control Plane",
        version="0.1.0",
        description=(
            "Control plane for a Milvus 2.6 deployment: registration, health, "
            "collections, metrics, components and logs.\n\n"
            "**Degradation contract.** Endpoints that mix stored and live data "
            "return a fixed envelope with `live`, `live_status`, `stale` and "
            "`degraded_reason` fields. An unreachable Milvus, MinIO or Docker "
            'socket yields HTTP 200 with `live_status: "unavailable"`, never a '
            "5xx. Only an unreachable PostgreSQL produces 503, and only on "
            "routes that need stored metadata to answer."
        ),
        lifespan=lifespan,
        openapi_tags=[
            {"name": "system", "description": "Liveness and readiness of the control plane."},
            {"name": "clusters", "description": "Registered Milvus deployments."},
            {"name": "health", "description": "Live probes and persisted health history."},
            {"name": "collections", "description": "Collection inventory and statistics."},
            {"name": "metrics", "description": "Curated runtime metrics scraped from Milvus."},
            {"name": "components", "description": "Container and pod state."},
            {"name": "logs", "description": "Recent component logs."},
            {"name": "events", "description": "Audit and incident trail."},
            {"name": "overview", "description": "Aggregate fan-out for the dashboard."},
        ],
    )

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(system.router)
    app.include_router(api_router, prefix=API_V1)

    return app


app = create_app()
