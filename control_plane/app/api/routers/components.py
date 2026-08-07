"""Container / pod state, reconciled against the expected component list."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from app.adapters.registry import get_docker_adapter
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import load_cluster, resolve_live
from app.schemas.component import ComponentRead, ComponentsEnvelope, ComponentsLive

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["components"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]


@router.get(
    "/components",
    response_model=ComponentsEnvelope,
    summary="Container and pod state",
    description=(
        "Component state read over the Docker socket, filtered by the "
        "`com.milvus-cp.component` label and **reconciled against the "
        "configured expected-component list**.\n\n"
        'A component that has vanished is reported as `state: "missing"` '
        "rather than being left out. Omitting it would turn an outage into an "
        'empty row, which reads as "fine" -- the difference between a '
        "dashboard that shows a problem and one that shows nothing. Stopped "
        "containers are included too, with their `exit_code`.\n\n"
        "**Degradation envelope.** An unreachable Docker socket returns "
        '**200** with `degraded_reason.code = "DOCKER_UNAVAILABLE"`. The '
        "control plane stays useful without Docker; it just cannot see "
        "containers."
    ),
)
async def get_components(
    cluster_id: ClusterId, session: DbDep, settings: SettingsDep
) -> ComponentsEnvelope:
    context = await load_cluster(session, cluster_id)
    docker = get_docker_adapter(settings)

    async def fetch() -> ComponentsLive:
        rows = await docker.list_components(compose_project=context.compose_project)
        components = [ComponentRead(**r.as_dict()) for r in rows]
        return ComponentsLive(
            components=components,
            total=len(components),
            running=sum(1 for c in components if c.state == "running"),
            missing=sum(1 for c in components if c.state == "missing"),
        )

    outcome = await resolve_live(cluster_id=cluster_id, resource="components", fetch=fetch)
    return ComponentsEnvelope(cluster=context.read, **outcome.envelope_kwargs())
