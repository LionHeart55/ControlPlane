"""Recent container logs for an allowlisted component."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.adapters.docker_client import DEFAULT_LOG_LINES, MAX_LOG_LINES
from app.adapters.registry import get_docker_adapter
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import load_cluster, resolve_live
from app.schemas.common import ErrorResponse
from app.schemas.logs import LogLineRead, LogsEnvelope, LogsLive

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["logs"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]


@router.get(
    "/logs",
    response_model=LogsEnvelope,
    summary="Recent component logs",
    description=(
        "Tail of one component's logs, stdout and stderr merged in timestamp "
        "order and each line tagged with its stream.\n\n"
        "**The component name is validated against an allowlist inside the "
        "adapter**, not merely in this route. User input never reaches a "
        "container lookup: an unknown or malformed name is rejected with "
        "**422** before any Docker call is made. A name that is allowlisted "
        "but has no container is **404**.\n\n"
        "`lines` is capped server-side at "
        f"{MAX_LOG_LINES}; asking for more is silently clamped and `truncated` "
        "is set.\n\n"
        "**Never served from cache.** A stale log tail is indistinguishable "
        "from a live one and would send someone debugging the wrong minute, so "
        "when Docker is unreachable this returns `live: null` rather than an "
        "old tail."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "No container for that component."},
        422: {"model": ErrorResponse, "description": "Component name not in the allowlist."},
    },
)
async def get_logs(
    cluster_id: ClusterId,
    session: DbDep,
    settings: SettingsDep,
    component: Annotated[
        str, Query(description="Component name. Must be in the adapter's allowlist.")
    ],
    lines: Annotated[int, Query(ge=1, le=MAX_LOG_LINES)] = DEFAULT_LOG_LINES,
    since: Annotated[
        str | None,
        Query(description="Relative window such as `30s`, `10m`, `2h` or `1d`."),
    ] = None,
) -> LogsEnvelope:
    context = await load_cluster(session, cluster_id)
    docker = get_docker_adapter(settings)

    async def fetch() -> LogsLive:
        tail = await docker.tail_logs(component, lines=lines, since=since)
        return LogsLive(
            component=component,
            lines=[LogLineRead(**line.as_dict()) for line in tail],
            count=len(tail),
            truncated=len(tail) >= min(lines, MAX_LOG_LINES),
        )

    outcome = await resolve_live(
        cluster_id=cluster_id,
        resource=f"logs:{component}",
        fetch=fetch,
        cacheable=False,
    )
    return LogsEnvelope(cluster=context.read, **outcome.envelope_kwargs())
