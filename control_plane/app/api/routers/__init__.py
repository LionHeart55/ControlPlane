"""API routers.

Mount order matters. `clusters` declares `/clusters/{cluster_id}` and the
resource routers declare paths beneath it; FastAPI matches in registration
order, so the more specific sub-resource routers are mounted first to keep a
literal segment from being captured as a path parameter.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    clusters,
    collections,
    components,
    events,
    health,
    logs,
    metrics,
    overview,
    system,
)

api_router = APIRouter()
api_router.include_router(overview.router)
api_router.include_router(health.router)
api_router.include_router(collections.router)
api_router.include_router(metrics.router)
api_router.include_router(components.router)
api_router.include_router(logs.router)
api_router.include_router(clusters.router)
api_router.include_router(events.router)

__all__ = ["api_router", "system"]
